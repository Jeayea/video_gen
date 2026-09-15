# FIXES — 修复记录

> 对照 [REVIEW.md](REVIEW.md) 的问题清单逐条修复。验证方式：104 个离线测试（`FakeLLM` / `FakeVideoProvider` 注入）+ `ruff` + 一次真实 OpenRouter 文本链路 + 一次真实 ffmpeg 视频链路。

## v0.3 · Phase 2 视频阶段（新增）

| 能力 | 实现 | 位置 |
|------|------|------|
| VideoProvider 抽象 | `generate(VideoRequest) -> GeneratedClip`；约定失败必须抛 `VideoProviderError`，不许返回空文件假装成功 | `providers/video/base.py` |
| MockVideoProvider | 有 ffmpeg 时用 `libx264` 生成真实 MP4（纯色 + 横扫白条，一眼看出是视频）；没有 ffmpeg 时写 `.placeholder.json` 并把 `media_kind` 标成 `placeholder` | `providers/video/mock.py` |
| ffmpeg 解析 | `FFMPEG_PATH` → `PATH` → `imageio-ffmpeg` 自带二进制；找不到就明确报错/降级，不静默失败 | `utils/ffmpeg.py` |
| 并发 + 重试 + 隔离 | 信号量控并发，单镜失败重试 N 次（指数退避），仍失败只标记该镜头不阻塞其他镜头；失败/取消清理半截文件 | `services/video_service.py` |
| 断点续跑 | `video_manifest.json` 记录每镜状态，已 ready 且文件仍在的镜头直接跳过，重试只补失败/缺失的 | 同上 + `pipeline._run_video_job` |
| 后台任务队列 | 提交即返回 job；进度/百分比、取消、单项目互斥、历史上限裁剪、退出时取消所有任务 | `orchestrator/jobs.py` |
| 状态与恢复 | `VIDEO_GENERATING → VIDEO_READY`；全失败退回 `STORYBOARD_CONFIRMED`（可重试）；启动时回收卡在「进行中」的项目 | `orchestrator/pipeline.py` |
| API | `video/generate`(202) · `video/status` · `video/manifest` · `video/cancel` · `clips/{shot_id}` · `jobs/{id}` | `main.py` |
| Web UI | 第 4 阶段真实面板：片段网格 + 内嵌播放 + 进度条轮询 + 取消 + 重试失败镜头 + 占位/失败标记 | `static/index.html` |

### 开发过程中发现并修掉的两个真实 bug

1. **取消一个还没被调度的任务会永远卡在 queued**：任务在首次执行前被 `task.cancel()`，协程体根本不会运行，
   状态就永远停在 `queued`（`is_active=True`）。修法：`cancel()` 里同步落定状态，`shutdown()` 同理；
   `_run()` 开头对已取消的任务直接返回。→ `orchestrator/jobs.py`
2. **重复提交视频任务会阻塞到上一个任务跑完**：正在跑的视频任务会一直持有项目锁，而「是否已有任务」的检查
   原本在锁内，于是第二次 POST 会挂住几分钟（真实 provider 场景）。修法：锁外先查任务表快速拒绝，
   锁内再查一次防竞态。→ `pipeline.start_video_generation()`

### 真实验证（MockVideoProvider + 真实 ffmpeg）

手工构造 2 场 4 镜（3/3/4/4 秒，9:16）→ `POST /video/generate` → 1 秒内 4/4 成功：

```
scene_1_shot_1.mp4  3.00s  h264 540x960 yuv420p 24fps
scene_1_shot_2.mp4  3.00s  h264 540x960 yuv420p 24fps
scene_2_shot_1.mp4  4.00s  h264 540x960 yuv420p 24fps
scene_2_shot_2.mp4  4.00s  h264 540x960 yuv420p 24fps
project_status: video_ready | summary: ready=4 failed=0 usable=True duration=14s
GET /clips/scene_2_shot_1 → 200 video/mp4 (6959 bytes)
GET /clips/..%2F..%2Fmeta → 404（越界被拒）
再次提交 → 21ms 返回，0 次重复生成（续跑生效）
```

## v0.2 · 文本链路修复

## P0 · 阻断级

| # | 问题 | 修复 | 位置 | 验证 |
|---|------|------|------|------|
| P0-1 | 分镜确认后卡死在 `VIDEO_READY`，且无法回退 | 新增显式 `STORYBOARD_CONFIRMED`；Phase 2 未实现前不再写入 `VIDEO_READY`；新增 `POST /storyboard/reopen`；UI 明确标注第 4 阶段未实现 | `models/project.py`、`orchestrator/pipeline.py`、`main.py`、`static/index.html` | 实测 `confirm → storyboard_confirmed → reopen → script_confirmed` |
| P0-2 | 目标 45s 实际生成 225s，无任何校验 | 大纲阶段把总预算均分到每场并写进 `outline.json`；`normalize_script_durations()` 偏差 >15% 时按比例归一化；分镜按场精确对齐；校验报告新增 `duration` 类问题 | `services/script_service.py`、`services/storyboard_service.py`、`utils/duration.py`、`services/consistency_service.py` | 真实模型：目标 30s → 剧本 30s、分镜每场合计 == 该场剧本时长 |
| P0-3 | `duration_sec: 7.5` / `"约10秒"` / `null` 直接让整个项目报废 | 新增容错层 `utils/coerce.py`，`duration_sec` / `age` / `shot_no` / `list[str]` 字段全部加 before-validator；无法解析时回退默认值 | `utils/coerce.py`、`models/{script,storyboard,bible,world_state}.py` | `tests/test_coercion.py`（22 例） |
| P0-4 | JSON 解析只认整段 ``` 包裹，无重试 | 重写 `_extract_json`：括号配平扫描截取 + 中文标点/智能引号/尾逗号修复；`chat_json(schema=...)` 校验失败把错误回灌给模型重试；HTTP 429/5xx 指数退避；模型不支持 `response_format` 时自动降级 | `providers/llm/openrouter.py`、`providers/llm/base.py` | `tests/test_json_extract.py`（15 例），含 3 次尝试的失败路径 |
| P0-5 | `confirm_script()` 连续赋值 4 次，确认可被静默覆盖 | 状态机重写：`SCRIPT_REVIEW → SCRIPT_CONFIRMED`；生成剧本的入口白名单收紧为 `{draft, failed, script_review}`；误用状态改为显式 `reopen` | `orchestrator/pipeline.py` | `tests/test_state_machine.py::test_confirm_then_regenerate_script_is_blocked` |
| P0-6 | `PUT /script` 无状态门控，可清空已确认内容 | 所有写操作走 `Pipeline._require(project, 白名单, action)`；每个项目一把 `asyncio.Lock`；`FileStore` 改原子写（`mkstemp` + `os.replace`）；项目 id 白名单校验，堵住 `..` 路径穿越 | `orchestrator/pipeline.py`、`utils/file_store.py` | `tests/test_api.py::test_full_flow_through_api`；真实 API 返回 400 |

## P1 · 工程与安全

| # | 问题 | 修复 |
|---|------|------|
| P1-1 | 真实密钥明文散落风险 | 所有异常/日志经 `scrub_secrets()` 脱敏；`Settings` 密钥字段 `repr=False`；`/api/health` 只返回 `llm_key_configured` 布尔值；日志只打印 `key_configured=true/false` |
| P1-2 | `run.sh` 绑 `0.0.0.0` 且零鉴权 | 默认改 `127.0.0.1`（`HOST`/`PORT`/`RELOAD` 可覆盖）；新增可选 `API_TOKEN`，中间件强制校验，`/api/health` 豁免；UI 支持 `?token=` 并记住 |
| P1-3 | 零测试 | 新增 71 个测试：`tests/{fakes,conftest}.py` + `test_json_extract.py` + `test_coercion.py` + `test_state_machine.py` + `test_api.py` |
| P1-4 | `LLMProvider` 缺 `chat_json`，provider 硬编码 | `chat_json` 提升为抽象方法；新增 `get_llm()` 工厂；`Pipeline(llm_factory=...)` 注入点，测试可完全离线 |
| P1-5 | `/static/*` 404，UI 路径靠猜 | `app.mount("/static", StaticFiles(...))`；路径由 `Settings.static_path` 从 `__file__` 推导，支持 `STATIC_DIR` 覆盖 |
| P1-6 | 非原子写、无并发保护 | 原子写 + 项目级 `asyncio.Lock`；**同步长请求未改**（见「遗留」） |
| P1-7 | 错误信息暴露 enum repr | 统一用 `project.status.value`，并提示需要哪些状态 |
| P1-8 | 死代码 / 无用依赖 | 删除重复的 `BIBLE_SYSTEM`、未用的 import、`jinja2` 依赖；`edge-tts` 保留并注明 Phase 2 用 |
| P1-9 | 无 README | 新增 [README.md](README.md)（状态表 / 快速开始 / API / 结构 / 限制 / 安全），DESIGN 追加第 10 节说明实现状态 |
| P1-10 | 无重试、无配置校验、错误信息裸栈 | HTTP 退避重试；缺 key 时抛出可读的 `LLMError` 并在启动日志告警；`LLMError` 统一映射为 502、`PipelineError` → 400、`PipelineNotFound` → 404 |

## P2 · 质量

- **一致性校验补齐**：新增分镜层校验（说话人是否在画面内、`scene_id` 是否存在、`video_prompt` 是否缺失/是否纯英文、时长是否合法、镜头数 2-5）；数字比对改为「数字集合交集」而非子串；人名启发式加停用词表；报告带 `stage` 字段与严重级计数
- **video_prompt 不再含内部 id**：`sanitize_video_prompt()` 把 `char_1`/`loc_1` 换成具体外貌/环境描述（实测旧输出 `Medium shot of char_3, ...` 已消失）
- **对白清洗**：`clean_dialogue_line()` 去掉 `（咬牙）` 这类舞台提示与包裹引号（实测真实输出已无括号台词）
- **World State 语义修正**：`open_threads` 由整体替换改为有序合并；`knows/possesses` 去重且顺序稳定，便于 diff
- **前端**：中文状态标签 + 新状态样式；按状态渲染「生成/确认/退回」按钮；剧本面板显示「实际时长 / 目标时长」与「已归一化」提示；一致性面板显示错误/警告/提示计数与阶段；项目可删除

## 验证结果

```
ruff check src tests   → All checks passed!
pytest -q              → 71 passed
```

真实 OpenRouter 端到端（`target_duration_sec=30`，gemini-2.5-flash）：

| 步骤 | 结果 |
|------|------|
| 生成圣经 | 200，8.3s |
| 生成剧本（3 场） | 200，17.3s，合计 **30s == 目标 30s**，无括号台词 |
| 确认剧本 | `script_confirmed` |
| `PUT /script`（已确认） | **400**「当前状态 'script_confirmed' 不允许修改剧本（需要 draft/failed/script_review）」 |
| 生成分镜 | 200，17.5s，10 镜，每场合计 == 该场剧本时长，`video_prompt` 全英文且无 `char_`/`loc_` |
| 确认分镜 | `storyboard_confirmed`（不再假报 `video_ready`） |
| 退回分镜 | `script_confirmed` |
| 一致性报告 | `stage=script+storyboard` |

密钥检查：服务日志中密钥出现次数 **0**；仓库内除测试用假串外无密钥；测试项目已删除。

## 遗留 / 下一步（Phase 2）

1. **视频/配音/合成**：`VideoProvider` + `MockVideoProvider` → Edge-TTS → BGM 曲库 → FFmpeg 合成烧字幕
2. **后台任务化**：生成接口仍是同步长请求，需要 job 队列 + `GET /jobs/{id}` 进度（也为视频轮询铺路）
3. **成本统计**：目前只在日志里打印 token 数
4. **多进程锁**：当前锁是单进程内的，多 worker 部署需换文件锁或数据库


---

## v0.4 — Phase A：配音 + BGM + 成片合成

补齐「主题 → 成片」的最后一段：`TTSProvider` 抽象（Edge-TTS 真实配音 / Mock 离线占位）、
本地 BGM 曲库按情绪选曲、FFmpeg 分段拼接 + 混音 + 字幕，以及音画时长对齐。

### 新增能力

| 能力 | 说明 |
|------|------|
| 配音 | `EdgeTTSProvider`：中文音色、按角色性别分配音色、换音色只重跑该角色 |
| 音画对齐 | 配音比镜头长就**拉长镜头**（`AUDIO_GAP_SEC` 留白），合成时 `tpad` 冻结末帧补足 |
| BGM | `BGM_DIR` + `bgm.json` 元数据，按镜头 `mood` 打分匹配；曲库为空就出无声底成片 |
| 合成 | 每镜分段（统一分辨率/帧率/采样率）→ concat 流拷贝 → BGM 混音 → 字幕 |
| 字幕 | `soft`（mov_text 内嵌）/ `burn`（libass 烧录）/ `none`；无论哪种都导出 `.srt` |
| 状态机 | 新增 `AUDIO_GENERATING → AUDIO_READY → COMPOSING → DONE`，同为后台任务（进度/取消/互斥） |

### 开发中发现并修掉的问题

| # | 问题 | 表现 | 修复 |
|---|------|------|------|
| A1 | 中文语音只返回 `SentenceBoundary` | 只认 `WordBoundary` 时时长恒为 0，音画对齐失效 | 两种边界都认，末位 + 尾巴补偿；有 `ffprobe` 时以真实时长为准，再退化为按 48kbps 码率估算 |
| A2 | 竖屏字幕溢出画面 | `force_style` 的字号是 **384x288 参考系**单位，直接写像素会让 1080x1920 上字幕大到出框 | 按「min(宽,高) × 5% 像素」算目标字号，再换算回参考系单位（1080 宽 → `FontSize=8`）；抽帧人工确认 |
| A3 | `-map` 被当成输入选项 | 软字幕那条命令报 `Option map ... cannot be applied to input url subtitles.srt` | ffmpeg 的所有 `-i` 必须写在输出选项之前 |
| A4 | 把 mp4 当 concat 列表读 | 收尾阶段报 `Invalid data found when processing input` | 拼接产物是普通 mp4，只有 concat **列表文件**才用 `-f concat` |
| A5 | `woman` 里的 `man` | 女主被判成男声（中英关键词都用了子串匹配） | 英文关键词改成整词匹配，中文仍用包含匹配 |
| A6 | 系统 ffmpeg 没有 libass | 请求烧字幕会直接失败 | 启动时探测 `-filters`/`-encoders` 能力并缓存，`burn` 不可用就降级 `soft`，`mov_text` 也没有就只导出 srt，并在报告里告警 |
| A7 | 上游改了、下游还在 | 分镜重生成后旧 `video_manifest.json` 还在，会跳过重新生成而沿用过期片段 | 文本/分镜一变就清掉下游媒体；补跑镜头作废成片但保留配音 |
| A8 | 取消一个还没被调度的任务 | 项目永远停在 `AUDIO_GENERATING`/`COMPOSING` | `cancel()` 同步落定状态，`recover_interrupted_jobs()` 按「产物是否真的存在」回收 |

### 验证

- `ruff check src tests` 全绿；`pytest -q` **180 passed**（新增 audio / compose / API / 状态机用例）
- 真实链路（不花 LLM 的钱）：手工分镜 → mock 视频 → **真实 edge-tts**（`zh-CN-XiaoxiaoNeural`）
  → 音画对齐（1s 镜头因 3.41s 配音拉长到 4s）→ 合成
  - 成片 `h264 1080x1920 24fps + aac 44100Hz 立体声`，时长 12.00s（与分镜一致）
  - 烧字幕模式：抽帧确认中文正常渲染且不出框（见 A2）
  - 软字幕模式（系统 ffmpeg）：额外得到 `Stream #0:2 (zho): Subtitle: mov_text`
  - BGM 命中 `tense_01（暗涌）`（分镜 mood = 紧张）
- UI：用真实 API 载荷在 stub DOM 上跑渲染冒烟（18 项检查全通过）


---

## v0.5 — Phase B：成本 / 批量 / 多模型 / 角色一致性

### 新增能力

| 能力 | 说明 |
|------|------|
| 成本账本 | 每个阶段落一条 `CostEntry`（token / 秒数 / 字符 → USD），存 `cost_report.json` |
| 预算门控 | `BUDGET_USD` > 0 时开工前检查「已花 + 预估」，超了抛 `BudgetExceeded`（HTTP 400）并说明差额 |
| 批量生产 | `BatchManager` + `Pipeline.run_chain`：多主题自动跑完整链路，可 `stop_after` 任一步骤 |
| 多模型切换 | 项目级 `llm_model` / 单次请求 `{"model": ...}`；候选与单价走配置，账本记录实际模型 |
| 角色一致性 | 参考图（base64 上传到 `refs/`）+ seed 透传到 `VideoRequest`；mock provider 用参考图取色 |
| UI | 第 7 面板成本表、第 8 面板批量生产、项目级模型下拉、角色参考图上传 |

### 设计取舍

| 决定 | 原因 |
|------|------|
| 预算比较留 `1e-9` 容差 | `0.5 - 0.4 = 0.099999...`，浮点直接比大小会把「刚好够」判成超预算 |
| 不知道花费时（LLM 阶段）只要还有余额就放行 | 事前估不准 token，但如果已经顶到预算就该拒绝，否则最后一点额度会被无限刷 |
| 批量复用 `_run_*_job` 而不是另写一套 | 进度/错误/落盘行为与单步执行完全一致，不会出现两条代码路径不一致 |
| 集合成阶段也记账（usd=0） | 本地 ffmpeg 不花钱，但时长/分段数对排查「为什么这次慢」很有用 |
| 参考图用 base64 JSON 而不是 multipart | 不引入 `python-multipart` 依赖，上传接口保持最小 |

### 验证

- `ruff check src tests` 全绿；`pytest -q` **230 passed**（新增 cost / batch / consistency / API 用例）
- 批量端到端（离线假 LLM + mock 视频/配音 + **真实 ffmpeg 合成**，不花 LLM 额度）：
  2 个主题 → 2 条 15.02s 成片（`h264 1080x1920 24fps + aac 44.1kHz 立体声 + mov_text 中文字幕轨`），
  批量状态 `succeeded`，单项目成本账本 6 条（bible/script/storyboard/video/audio/compose），
  视频 15s × $0.05 = $0.75、配音 9 字 × $15/M = $0.000135 与预期一致
- 预算：把预算压到「刚好等于已花费」后，下一阶段被拒绝且项目状态不变（不会被误标 FAILED）
- UI：真实载荷在 stub DOM 上跑渲染冒烟（25 项检查全通过，含成本表 / 批量表 / 模型下拉 / 参考图入口）


---

## v0.6 — 接入真实视频模型（OpenRouter / MiniMax H3）

`VIDEO_PROVIDER=openrouter` 即可用真实模型出片，走 OpenRouter 统一的异步视频接口，
复用同一把 `OPENROUTER_API_KEY`。

### 新增

| 能力 | 说明 |
|------|------|
| `OpenRouterVideoProvider` | `POST /api/v1/videos` 提交 → 轮询 `polling_url` → 下载 `unsigned_urls[0]` |
| 能力驱动参数 | 分辨率/时长档位/是否支持 seed 与参考图全部运行时从 `/api/v1/videos/models` 读（带进程缓存），换模型不改代码 |
| 时长档位对齐 | `planned_seconds()` 把镜头时长抬到模型支持的最短档（H3 是 5 秒），预算与记账都按**实际计费秒数**算 |
| 真实单价 | `price_per_second()` 读 `pricing_skus.duration_seconds`（H3 = $0.13/s），账本不再依赖手填单价 |
| 参考图一致性 | 参考图 → `input_references`（reference-to-video）；模型不支持时退回 `frame_images.first_frame` |
| 错误映射 | 401/402/403/404/429/400 各自给出可读原因（参数不支持 / 余额不足 / 模型不存在…），消息绝不带密钥 |
| 启动提醒 | 启用真实 provider 时启动日志打印 model / 单价 / 最短时长 / 预算 |

### 开发中发现并处理的问题

| # | 问题 | 处理 |
|---|------|------|
| B1 | H3 最短 5 秒，而分镜常见 2~4 秒 | 请求抬到 5 秒，多出的部分由合成阶段 `trim` 掉；预算与账本按 5 秒/镜计（45 秒成片 ≈ $9.75） |
| B2 | H3 的 `seed` 能力是 `false`，带上会被 400 | 只在模型声明 `seed: true` 时才带该参数 |
| B3 | 分辨率只支持 2K，配 1080p 会 400 | 与能力列表比对，自动换成支持的档位并告警 |
| B4 | 参考图可能不被接受 | 400 且请求里带参考图时，去掉参考图重试一次并告警，不让一致性拖垮整条链路 |
| B5 | 空下载会留下 0 字节文件 | 校验大小后删除，避免续跑把它当成"已生成" |
| B6 | provider 可能在已关闭的事件循环里 `aclose()` | 关闭异常降级为 debug 日志（测试里最常见） |
| B7 | 换了 provider/模型后旧片段被当成"已就绪"沿用 | 清单里记了 `provider`/`model`：一旦与当前 provider 不一致，全部片段标回 PENDING 重新生成（否则 mock 占位视频会一直留着，看起来像没生效） |

### 验证

- `ruff check src tests` 全绿；`pytest -q` **256 passed**
- 新增 `tests/test_video_openrouter.py`（26 例）**全部用 `httpx.MockTransport` 打桩**：
  请求体（时长档位/分辨率/比例/无 seed/无音轨）、轮询状态机（pending→processing→completed）、
  失败与超时、空下载、参数被拒后退化重试、参考图两种形态、错误映射（且断言消息里没有密钥）、
  以及「账本按 $0.13 × 10 秒 = $1.30 记账」「预算不足时**不提交**任务」两条联动断言
- 真实调用只做了两件不花钱的事：`GET /api/v1/key`（鉴权有效、非免费层、无限额）、
  `GET /api/v1/videos/models`（H3 的能力与单价），以及一次 `/videos/<不存在的 job>` 确认鉴权生效


---

## v0.7 — 真实调用踩出来的三个 bug

第一次真的用 H3 出片时暴露了三个问题，其中第二个**在重复烧钱**（3 次提交 = 3 份钱）。

| # | 问题 | 现象 | 修复 |
|---|------|------|------|
| C1 | 下载没带鉴权 | 片段生成成功，但 `unsigned_urls` 裸下载 **401**（它其实是 `/api/v1/videos/{id}/content?index=0`，**需要 Authorization 头**；官方 cookbook 里那段 `requests.get(content_url)` 是误导的） | 按「带鉴权的 content 端点 → unsigned_urls（带鉴权）→ unsigned_urls（匿名）」依次尝试，成功路径写日志 |
| C2 | **生成成功后的失败被当成可重试** | `video_service` 的重试策略不区分「生成失败」和「产物下载失败」，于是同一个镜头提交了 3 次 → **白付 3 份钱（$2.49）** | 新增 `VideoArtifactError(retryable=False)`：生成成功后的任何落盘失败都不再重试；`clip.task_id` 仍然记下 job id，错误信息里带上候选地址，方便人工捞回 |
| C3 | 账本用价目表推算 | 实际单价随分辨率/时长档位浮动，`pricing_skus` 只是基础档 | 轮询响应里的 `usage.cost` 是**权威扣费**，现在按它记账（`clip.cost_usd` → 账本）；实际比推算高 20% 以上会告警建议核对单价 |

### 真实端到端验证（1 个镜头，MiniMax H3）

```
① H3 真实生成：minimax/hailuo-3 → 1440x2560 (2K 竖屏) h264 24fps，5.17s，2.37MB
   自带 AAC 32kHz 音轨 + task_id=ppRUqjqKCjDcMQ839hsk
② 真实 Edge-TTS：zh-CN-XiaoxiaoNeural，3.408s → 音画对齐把镜头 3s 拉到 4s
③ 合成：1080x1920 h264 + aac 44.1kHz 立体声 + mov_text(zho) 软字幕，4.02s，$0 本地开销
   （H3 的 5.17s 片段被裁到镜头时长 4s，正是 B1 设计的裁剪路径）
④ 账本：video $0.65（= OpenRouter 自己报的 usage.cost，5s × $0.13）+ audio/compose $0
```

- 抽帧人工确认：画面是真实摄影质感的「手停在杂志封面上的特写」，与 prompt 一致
- 账户级用量增量测得 $0.7009（比 `usage.cost` 多 $0.05）：`/api/v1/key` 的 usage 是**账户级且滞后**的，
  单次口径请以各任务的 `usage.cost` 为准 —— 这也是 C3 让账本改用它的原因
- **代价说明**：因为 C2，第一次尝试重复提交了 3 次，白花了约 $2.49；修复后同一条链路只提交 1 次

### 已知行为（不是 bug）

- H3 的片段自带音轨，但合成时只取视频轨（对白用 TTS 保证清晰）。想保留环境音当底噪需要新增混音选项
- 取消只是停止轮询，**服务端任务可能仍在跑并计费**（异步视频 API 的固有行为）


---

## v0.8 — 环境音混音 + 测试与付费服务隔离

### 新增：环境音混音（`COMPOSE_AMBIENT_MODE`）

真实视频模型（H3 等）会自带音轨，之前合成时被整条丢掉。现在可选：

| 模式 | 行为 |
|------|------|
| `off`（默认） | 丢弃模型音轨，对白人声最干净 —— 行为与之前完全一致 |
| `under` | 把模型音轨压到 `COMPOSE_AMBIENT_GAIN_DB`（默认 -20dB），再和人声 `amix`（`normalize=0`，人声音量不被拉低） |

- 镜头没有台词时，环境音就是它唯一的音轨（不会变成静音）
- **先探测再混**：用 `ffprobe` 确认片段真的有音轨；探不到（没装 ffprobe / 片段本身无音轨）
  就只告警并退回 `off` —— 绝不拼一条 ffmpeg 必然报错的滤镜图
- 切换开关会让分段重新渲染（渲染新鲜度里带了环境音开关的标记），否则会复用纯人声的旧分段
- 报告里记录 `ambient_mode` / `ambient_mode_effective` / `ambient_gain_db`；UI 与 API 都可覆盖

### 修复：测试不再受开发者 `.env` 影响

`.env` 里把 `VIDEO_PROVIDER` 切成 `openrouter` 之后，测试开始失败 —— 因为
`test_video_provider_factory` 假设默认是 mock、健康检查断言了 `mock`。
更危险的是：只要哪个测试用了默认工厂去生成视频，就会**真的调付费接口**。

现在 `conftest` 的 autouse fixture 强制 `video_provider=mock`、`budget_usd=0`、退避归零，
测试与付费服务彻底隔离；需要真实 provider 的用例自己用 `httpx.MockTransport` 打桩。

### 实测（用真实 H3 片段，本地 ffmpeg，零 API 费用）

```
H3 片段自带环境音   mean = -19.3 dB  peak = -1.2 dB   ← 几乎和人声一样响！
Edge-TTS 人声        mean = -19.5 dB  peak = -5.9 dB
COMPOSE_AMBIENT_MODE=off   成片音轨 mean = -23.2 dB
COMPOSE_AMBIENT_MODE=under 成片音轨 mean = -23.2 dB（人声没被拉低，normalize=0 生效）
两次成片音频相减的残差 = -49.7 dB（peak -29.3 dB）
```

残差比 16bit 量化噪声（-91dB）高 41dB → 环境音确实混进去了，且落在人声下面约 26dB。

**调参建议**：H3 自带音轨本身很响，`-20 dB` 是保证对白清晰的安全默认；
想要环境音更"在场"可以试 `-12 ~ -14 dB`。`off` 依旧是完全丢弃。

### 验证

- `ruff check src tests` 全绿；`pytest -q` **281 passed**
- 新增 `tests/test_ambient.py`（15 例）：滤镜拼装的四种组合（纯人声 / 纯环境音 / 混音 / 静音）
  与 `normalize=0`、可用性判定（无 ffprobe 降级、片段无音轨降级、off 模式不探测）、
  真实 ffmpeg 混音产物（仍是 1 路视频 + 1 路音频）、
  以及「off → under 必须改变成片字节」（证明分段真的重渲染了）

## v0.9 — 配音文案交给多模态 LLM（看画面写/改台词）

### 背景

分镜里没台词的镜头以前只能是静默镜头；用户问的正是「TTS 能不能也调用 LLM 根据视频内容生成」。
能，但关键是**看真实画面**而不是只看文字描述 —— 所以这一版把视频片段抽帧后按多模态消息发出去。

### 新增

| 能力 | 实现 | 位置 |
|------|------|------|
| 多模态消息 | `LLMMessage.content` 支持 `str \| list[dict]`；`image_message()` 把（说明, data URL）按顺序插进正文，`message_text()` 取文字部分 | `providers/llm/base.py` |
| 抽帧 | `extract_frames()`：`-ss` 取 1-3 帧 → `scale=W:-2` → jpg，缓存在 `projects/<id>/frames/`，片段作废时一起清 | `services/audio_script_service.py` |
| 文案生成 | `apply_voice_script()`：按场分组 → 抽帧 → `chat_json(schema=VoiceScriptResponse)` → 写回分镜 | 同上 |
| 念法建议 | `rate`/`volume`/`pitch` 解析成 edge-tts 格式（`"慢 10%"` → `+10%`），超范围夹逼 | 同上 |
| 断点续跑 | 镜头打 `voice_script_source=llm:<mode>` 标记，同模式重跑跳过；`force_script=True` 才重跑 | 同上 |
| 接线 | 配音任务里先跑文案再合成；token 记到 `CostStage.AUDIO`；预算门禁把文案与 TTS 一起预估 | `orchestrator/pipeline.py` |
| API/UI | `audio/generate` 支持 `script_mode`/`use_vision`/`force_script`；UI 有模式下拉 + 两个勾选框，逐条显示「LLM 文案」与念法 | `main.py` / `static/index.html` |

### 顺带修掉的两个隐患

1. **文案变了不重新合成**：`synthesize_all` 原先只比较音色，LLM 改过的台词会被旧音频盖住。
   现在文案 / 念法（rate/volume/pitch）任一变化都会重新合成该条。→ `services/audio_service.py`
2. **强制重跑形同虚设**：`narration` 模式的候选镜头是「本来就没台词的」，第一轮补完之后
   就再也选不中了，`force` 无从生效。现在 `force` 会把「本模式补过的旁白」也重新纳入候选。
   → `services/audio_script_service.py`

### 设计取舍

- **按场调用**而不是整片一次：一场 2-5 个镜头，图片数量可控；单场失败只影响一场
- **一律降级**：调用失败 / 坏 JSON / 模型漏镜头 / 抽帧失败，都只记 warning 并用原文案继续配音，
  文案阶段永远不会把配音打死（有专门的测试钉住这条）
- **结果写回分镜**：好处是字幕自动跟着新文案走、续跑天然幂等；代价是「程序改了人工确认过的产物」，
  原文留在 `voice_script_original` 供核对，已在 README「已知限制」里写明

### 测试

新增 `tests/test_audio_script.py`（33 个用例）：念法解析 / 超长截断 / 目标筛选 / 写回与标记 /
失败降级 / 记账口径 / 第二轮不重复花钱 / force 重跑 / 文案变更触发重合成 / 真实抽帧（需要 ffmpeg）/
设置来源 / API 参数与 422。`FakeLLM` 增加配音文案阶段（含「收到几张图」的计数），
`conftest` 默认把文案模式设成 `off`，本地 `.env` 打开也不会污染用例。

### 真实验证（2 次 OpenRouter 视觉调用，合计 ≈$0.0019）

用之前那次真实 H3 片段（`scene_1_shot_1.mp4`，1440×2560）抽 1 帧，`google/gemini-2.5-flash`：

| 场景 | 输入 | 结果 | 花费 |
|------|------|------|------|
| `polish`：镜头已有台词 | 抽帧 + 分镜 + 项目信息（2479 in / 91 out） | 模型认为「原台词已经很口语，无需修改」→ 返回空 `text`，**原文一字未动**（`留空 = 保持原文` 生效） | $0.000971 |
| `narration`：镜头**清空**了 `visual_description` 与 `mood` | 抽帧 + 只有镜头号/时长（2453 in / 91 out） | 生成「她翻阅着杂志，眼神中带着一丝期待。」，note 写明「画面显示手在翻杂志」 | $0.000963 |

第二条是关键证据：文字侧没有任何画面信息，模型仍准确说出了画面内容 → **抽帧真的作为图片发出去了**
（多模态结构与 schema 解析均通过真实链路）。抽帧文件（480×854 / 640 宽 JPEG）也确认是真实画面。

**成本量级**：一次按场调用（1 张图 + 约 1.5k 提示 token）≈ **$0.001**，一个 15 镜 / 4 场的项目
跑一轮 `narration` ≈ $0.004 —— 相比视频按秒计费可以忽略。真正花钱的是"是否 force 重跑"。

## v0.10 — 成本前置（preflight）+ 短镜头合并 + 一个预算被绕过的真 bug

### 背景：点一下「生成视频片段」其实要 $9.75

用户项目里 15 个镜头共 45 秒，切到真实模型后按钮写着「生成缺失片段」，看起来是补几个，
实际（因为 provider 变更会作废旧片段）是**全部 15 个重跑**；而 H3 最短 5 秒/镜头，
计费 15×5=75 秒 × $0.13 = **$9.75**。用户当时设了 `BUDGET_USD=12` —— 差一点就刚好放行。

### 修掉的 bug：预算门控被「换 provider」绕过

`start_video_generation` 用 `_pending_shots()`（只跳过已就绪片段）估预算，
而真正下单的 `video_service.generate_clips()` 会先判 `_provider_switched()` 把旧片段全部
重置为待生成。两者口径不一致，于是：

```
旧清单 provider=mock，已就绪 15 个
新 provider=openrouter
预算门控：待生成 0 个 → 预估 $0.0000 → 放行
实际执行：全部 15 个重跑      → 花掉 $9.75
```

修法：把「provider 是否变了」抽成唯一事实来源 `video_service.provider_switched()`，
预算门控、preflight、真正下单三处共用；`_estimate_video_usd` 与 `_guard_budget` 都按
「会重跑就按全部镜头算」。回归用例 `test_budget_gate_blocks_full_regeneration_after_provider_switch`
就是钉这个场景（修之前它会失败：预估 $0 直接放行）。

### 新增：成本前置（`services/preflight_service.py`）

`GET /api/projects/{id}/preflight?action=all|video|audio|compose` 返回动手前的账单：

- **计费口径**：`planned_seconds()` 按模型档位算（3 秒 → 5 秒），单价优先问 provider
  （`price_per_second()`，读 `pricing_skus`）再用配置；`price_source` 标明来源
- **重跑判定**：`regenerate_all` + `regenerate_reason`（`mock → openrouter`），UI 据此改按钮文案
- **预算投影**：已花 / 本次预估 / 本次后剩余 / 是否超 / 还差多少（`BUDGET_USD=0` 视为不限）
- **省钱建议**：`merge_plan`（合并后镜头数、计费秒数、可省 USD、是否值得做）
- **多档位差异提示**：`模型最短 5 秒/镜头：分镜 45 秒实际要买 75 秒，多付 30 秒 ≈ $3.90`
- provider 建不起来（没 key / 没依赖）**也不报错**：用 `UnavailableVideoProvider` 退回配置单价 + 告警，
  因为「看一眼要花多少」本身不该失败
- 配音侧一并算上 LLM 文案的 token 预估（`narration/polish/full` 才有）

### 新增：短镜头合并（`storyboard_service.merge_shots`）

`POST /api/projects/{id}/storyboard/merge-shots`（默认 `dry_run=true` 只预览）：

- 规则（只看「对白会不会丢」）：没有对白的镜头可被吸收；主镜头一个字都没有时对白可以接管；
  **同一说话人**的连续两句可以并成一条配音（会自动补句号）；主镜头已有旁白或说话人不同则不行
  —— 一条配音只能有一个声音。只合并**连续**镜头（叙事顺序不乱）；凑够 `min_sec` 就停，`max_sec` 封顶
- 主镜头保留原 id（旧片段/配音的对齐关系不炸）；被吸收镜头的画面描述追加过去；
  主镜头本来没文案时**把旁白接过来**，已有文案时逐条记进 `dropped_lines`（对白一条不丢）
- 写盘后重新编号 `shot_no`、清掉 `duration_normalized/audio_aligned`，下游媒体全部作废，
  项目状态回到 `STORYBOARD_CONFIRMED`
- **已有可用片段时默认拒绝**（合并会让付费片段作废），要 `force=true` —— UI 会把这句话写在确认框里

### UI

- 视频面板常驻一行：「本次生成 N/M 个片段 · 计费 Xs × $Y/s ≈ $Z · 预算剩余 …」，
  并在换模型时提示「已就绪的 N 个片段要全部重跑」；省钱建议也在这行
- 点「生成视频片段」→ 先拉一次 preflight → `confirm()` 列出数字（**免费/mock 且不需要重跑时不弹**，不烦人）；
  超预算直接不提交并说明还差多少
- 按钮文案按 preflight 变：`重新生成全部 15 个片段` / `生成缺失片段` / `重试 N 个失败镜头`
- 分镜面板加「合并短镜头（省钱）」：先预览 → 确认框列出 `15 → 5 个 / 45s → 45s / 省 ≈ $3.90`，
  有付费片段时明确写「会让它们作废」
- 成本面板加「下一次操作预估」块（视频含档位单价、配音含 LLM 文案、合成 $0）

### 测试

新增 `tests/test_preflight.py`（24 例）：档位计费、换 provider 全量重跑、只算缺失片段、
provider 不可用降级、预算投影（超支/不限）、配音 LLM 文案单算、合并的 6 条安全规则、
pipeline 预览→应用→force 流程，以及**预算绕过 bug 的回归用例**；
`test_video_openrouter.py` 补 `duration_options()`（含未知模型返回空）；
`FakeVideoProvider` 支持模拟「按秒单价 + 时长档位」。全套 **349 passed**，ruff clean。

### 真实项目实测（只读调用真实 provider 的模型能力表，零费用）

对用户现有项目（`e8e40d3d9d29`，15 镜 / 45 秒 / 15 个 mock 片段）跑 preflight：

```
project_status: video_ready
video: provider=openrouter model=minimax/hailuo-3 price=$0.13/s(provider) min=5.0s
  镜头 15 个 → 本次生成 15 个 | 分镜 45s → 计费 75.0s | 预估 $9.7500
  regenerate_all: True  (mock → openrouter，mock → minimax/hailuo-3)
  warnings: ['模型最短 5 秒/镜头：分镜 45 秒实际要买 75 秒，多付 30 秒 ≈ $3.9000',
             'provider / 模型变了（mock → openrouter），已就绪的 15 个片段会全部重新生成']
audio: script_mode=narration → 1 个镜头 / 1 帧 ≈ $0.0006（edge-tts 本身免费）
budget: 预算 $12 · 已花 $0 · 本次后剩余 ≈ $2.25 · 不够则直接不提交
merge: 15 → 12 个镜头 · 75s → 64s 计费 · 省 ≈ $1.43 · dropped_lines: []
```

这组数字正是用户点下按钮前最需要知道的东西：**按钮写着「生成缺失片段」，实际是全部 15 个重跑**。

## v0.11 — 分镜阶段就「少而长」（视频档位感知）

### 背景

v0.10 把「3 秒镜头也买 5 秒」的浪费算给了用户看，并提供事后合并。但事后合并只能补救、
而且受「一条配音一个声音」的限制（不同说话人的镜头合不动）。真正省钱的写法是**一开始就别拆那么碎**：
用户现有项目 15 镜 × 3 秒 = 计费 75 秒 = $9.75；若按 5 场各 1 镜 × 9 秒 = 45 秒 = **$5.85**。

### 新增

| 能力 | 实现 | 位置 |
|------|------|------|
| 档位解析 | `Pipeline._duration_tier()`：读 `provider.duration_options()`（H3 = 5~15s），拿不到就退回 `VIDEO_MIN_SHOT_SEC`；`min<=1` 视为无档位 | `orchestrator/pipeline.py` |
| 镜头数规划 | `plan_shot_count(场时长, min, max, target)`：每个镜头都 ≥min、≤max，并贴近目标时长（默认 min×1.6 ≈ 8s） | `utils/duration.py` |
| 时长分配 | `fit_durations(n, 总时长, min, max)`：不足 `n×min` 时**宁可让本场变长**（反正也是花 min 的钱） | 同上 |
| 提示词硬约束 | `duration_constraints()`：把「本场只拆 N 个 / 每个 5~15 秒 / 加起来正好 X 秒」写进用户消息；`planned=1` 时额外提示「用连续动作+缓慢运镜，别写画面切换」 | `services/storyboard_service.py` |
| 确定性兜底 | `apply_duration_tier()`：超编镜头按「不丢对白」规则合并（复用 v0.10 的 `_can_absorb`）→ 再 `fit_durations` 重分配 → 重新编号 `shot_no` | 同上 |
| 落盘与展示 | `Storyboard.duration_tier` / `tier_warnings`；UI 分镜面板显示「视频档位 5~15s · 本片 N 镜 · 计费 M 秒 ≈ $X · ⚠️ 告警」 | `models/storyboard.py` / `static/index.html` |

### 设计要点

- **合不动就不合**：四镜各 5 秒、说话人各不相同时，计划可能是 3 镜，但合并会丢谁的声音 ——
  于是保留 4 镜并写 `tier_warnings`。省钱永远让位于「不丢台词」
- **场景可能变长**：3 秒的场次按最短 5 秒排（`fit_durations` 抬到 min），总时长因此 +2 秒；
  这不是 bug 而是「反正也要花 5 秒」的正确写法，会写进 `tier_warnings`
- **默认行为不变**：mock provider 没有档位（`duration_options()` 返回空）→ `_duration_tier()` 返回
  `None` → 完全走原来的「2-5 镜 + `normalize_storyboard_durations`」。用户的 `.env` 是
  `VIDEO_PROVIDER=openrouter`，所以真实链路会自动启用
- 想关掉：`VIDEO_MIN_SHOT_SEC=1`（或把 `VIDEO_SHOT_TARGET_SEC` 调大以换取更长的镜头）

### 预期效果（按模型档位 5~15s）

```
旧：15 镜 × 3 秒 = 45 秒分镜 → 计费 75 秒 → $9.75
新： 5 镜 × 9 秒 = 45 秒分镜 → 计费 45 秒 → $5.85   （省 $3.90，档位浪费归零）
```

### 测试

`tests/test_preflight.py` 扩到 34 例：`duration_constraints()` 文案、`plan_shot_count`/`fit_durations`
的档位数学、按档位塌缩镜头且保留对白、合不动时保留并告警、场次过短抬到 min 并告警、
以及「有档位 → 5 镜 / 无档位 → 10 镜（原行为不变）」两条 pipeline 级用例。全套 **359 passed**，ruff clean。

## v0.12 — 修掉「花了 $9.75，账本只记 $0.0011」的漏记账 bug

### 现场

用户跑完真实链路（15 个 H3 片段 + Edge-TTS + FFmpeg 成片），成本面板只显示 **$0.00112**
（两条 audio + 一条 compose），而视频片段上每条都写着 `cost_usd: 0.65` —— 实花 **$9.75**。

### 根因：用「和上次清单做集合差」判断哪些是新生成的花费

```python
before = {c.shot_id for c in existing.ready_clips}          # 先记下旧片段 id
manifest = await video_service.generate_clips(...)          # 内部：换 provider → 旧片段全部重置
newly = [c for c in manifest.ready_clips if c.shot_id not in before]   # ← 空集合！
```

真实场景恰好命中：项目里已有 15 个 **mock** 片段 → 切到 `openrouter` → `generate_clips`
判定「provider 变了」把它们全部重置再重跑。**重跑后的 shot_id 和旧的一模一样**，
于是「新生成」的差集为空，`cost_service.record()` 一次都没被调用 —— $9.75 静默消失。
（v0.10 修预算门控时只统一了「预估」口径，漏了「记账」口径。）

音频阶段有同一类问题：文案被 LLM 改写后重新合成，clip id 不变 → 同样漏记（paid TTS 会少算钱）。

### 修法：按条目上的「已记账」标记，而不是集合差

- `VideoClip.cost_recorded` / `AudioClip.cost_recorded`：这条的花费是否已入账
- 重新生成/重新合成时**清掉标记**（`video_service._generate_one`、provider 变更重置、
  `audio_service.synthesize_all` 的 PENDING 分支）—— 重跑就是重新计费
- 记账后**打标记并落盘**（顺序很关键：先记账再 save，否则标记丢失会重复记账）
- 这样同时解决三件事：换 provider 全量重跑、文案改写后重合成、进程在「产物已落盘、
  账本还没写」时挂掉 —— 下次续跑只补记账，不会重复计费

### 顺带修掉：空跑会删成片

原来 `_run_video_job` 结束时**无条件** `purge_media(compose=True)`。于是「重跑视频但一个片段
都没重生成」这种空跑会把已经渲染好的成片删掉。现在只有**真的重跑过片段**才作废成片 ——
判断依据是 `VideoClip.generated_at`（每次成功生成打时间戳；`task_id` 在假 provider /
部分真实 provider 上是确定性的，不能当唯一依据）。

### 新增：补齐记账（`POST /api/projects/{id}/cost/reconcile`）

给已经被老版本漏记的项目用：读片段上的实测 `cost_usd` 补进账本，
**不调 API、不动媒体、不动项目状态**，幂等。`GET /cost` 现在同时给出：

- `video_actual_usd`：片段实测花费合计（provider 报的 `usage.cost`）
- `video_ledger_usd`：账本里视频阶段记了多少
- `video_unrecorded_usd`：还没入账的部分（> 0 时 UI 显示「漏记 $X」+ 一键补齐按钮）

### 现场修复

对用户项目 `e8e40d3d9d29` 执行补齐：

```
修复前：账本合计 $0.001120 | 视频实测 $9.7500 | 账本内视频 $0.0000 | 漏记 $9.7500
补齐：  记录 $9.75 / 15 个片段
修复后：账本合计 $9.751120 | 预算 $12 → 剩余 $2.2489 | 成片（65.02s，19.1MB）完好
```

### 这次真实跑出来的另外两个事实

1. **成片 65 秒而不是 45 秒**：分镜是 45 秒 / 15 个 2-4 秒的镜头，但 H3 最短 5 秒 →
   每个片段都是 5 秒 → 实际画面比分镜长，成片自然变长（同时为 75 秒付费）。
   v0.11 的「分镜档位感知」正是为这件事做的：按档位重排后是 8 镜 / 45 秒 ≈ $5.85
2. **配音文案（narration）真实生效**：`scene_3_shot_1` 是唯一没台词的镜头，
   抽了 1 帧 → LLM 写出「林悦身着华服，自信地走上T台，聚光灯下她光芒万丈。」，
   成本 $0.00112

### 测试

`tests/test_preflight.py` 新增 6 例：换 provider 重跑必须记账、重跑不重复记账、
空跑保留成片、`reconcile` 幂等补齐、LLM 改写文案后重新合成要重新记账、
`cost_status` 的实测/账本/漏记三个口径。全套 **366 passed**，ruff clean。

## v0.13 — 字幕看得见 + 角色图（AI 定妆照）

> 用户实测反馈的两个问题：**「角色的脸不统一」**、**「没有字幕」**。
> 查下来都是真实原因，不是玄学。

### 问题 1：没有字幕

现场证据：

```
compose_report.json:  subtitle_mode="soft" → effective="soft"，warnings=[]
ffprobe final.mp4:    stream 2 = mov_text / subtitle / language=zho   ← 字幕**在**片子里
output/subtitles.srt: 9 条 cue，时间轴与画面逐镜对齐                   ← 也在
```

**根因**：`COMPOSE_SUBTITLE_MODE` 没配 → 默认 `soft` → 字幕是 mp4 里的 mov_text **软字幕轨**。
Chrome/Safari 的 `<video>` 根本不渲染 mov_text，Web 面板也没给播放器挂 `<track>`，
所以在页面上 / 手机里「就是没有字幕」。想烧字幕又被第二件事挡住：

```
FFMPEG_PATH=ffmpeg → /opt/homebrew/bin/ffmpeg 9.0.1
build 里没有 --enable-libass → -filters 查不到 subtitles/ass/drawtext
→ /health: ffmpeg_can_burn_subtitles = false → 选 burn 会静默降级回 soft
```

**修法（两条都做了）**：

| 改动 | 位置 |
|------|------|
| `resolve_ffmpeg_for(required)`：按**所需能力**在候选里挑 exe（主 ffmpeg 不会烧就换 imageio-ffmpeg 便携版，它有 libass），找不到才降级并告警；`FFMPEG_AUTO_UPGRADE=false` 可关 | `utils/ffmpeg.py` |
| `run_ffmpeg(..., exe=)` / `run_ffmpeg_capture(..., exe=)`：能指定非主 ffmpeg | 同上 |
| 合成前算出「真正要执行的 exe」并用它的能力判降级；新增报告字段 `subtitle_ffmpeg` | `services/compose_service.py` |
| 三种模式都额外导出 `output/subtitles.vtt`（浏览器只认 VTT） | `services/subtitle_service.py`（`build_vtt` / `srt_to_vtt`） |
| `GET /subtitles.vtt`：老项目没有 vtt 文件时**就地由 srt 转换**，不用重新合成 | `main.py` |
| 成片播放器挂 `<track>` + 「预览字幕」开关；字幕模式下拉标注当前能不能烧 | `static/index.html` |
| `/health` 增加 `ffmpeg_burn_path` / `ffmpeg_auto_upgrade` | `main.py` |

真实重合成验证（用户项目 `e8e40d3d9d29`，本地 ffmpeg，**零 API 花费**）：

```
subtitle_mode=burn → effective=burn
subtitle_ffmpeg=.../imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1
warnings=["主 ffmpeg 没有 libass，烧字幕改用：...v7.1"]
ffprobe: 只剩 h264 + aac（烧进去了，不再有 mov_text 流）
抽帧 @2.0s / @16.5s：画面底部可见「李薇儿：哎呀，小悦…」「张姐：抬头！挺胸！…」
```

### 问题 2：角色的脸不统一

现场证据（同一个人物「林悦」在不同镜头）：

| 镜头 | 画面 |
|------|------|
| `scene_1_shot_3` | 棕色齐肩发、灰绿上衣、圆脸 |
| `scene_2_shot_3` | 黑色高马尾、运动装、脸型不同 |

**根因链**（查完的完整调用路径）：

1. `story_bible.json` 的 4 个角色**都没有 `reference_image`**，项目里 `refs/` 目录压根不存在
2. → `video_service.resolve_reference()` 返回 `(None, None)`
3. → `openrouter._reference_payload()` 直接 `return {}`：payload 里**既没有 `input_references`
   也没有 `frame_images`**
4. → 15 个镜头全是**纯文生视频**，每个镜头独立抽一次脸

叠加两个次要原因：

- **seed 对 H3 是死的**：capabilities 里 `seed: false`，带上会 400 —— 唯一锚点只剩参考图
- **`build_prompt(shot, project)` 根本不读 bible**：只有「分镜 LLM 恰好把
  `character.visual_prompt` 抄进 `video_prompt`」的镜头才有外貌描述
  （`scene_1_shot_2` 抄了，`scene_1_shot_3` 只有一句 "a young woman's face"）

**修法：文本锚点（免费）+ AI 定妆照（$0.04/张）**

先补不花钱的那一半：`build_prompt(shot, project, bible)` 现在会读圣经，把在场角色
**没被提到过的** `visual_prompt` 确定性补进每个镜头的 prompt（分镜 LLM 已经抄过就跳过，
不重复），并追加 `same characters as before, consistent face and hairstyle`。
定妆照 prompt 组装也顺手截掉人设里的「人物弧光」（`Later, she transforms into…`）——
定妆照要的是一张稳定的脸，不是中间态。

**再做 AI 生成角色定妆照的完整链路**

| 改动 | 位置 |
|------|------|
| `ImageProvider` 抽象（`generate(ImageRequest) -> GeneratedImage`，失败/产物错误分开，产物错误不重试=不重复买图） | `providers/image/base.py` |
| OpenRouter 图片接口：`POST /api/v1/images`（`n/aspect_ratio/resolution/input_references`），读 `usage.cost`，全有或全无计费 → 失败重试安全 | `providers/image/openrouter.py` |
| mock：纯 Python 手写 PNG（不花钱、不依赖 ffmpeg，颜色按 subject 哈希稳定） | `providers/image/mock.py` |
| 角色图服务：定妆照 prompt 组装（正面/中性表情/干净背景/无文字）+ 落盘 `refs/<cid>.png` + 写回圣经 + 清理旧后缀 | `services/character_image_service.py` |
| 批量补齐（默认只补缺失）、`reference_plan` 只算不写、预算不足 402 | 同上 + `main.py` |
| 记账新增 `CostStage.IMAGE`（按张数 + 实报 cost） | `models/cost.py` / `services/cost_service.py` |
| UI：角色卡片缩略图 + 「AI 生成 / AI 重画」+ 「AI 补齐缺失的角色图」+ 缺几张/预估花费提示 | `static/index.html` |
| 文本锚点：`build_prompt` 读 bible 补在场角色外貌（免费，没参考图时也能减少漂移） | `services/video_service.py` |

顺手修掉的**老问题**：`resolve_ffmpeg()` 之前把「配置了但不存在」当「没配置」处理是对的，
但现在候选列表里会保留 `FFMPEG_PATH` → `PATH` → 便携版的完整顺序，能力探测按 exe 分别缓存。

### 真实生成 4 张角色图时暴露的两个真 bug（都修了）

**① 并发批量写坏圣经（丢参考图登记）**

第一次真实批量生成（`concurrency=2`，4 个角色）：4 张图**都**生成成功、文件都在
`refs/`，但圣经里只剩 2 个角色 —— 每个任务各自 `load_bible`（生成前）→ 改自己那条 →
`save_bible`（整份覆盖），后写的把先写的覆盖掉了。

```
修复前：char_1 林悦:（空）  char_2 李薇儿: refs/char_2.jpg
        char_3 张姐:（空）  char_4 赵总:  refs/char_4.jpg
```

修法：`_write_reference()` 在**按项目串行化**的锁内**重新读一遍圣经**再改自己那一个角色
（关键不是锁本身，而是「不要把生成前读到的旧快照写回去」）。

复现-验证：`tests/test_character_image.py::test_concurrent_batch_does_not_lose_bible_updates`
把假 provider 加上 `delay_sec` 让任务真的交错。把 writer 换回旧写法实测：
**旧写法 4 张只留下 1 条登记，新写法 4/4**。

顺手加了「孤儿图」处理：`refs/<cid>.*` 有文件但圣经没登记时，
`reference_plan` 会把它列进 `orphans`，单个/批量生成都会**免费补登记**而不是重新花钱生成
（本次就是这么把已经付过钱的 2 张找回来的，0 额外花费）。

**② 图片模型把 prompt 里的中文画进了画面**

prompt 里带着中文名 `林悦` 和风格 `都市复仇`，模型把「都市复仇（城商设畏）」当海报标题、
把「林悦 20岁」当标注印在了图上 —— 这种带文字的图当视频参考图，文字很容易被带进画面。

修法：定妆照 prompt 现在**一个中文字符都不许有**（名字 / 中文 `role` / `style.genre`
全部不进 prompt，`role` 映射成 protagonist / antagonist / …），并且明确写
`no text, no typography, no letters, ... no poster layout, no collage`，
同时**删掉**原来的 `Cinematic character design sheet` —— "sheet / poster / design"
这类词本身就会把画面带成带排版的海报。

**③ 定妆照的「人种漂移」（观察，未改数据）**

4 张干净版定妆照里，林悦是东亚面孔，张姐 / 赵总却是欧美面孔 —— 圣经的
`visual_prompt` 只写了「sophisticated woman in her late thirties」这类描述，**没写人种**，
模型就各按各的理解画。这不是代码 bug（prompt 忠实反映了圣经），
但会让同一部戏的演员表看起来不像一家人。两个可用的修法：

- 在故事圣经里给每个角色补上人种/地域描述（数据侧，最准）
- 或者用新增的 `IMAGE_PROMPT_SUFFIX`（英文，例如 `East Asian cast, Chinese television drama`）
  统一整组基调（配置侧，一次覆盖所有角色）

### 实测验证：参考图真的锁住了脸

把 3 个含林悦正脸的镜头（`scene_1_shot_3` / `scene_2_shot_3` / `scene_4_shot_4`）
重置为待生成后重跑（**唯一变量**就是这次带上了 `input_references`）：

```
scene_1_shot_3: 旧=棕色齐肩发圆脸 → 新=黑短发，与定妆照同一张脸（哭戏）
scene_2_shot_3: 旧=黑色高马尾不同脸型 → 新=同一张脸（坚毅表情）
scene_4_shot_4: 旧=另一个人 → 新=同一张脸 + 后期造型（桂冠/妆容），人物弧光成立
花费：3 × 5s × $0.13 = $1.9500（preflight 预估 $1.95，完全一致）
账本：video 阶段 $9.75 → $11.70（calls 15 → 18），总计 $12.02 / 预算 $20
```

服务日志里**没有**「带参考图提交被拒」的降级告警 → `input_references` 是被接受的；
另外 H3 元数据里的 `reference_images` SKU（$0.04/镜头）这次**没有实际计费**
（每镜头实收仍是 $0.65）。对比图见 `projects/<id>/consistency_before.png` / `_after.png`。

顺带修掉一个 prompt 脏数据：分镜 LLM 把标签也写进了 `video_prompt`
（真实样本 `English prompt: A confident…`），`build_prompt` 现在会剥掉
`English prompt:` / `Prompt:` / `英文提示词：` 这类前缀。

### 全量重跑：整片 15/15（含「一镜多参考图」）

用户确认「主角前后仍然跳脸」后，把剩下 12 个旧片段也重跑，并把双人镜头升级成
**一镜多图**（`VideoRequest.reference_images: list[Path]`，画面里每个配了参考图的角色各带一张；
模型 400 时两级降级：多张 → 第一张 → 不带）：

```
主角林悦（13 个镜头）：素颜便装 → 训练 → T 台 → 桂冠，全片同一张脸 ✓
双人镜头（s1_2/s4_1/s4_3）：主角稳定，第二个角色改善有限（多图权重问题，见 README 限制 17）
花费：整轮 $10.27（3 镜验证 $1.95 + 12 镜 $8.32）；账本 video $20.02，漏记 $0.00
```

过程中真实撞到一次**本机 DNS 抖动**（`ConnectError: nodename nor servname provided`）：
4 个片段提交失败（重试 3 次仍失败），但**失败不计费**（片段 cost=$0）；
`compose` 随后以 400「视频片段不完整」正确拒绝合成，补跑这 4 个（$3.12）后 15/15 才出片。
这正是「宁可拒绝也不产缺料成片」的设计意图被真实检验了一次。

另外差点超支一次：12 个镜头的 preflight 报价是 **$8.32**（不是按 5 秒粗算的 $7.80 ——
其中两个镜头是 7 秒），而当时剩余 $7.9789，**预算门控直接拒绝**，用户把 `BUDGET_USD`
提到 30 后才跑。这类「差 $0.34」的边界正是门控存在的意义。

### 测试

新增 `tests/test_character_image.py`（23）、`tests/test_image_openrouter.py`（11）、
`tests/test_ffmpeg_selection.py`（5），并在 `test_compose.py` 补 4 例 VTT/升级路径、
`test_api.py` 补 5 例（plan / 单个 / 批量 / vtt / health）、`test_video.py` 补 3 例文本锚点。
全套 **421 passed**，ruff clean。

顺带修掉一个测试基线漏洞：`.env` 改成 `COMPOSE_SUBTITLE_MODE=burn` 后，
`test_compose.py` 里两个默认 soft 的用例开始失败（真实行为变了）——
`conftest` 现在强制 `compose_subtitle_mode=soft` / `compose_ambient_mode=off`，
需要烧字幕的用例自己传 `overrides`（和当初 `VIDEO_PROVIDER` 漏进测试是同一类问题）。

关键回归用例：

- `test_burn_upgrades_to_a_libass_capable_ffmpeg`：主 ffmpeg 假装没 libass → 真的用便携版烧了一遍
  → 断言成片里**不再有** subtitle 流
- `test_burn_degrades_when_libass_is_missing`：**任何**候选都没有 libass → 降级 soft + 告警
- `test_batch_isolation`：一个角色生成失败，其它角色照样拿到图

## v0.14 — 配音符合人物 + 表情自然/动作流畅 + 剧本严谨

> 用户提的四条质量要求：**配音要符合人物、每个人物有自己的声音**、**表情自然**、
> **动作流畅**、**故事/剧本严谨**。

### 1. 配音：4 个角色只用到 2 个音色，女反派被配了男声

真实产物（`audio_manifest.json`）查出来的现状：

```
林悦(20岁女主)   -> zh-CN-XiaoxiaoNeural
张姐(38岁导师)   -> zh-CN-XiaoxiaoNeural   ← 和女主同一个声音
李薇儿(22岁女反派) -> zh-CN-YunxiNeural      ← 女角配了男声！
赵总(45岁男)     -> zh-CN-YunxiNeural       ← 和上面撞车
```

根因：`_MALE_HINTS` 里混进了 **「反派」这种身份词**，而性别判定又是「先判男声」——
李薇儿 `role=反派` 直接命中男声分支（她的 `visual_prompt` 里 "young woman" 其实也命中了女声词，
但排在后面）。加上 `_fallback_voices` 只有 3 个配置值，撞音色是必然。

修法：新增 `services/voice_catalog.py`

- Edge zh 音色全表（14 个，含性别/年龄区间/音色性格/方言标记，元数据来自 `edge_tts.list_voices()`）
- 性别判定改成**加权计分**：显式性别词权重 2、亲属称谓权重 1，**身份词不参与**；
  打平才算「不知道」
- 选角打分：性别 → 角色定位 → 年龄 → 口音代价；**先在同性别空闲音色里挑**
- 池子用尽 → 复用最合适的音色 + `voice_delivery`（音高/语速）区分 + 留下可读说明
- **年龄明显不匹配且有空闲更合适音色时重配**（45 岁主编不能挂「清朗少年感」）
- 选中的音色写回 `Character.voice_id`，重跑不换人；人工写死的 `voice_id` 优先
- 新增 `GET /api/projects/{id}/voices`（**不花钱的选角预览**）与
  `POST /api/projects/{id}/audio/reopen`（成片后换音色重做配音；原来状态机会拒绝）

真实项目重配结果（Edge TTS 免费，15/15 成功）：

```
林悦   zh-CN-XiaoxiaoNeural（温润知性）
李薇儿 zh-CN-XiaoyiNeural（清亮张扬）    ← 从男声纠正为女声
张姐   zh-CN-XiaoxiaoNeural + pitch -12Hz / rate -6%  ← 池子只有 2 个通用女声
赵总   zh-CN-YunyangNeural（沉稳权威）   ← 从「少年感」纠正
旁白   zh-CN-YunjianNeural
```

试听样音落盘在 `projects/<id>/voice_sampler.mp3`（一人一句，18.4s）。

### 2. 表情自然 / 动作流畅：prompt 里丢了两个字段

| 分镜字段 | 原来的命运 |
|---|---|
| `camera`（"近景 · 推"） | **根本没进 prompt** —— 运镜信息全丢，模型只能自己猜 |
| `mood`（"委屈，不甘"） | 中文直接拼进英文 prompt，模型基本读不懂（还有被画进画面的风险） |

修法：`build_prompt` 新增 ——

- `camera_directive()`：景别（大特写…远景/过肩）× 运镜（固定/推/拉/摇/移/跟/升降/手持）→ 英文
- `mood_directive()`：中文情绪词映射表（30+ 词）；**没收录的中文词直接丢弃**，绝不塞进英文 prompt
- `VIDEO_PERFORMANCE_DIRECTIVE`（可配）：克制的微表情、自然眨眼呼吸、平滑连续运动、稳定构图、
  单镜到底不切、不许变形、避免夸张舞台腔 —— 追加到**每个**镜头
- `VIDEO_STYLE_PROMPT`（可配）：英文风格约束
- 分镜提示词同步收紧：情绪镜头优先中景/近景、一镜只写一个连续动作、表演用克制词
- 顺带修掉 `English prompt:` 标签泄漏的**根因**：分镜 schema 示例里就写着
  `"video_prompt": "English prompt: ..."`，LLM 照抄 —— 示例已改，`build_prompt` 仍保留防御性剥离

### 3. 剧本严谨：能确定性修的就别让人看

`repair_storyboard()` 在分镜生成后、校验前跑：**说话人不在画面里 → 补进
`character_in_frame`**（真实项目 15 个镜头里 3 个中招）。这一条同时修三件事：参考图、
外貌锚点、穿帮。需要创作者决策的问题（缺 video_prompt、悬念未闭合）照旧报给人。

### 测试

新增 `tests/test_voice_catalog.py`（14 例：性别判定不被身份词带偏、每人不同音色、
复用+念法、性别冲突重配、年龄不匹配重配、音色池空则保留、方言默认禁用、确定性、
未知说话人兜底），`test_video.py` 补 3 例（camera/mood 英文化、表演规范、未收录词丢弃），
`test_consistency.py` 补 2 例（修复 presence、不猜未知说话人）。全套 **440 passed**，ruff clean。

### 4. 剧本严谨：严格模式拦确认 + LLM 剧本医生

第一层是确定性检查（不花钱）：

| 检查 | 级别 | 判据 |
|---|---|---|
| 空场（无台词无概述） | ERROR（严格模式） | 机器一定能看出 |
| 相邻两场内容一字不差 | ERROR（严格模式） | 一定是注水，没推进 |
| 结尾仍以悬念问句收尾 | **WARNING（永远）** | 正常台词里问句太多，当证据会误伤 |
| 全剧找不到对抗语汇 | **WARNING（永远）** | 只是「可能没戏」的提示 |

> 设计取舍记一笔：最初把后两条也做成 ERROR，结果 13 个既有用例全被拦下 ——
> 说明它们**不是证据**。现在只有「机器一定能判定」的问题才够格阻止确认。

第二层是 LLM 剧本医生 `script_service.review_structure()`：按钩子 / 目标与阻力 /
转折 / 高潮 / 收束 / 因果链 / 人物动机 逐项审，只报可执行的硬伤（最多 6 条），
`severity=error` 的会阻止确认。评审失败一律降级记 warning；评审的 token 与剧本生成
合并记在同一条 `script` 账里。

确认被拦时：`POST /script/confirm` 返回 400 + 具体问题与建议，body 带
`{"force": true}` 可强制继续（UI 先弹出原因再问你是否强制）。配置：
`SCRIPT_STRICT=true`（严格模式开关）、`SCRIPT_LLM_REVIEW=true`（是否调剧本医生）。

### 测试（v0.14 汇总）

`test_voice_catalog.py` 14 例 + `test_video.py` 3 例 + `test_consistency.py` 6 例
（含严格模式：重复场 ERROR、问句结尾只 WARNING、空场 ERROR、confirm 被拦 + force 放行）。
全套 **444 passed**，ruff clean。

### 表演规范实测（3 个镜头 A/B，$1.95）

用新的表演/运镜规范重跑 `scene_1_shot_3`（哭戏）、`scene_2_shot_2`（含泪不甘）、
`scene_2_shot_4`（训练全景），与旧版逐帧对比（`projects/<id>/expression_before_after.png`）：

| 镜头 | 旧 | 新 |
|---|---|---|
| s1_3 哭戏 | 眉毛挤成一团、张嘴痛哭、面部肌肉扭曲（舞台剧腔） | 眼眶湿润、压抑的悲伤，姿态自然 |
| s2_2 不甘 | 极端特写 + 眼泪 + 表情夸张 | 中景、表情平静（**情绪强度明显被压低**） |
| s2_4 训练 | 姿态略怪、画面偏虚 | 更稳、更实 |

**结论要说清楚：自然度上去了，情绪强度下来了。** 这条 `VIDEO_PERFORMANCE_DIRECTIVE`
（subtle / restrained / avoid exaggerated）确实治住了「舞台剧式表演」，但同一句话也会
把该有的情绪一起压平。它是**可配的**：想保住自然度同时让情绪仍然读得出来，可以改成
`natural restrained acting with clearly readable emotion (no theatrical exaggeration)` 之类，
再跑 2-3 个镜头对比。这是创作取向，不该由代码单方面决定。

## v0.15 — 长篇大纲 + 云端配音 + 背景丰富度 + 口型对齐

> 用户反馈四条：**声音太像 AI**、**声音和口型对不上**、**剧情太简略太平淡（想先写长篇大纲再选章生成）**、
> **背景不够丰富**。外加一次误删修复。

### 1. 剧情太平淡：先写全篇 20 章大纲，再挑章展开

根因不是模型不行，是**流程**：原来是「45 秒短片 → 直接写 4 场 → 开拍」。4 场装不下钩子/转折/高潮/代价，
而且每场孤立生成、没有全篇因果链兜着，写出来自然是套话。

修法（`story_outline.json` + 两个新接口）：

| 阶段 | 做什么 | 为什么这么做 |
|---|---|---|
| 骨架 | 一次调用给全 20 章（标题 + 一句话剧情） | 20 章正文一次写完必被输出长度截断，模型会开始糊弄 |
| 正文 | 每批 3-4 章扩写（300-500 字 + 4-8 个情节点 + 章末钩子 + 场次/时长预算），写完即落盘 | 可分块、可中断续跑，每章都写得细 |
| 展开 | 勾选想拍的章 → 只把这几章展开成「分场大纲」→ 逐场生成剧本 | 视频按秒计费，只对选中的章花钱 |

- 提示词把「因果链 / 阻力逐章升级 / 至少 5 处不可逆大翻转」写成硬要求
- 展开时把**全篇弧光 + 章内情节点 + 章的环境线索**一起送进分镜，避免分镜脱离全篇
- 已经展开过的章不会重复生成（重复点不花钱）；之后续写新章 = 追加，旧场次原样保留
- 项目 `target_duration_sec` 自动对齐到「已展开章的预算之和」

真实跑出来的账（同一句输入「合照被裁的女孩逆袭」）：**20 章 / 总预算 756 秒 / 6 次调用 / $0.0352**，
全程 167 秒。选前 2 章展开 = 6 场 / 72 秒 / $0.0176。按 H3 $0.13/秒算，
**整篇 20 章的视频费约 $98** —— 所以「按章选做」不只是创作自由，也是成本闸门。

### 2. 一个假阳性：只选了前几章，却被判「故事没有落地」

展开 `ch_1 + ch_2` 后，一致性报告立刻报「结尾场次仍以悬念收尾，故事没有落地」——
这是**误判**：悬念正是留给后面章节的。修法是给校验加 `partial` 语义：

- `validate_script(..., partial=True)`：跳过「结尾悬着」这条
- `review_structure(..., partial=True)`：明确告诉剧本医生「这段不是全篇，不要要求本段收束全篇」
- 判定标准：`已展开章数 < 大纲总章数`。全篇展开完之后，这条检查照旧生效

### 3. 声音太像 AI：两个原因，一个是流程、一个是引擎

1. **所有台词的念法都是空的**：`voice_direction` 只在配音文案模式下生成，而默认是 `off` ——
   一整部戏的台词用同一个语速/音高念出来，没有情绪、没有停顿
2. **Edge 的表达力上限**：`rate/pitch` 调不出「忍着哭」「假惺惺的关心」，中文也没有可用的 SSML 风格

修法：接入云端 TTS（`TTS_PROVIDER=openrouter`，`openai/gpt-audio-mini`），念法用**自然语言**描述，
并新增 `AUDIO_LLM_SCRIPT=perform` 模式（**一个字都不改台词**，只按画面写念法）。

真实项目 15 个镜头跑完拿到的念法（LLM 看着抽帧写的）：

```
scene_1_shot_1 李薇儿：带着一丝假惺惺的关心和嘲讽，语速稍慢，语气轻佻，像在看好戏
scene_1_shot_3 林悦  ：声音带着哭腔，语速缓慢，语气委屈而困惑，像在极力压抑情绪，声音略微颤抖
scene_2_shot_1 张姐  ：严厉且带有命令的语气，语速中等偏快，强调「刀」字，不容置疑
```

云端选角还顺手解决了 Edge 的音色池问题：**张姐从「复用女主的音色」变成独立的 coral**
（云端女声 5 个、男声 6 个，比 Edge 的 2+4 宽得多）；换引擎时旧音色名会自动重配
（Edge 名字发给云端接口会 400，反之亦然）。

#### 真实调用踩出来的两个坑（都已修）

1. **音频输出必须 `stream: true`** —— 否则直接 400 `Audio output requires stream: true`。
   改成 SSE 逐块收 base64 再拼接
2. **流式只支持 `pcm16`** —— 400 `audio.format does not support 'mp3' when stream=true`。
   所以 provider 自己用标准库把裸 PCM 封成 WAV（24kHz 单声道），不依赖 ffmpeg

#### 最值得记的一个坑：模型会自己加戏

同一段提示词，换个音色就出事（真实转写）：

```
输入：薇儿，为什么？
输出：薇儿……为什么……难道就这样吗？我……我真的……很……无法理解……
      （语气颤抖，声音渐弱）……为什么……你要离开……（深呼吸）      ← 15.15 秒，还把括号念了出来
```

修法（两道防线）：

- **系统提示词写死**：只念原文、一个字都不许加、不许念括号里的内容、念完立刻停
- **事后用转写文本校验**：转写明显超长 → 带更严格的提示重试一次；仍然超长且时长超过
  「按原文估算 × `TTS_MAX_DURATION_RATIO`（默认 2.0）」→ **直接判失败**，
  宁可不配这一句，也不把 15 秒独白塞进 5 秒的镜头

修完复测同一组样本：15.15s → **1.95s**，10.70s → **4.30s**，转写全部与原文一致。

**成本实测**：15 句台词（约 60 秒语音）provider 实报 **$0.0058**；整片配音 + 念法 LLM 一起 $0.0144。

### 4. 背景只有一面空墙：场景描述从来没进过 prompt

故事圣经里每个场景都有 `visual_prompt`（"A bustling modern office with glass partitions"），
但视频 prompt 里只有分镜写的一句 `The office lighting is harsh and cold.`。

修法三层：

1. `Shot.location`：分镜必须标出场景 id（系统校验真实存在）
2. `location_anchor()`：把该场景的 `visual_prompt` 作为 `setting: …` 注入视频 prompt
   （同一场景的多个镜头背景因此一致；已出现过的描述不重复注入）
3. `VIDEO_BACKGROUND_DIRECTIVE`：每个镜头追加「有层次、有陈设、有材质、有方向光、有景深，
   禁止空墙/纯影棚」

分镜提示词同步加硬：`environment` 必须写到具体可见物，只写 `in an office` 视为不合格；
长篇大纲展开时还把章的环境线索与「这一场在整篇里的位置」一起带进分镜。

### 5. 口型对不上：prompt 里根本没有台词

H3 自带音频生成能力，它会**自己编一段话**在说（嘴按它编的话动）；我们再叠 TTS 配音，
两段话的字都不一样 —— 口型必然对不上。修法是把台词写进 prompt（`VIDEO_PROMPT_DIALOGUE=true`）：

```
林悦 speaks this exact Mandarin line: "薇儿，为什么？" — natural lip-sync matched to the
Mandarin speech, mouth movement and breathing follow the line, no on-screen text or captions
```

三方案实测（同一镜头 `scene_1_shot_3`、同一句话，各 $0.65，共 $1.95）。原生音轨抽出来转写后：

| 方案 | 模型原生说出口的话 | 原生说话区间 | 我们的配音区间 | 结论 |
|---|---|---|---|---|
| A：prompt 无台词 | **「你怎么能这样对我」** | 2.76–3.98s | 0.02–1.34s | 模型编了**另一句话**；嘴 2.7 秒后才动 |
| B：台词进 prompt + TTS | 「为什么」 | 1.22–4.60s | 0.02–1.34s | 话对上了，开头仍错位 **1.2 秒** |
| C：台词进 prompt + 原生语音 | 「薇儿，为什么」 | 0.98–3.72s | 同一轨 | 零错位，但音色由模型每镜头决定 |

A 的转写是决定性证据：不写台词，模型会自己编一段话说。C 口型最好，但与「每个角色有自己的声音」冲突。

**所以最终落地的是「B + 本地口型对齐」**：`utils/audio_align.py` 找出画面里的人几秒开口
（用相对自身峰值的能量阈值，而不是 ffmpeg `silencedetect` 的绝对 dB —— 实测片头底噪 -45dB，
绝对阈值会把 A 的 2.76s 误判成 0），在合成时用 `adelay` 把配音**延后**到那一刻。
不重烧视频、不换音色、不动音频内容。上限 `COMPOSE_MOUTH_ALIGN_MAX_SEC=1.5s`，
且必须满足「起点 + 配音时长 ≤ 片段时长」，尾音会被切掉时宁可不对齐。

全片 15 镜重合成结果：**10/15 需要对齐，延后 0.08~1.20s**，成片仍 67.0 秒（没有尾音被切）；
报告里写明 `口型对齐：10 个镜头的配音延后了 0.08~1.20s`。

局限：这是**起点对齐**，不是逐帧对齐 —— 一句话内部的节奏仍由 TTS 决定。
样本见 `projects/e8e40d3d9d29/ab_lipsync/`（`lipsync_compare.mp4` + 三份原始片段 + 测量 JSON + README）。

### 6. 误删的 `char_1.jpg`：从成片片段里把同一张脸找回来

用户误删了主角定妆照且没有备份。但**所有片段都是从这张图生成的**，所以片段里的脸与它同源：

```bash
ffmpeg -y -ss 3.0 -i projects/<id>/clips/scene_1_shot_4.mp4 \
  -frames:v 1 -vf crop=1200:1200:110:120,scale=1024:1024 -q:v 2 \
  projects/<id>/refs/char_1.jpg
```

选帧标准：正脸、表情中立、光线均匀、背景简单。找回后用 `adopt_orphan_reference()` 免费补登记
（**$0**，不需要重新生成），把来源与复现命令写进 `refs/README.md`。
注意：重新生成的定妆照与现有片段里的脸**不是同一张脸**，除非决定整片重跑，否则优先用找回的这张。

### 测试

新增 `tests/test_story_outline.py`（12 例：骨架+分批扩写、续跑不重复花钱、force 重出保留旧正文、
只展开选中章、追加语义、无效章报错、partial 判定、老路径不受影响）与
`tests/test_tts_openrouter.py`（16 例：流式请求体、instruction 进系统提示、PCM 封 WAV、
加戏检测、严格重试、时长守卫失败、云端选角/换引擎重配）。`test_api.py` 补 4 例
（voices_cloud、长篇大纲流程、draft 权限、perform 模式校验），`test_video.py` 补 3 例
（台词进 prompt、场景锚点注入、已描述不重复）。全套 **472 passed**，ruff clean。

## v0.16 · 逐帧口型 + 画面运动进念法 + 音画同步自检

用户报的两件事：`lipsync_compare.mp4` **为什么语音和视频是分开的**，以及
「继续实现逐帧级口型和运动信息进配音文案」。

### 0. 先修事故：对比片音画漂移 7.154 秒

| | 视频 | 音频 | 差 |
|---|---|---|---|
| 旧 `lipsync_compare.mp4` | 41.833s | 34.679s | **-7.154s** |
| 新（重拼 + 自检） | 51.250s | 51.257s | +0.007s |

根因：每个部件的音视频时长不等（说明卡视频 9.333s / 音频 7.048s），
再用 `-f concat -c copy` 拼接 —— **mp4 的音轨表达不了空洞**，这些空洞被丢掉，
后面所有片段的音频整体提前。听感就是「画面在演一帧，声音在说另一句」。
逐窗核对（8/8 个部件的画面窗口里都有它自己的声音）后确认修好。

修法写成了工具 `tools/lipsync/build_compare.py`：每个部件先算精确时长，
画面 `-t` 截准、音频 `apad,atrim` 补/截到同一长度，再用 **concat 滤镜**拼接，
最后用 `utils/av_sync.py` 复验。成片合成分支也加了同一项自检（差 >0.20s 报警）。

### 1. 逐帧口型（`LIPSYNC_ENGINE=wav2lip-onnx`）

| 能力 | 实现 | 位置 |
|------|------|------|
| Wav2Lip ONNX 推理 | 不需要 torch：onnxruntime + numpy + opencv（约 120MB），独立环境 `.venv-lipsync` | `tools/lipsync/wav2lip_onnx.py` |
| 音频前端 | 自实现 librosa 的 mel（纯 numpy），与真 librosa 逐格差 **8.9e-08** | `tools/lipsync/wav2lip_mel.py` + `check_mel.py` |
| 可插拔引擎 | `wav2lip-onnx`（内置）/ `cli`（LatentSync、MuseTalk…填 `{video} {audio} {out}` 模板） | `services/lipsync_service.py` |
| 镜头筛选 | 默认只做「有台词且说话人在画面里」的镜头（旁白镜头不该动嘴） | `lipsync_service.shot_eligible` |
| 与起点对齐互斥 | 做过逐帧口型的镜头不再整体延后配音，否则嘴和声音错开 | `compose_service._render_all` |
| 画质 | 只贴模型真正改过的嘴唇（皮肤保持原分辨率）+ 嘴唇核心区必贴 + 高频细节叠回 | `tools/lipsync/wav2lip_onnx.py` |
| 诊断 | `output/lipsync_report.json`（逐镜帧数/检出率/改动量/耗时）+ `utils/lip_sync.py` | — |

整片实测（11 个有台词镜头）：**11/11 成功、人脸检出率 100%、逐镜 4.7~6.2 秒、
整片 62.5 秒（本地 CPU，$0）**，成片仍 67.0 秒、音画差 +0.04 秒。

过程中踩到并修掉的三个真 bug：

1. **模型「静默失效」**：6 通道输入必须是「下半张脸抹掉 + 原图」（原版
   `img_masked[:, 48:] = 0`）。两路都给原图时模型直接从输入抄嘴 —— 输出与输入最大差异
   **0.011**，口型完全没生效却毫无报错。修好后的 mel 灵敏度是 **0.514**。
   现在脚本统计 `mean_patch_change`，低于 0.01 判失败并报警。
2. **相对路径 + 软链**：`cwd=项目目录` 让 `.venv-lipsync/bin/python` 找错位置；
   `Path.resolve()` 又会跟随软链把 venv 的 python 解析成系统 python（`No module named 'cv2'`）。
   改用 `os.path.abspath`（不解析软链）并统一给绝对路径。
3. **mel 补边**：librosa ≥0.10 是 `pad_mode="constant"`，用 reflect 时与它最大差 0.61。

### 2. 画面运动进配音文案（`AUDIO_LLM_MOTION=true`）

静态抽帧只知道画面里是谁、在哪，说不出「一句话里哪一段动得最猛」，于是念法一个速度。
现在本地实测运动曲线（6fps/64×64 灰度、相邻帧平均变化，**纯标准库**）一起发给配音导演：

```
- 整体画面：整段轻微（平均强度 0.0281，峰值 1.67s）；起伏：1.00–1.83s 强烈起伏（平时的 2.5 倍）
- 念法建议：动作最猛在 1.17–1.83s，这段把音量和语速顶上去，让声音跟着动作走
```

判读是两级：**整段活跃度**按均值分档（真实片段实测 0.008~0.090），
**起伏窗口**按中位数的 1.6× 找。只用相对峰值不行 —— 说话的人整段都在动、峰均比 1.5，
会被整段标成「剧烈运动」，等于没说。

### 3. 没做成的事（写下来免得下次再试）

想给口型质量一个**自动判据**，试了四种便宜像素统计量，拿「模型原生语音」（天生同步）
当基准验：最好只有 r≈0.5、最佳滞后 0.1~0.25 秒（画面差异 0.25、亮度 0.08、
暗部占比 0.08、垂直边缘 0.15）。连基准都测不准，说明这些量根本反映不了嘴的开合
（特写里摇头/机位微移都会盖过嘴）。所以**没有**把它做成通过/不通过，
只保留引擎自检 + 改写时段 + 人工看帧。要做成自动判据得另上 SyncNet 或人脸关键点。

## v0.17 · 名字 ↔ 国家 ↔ 脸对齐（哪国人就长哪国脸）

用户报的：**角色设定和名字尽量对齐** —— 角色叫中文名/日文名，就该捏对应国家的
「平均脸 / 主角脸」，而不是中文名配一张欧美脸。

### 问题是什么

图片/视频模型接到 `A 28-year-old woman` 时按自己的先验画，而那个先验**偏欧美化**。
名字是唯一「总是存在」的国籍线索，但它**绝不能进 prompt**（会被画成画面上的文字，
v0.10 的真实事故），所以必须先在本地把名字翻成英文的外貌锚点。

### 做了什么

| 能力 | 实现 | 位置 |
|------|------|------|
| 名字 → 国家（带置信度） | 假名 0.95 / 谚文 0.95 / 文本提示 0.9 / 日本姓氏 0.8 / 罗马字姓氏 0.75·0.7 / 中文姓氏 0.6 / 拉丁兜底 0.6 / **共用汉字姓 0.4** | `utils/region.py` |
| 一部片子一张脸谱 | 全剧组按「置信度 × 角色重要性」投票，分不出国籍的角色跟随多数派 | `region.project_region` |
| 外貌锚点 | `Chinese cast — East Asian features of a typical Chinese television drama lead, an average everyday Chinese face …, not mixed-race and not Westernised` | `region.REGION_ANCHORS` |
| 圣经生成 | 提示词要求「名字、地区、外貌三者必须对齐」，并新增 `Character.region` 字段（人工可改） | `services/bible_service.py` + `models/bible.py` |
| 定妆照 | 注入该角色的国家锚点；已写明人种 / `IMAGE_PROMPT_SUFFIX` 写了人种时**不叠加** | `character_image_service.build_portrait_prompt` |
| 视频 prompt | `appearance_anchor` 补整个剧组的人种锚点（没有参考图时唯一能拦住欧美脸的东西） | `services/video_service.py` |
| 配置 | `IMAGE_REGION_AUTO` / `IMAGE_REGION_DEFAULT` / `IMAGE_REGION_OVERRIDE` | `config.py` + `.env.example` |

### 途中处理的两个真问题

1. **中日韩共用汉字姓判不出来**：金=Kim/Jin、李=Lee/Li、林=Lim/Hayashi…
   这些姓氏单独看**永远分不出国籍**。做法是给 0.4 的**低置信度**，让它跟随整个剧组 ——
   「金敏俊」在国剧里就是中国人、在韩剧里就是韩国人。韩国特有汉字姓（朴/禹/鞠…）
   和片假名韩姓（キム・ジウン）另有高置信度通道。
2. **中文名漏进了视频 prompt**（两处）：
   - `appearance_anchor` 写的是 `林悦: A young woman…`
   - `dialogue_directive` 写的是 `林悦 speaks this exact Mandarin line: "…"`

   和图片 prompt 是同一类风险（中文被画成画面上的文字，v0.10 真实事故）。
   现在两处都不带名字：前者只给外貌，后者改成 `the on-screen speaker says this exact
   Mandarin line`。对模型来说「画面里正在说话的那个人」已经够用。
   验证：真实项目里 build_prompt 的输出已不含任何角色名。

### 测试

新增 `tests/test_region.py`（24 个）：假名/谚文/日文姓氏/韩国特有姓/共用姓/拉丁四种、
文本提示压过名字、剧组投票（含主角加权与 override）、单角色优先级、锚点注入与
「已写明人种不叠加」、prompt 里绝不出现中文、`region` 字段归一化。


## v0.19 · 配乐：按分镜现写一首 + 人声闪避

用户问的：**视频支持插入背景音乐吗（结合视频内容，生成一段符合情境的音乐）？**
改造前只能回答「一半」：曲库选曲是「插入」，不是「生成」—— 一首现成的曲子整段循环，
和弦不会在转场处换和弦，动作爆发的那一下没有重音，有人说话时也不让路。
这一版加了**本地作曲引擎**：先写配乐谱，再合成音频。

### 做了什么

| 能力 | 实现 | 位置 |
|------|------|------|
| 配乐谱 | `MusicPlan` / `MusicSection` / `MusicChord` / `MusicAccent`：色调、调式、BPM、段落、和弦走向、配器、重音、说话窗口、人话说明 | `models/music.py` |
| 情绪 → 色调 | 8 种色调（紧张/悲伤/温暖/希望/激烈/神秘/轻快/中性）：调式 + BPM + 和弦 + 配器 + 混响；分镜情绪**多数票** + 用户自由文本；`music_mood` 可强制 | `utils/music.py` |
| 段落对齐镜头 | 场景边界 / 能量跳变 / 26s 上限切段；段落太少时按总时长自动补切；首段压薄、末段落在主和弦 | 同上 |
| 画面运动进配乐 | 复用 `utils/motion.py` 的实测曲线（与配音文案共用缓存）：镜头能量决定配器厚薄，**爆发时刻给重音** | 同上 |
| 让路 | 有台词的段落去掉鼓与点音；旋律**跳过**说话窗口（用配音的真实时长算窗口） | 同上 |
| 合成 | 纯标准库：波表 + 相位累加 + 包络（pad/弦乐/钢琴/低音/铃/旋律/短和弦）+ 噪声打击（底鼓/军鼓/踩镲/镲/推进器）；44.1kHz 立体声 | 同上 |
| 后处理 | `afir` 卷积混响（IR 由 Python 生成）→ 塑形 EQ → 压缩 → 限幅 → `loudnorm=-18 LUFS` | `services/music_service.py` |
| 交付前自检 | 后处理完量一遍峰值/RMS：`peak<0.02` 或 `RMS<-35dBFS` → 判定坏了，退回干燥音轨并写进报告 | 同上 |
| 缓存 | 输入指纹（镜头时长/情绪/台词、说话窗口、配乐要求、色调、seed、混响）→ 相同就复用 `music_plan.json` | 同上 |
| 人声闪避 | 合成时 `[voice]asplit` → `sidechaincompress`（threshold/ratio/attack/release 可配），ffmpeg 没有该滤镜就退回固定音量并告警 | `services/compose_service.py` |
| 外部模型 | `BGM_ENGINE=cli` + `BGM_CLI_CMD`（`{out}`/`{prompt}`/`{duration}`）；提示词英文 + 段落时间轴 | `services/music_service.py` |
| API / UI | `GET/POST /api/projects/{id}/music`、`/music/audio`、`/music/prompt`；面板：引擎/情绪/要求/音量/闪避 + 段落表 + 试听 | `main.py` / `static/index.html` |
| 配置 | `BGM_ENGINE` / `MUSIC_*` / `BGM_DUCK*` / `BGM_CLI_*` | `config.py` + `.env.example` |

### 途中修掉的三个真 bug（都是「量出来」才发现的）

1. **`afir` 的自动增益把配乐整条压没**：`irnorm=1`（**默认值**）下 67 秒的配乐
   RMS 只有 **-81.7dBFS** —— 成片报告里写着「有配乐（本机现写）」，实际什么都听不见。
   修法：IR 峰值自己在 Python 里定死（`IR_PEAK`），混响用固定 `volume` 混，
   `afir` 只当卷积器（`irnorm=0:gtype=-1`），并且**交付前量一遍**再决定交谁。
   另一个相关坑：`afir` 是流式滤波器，输入不 `apad` 就把卷积尾巴截掉了（混响只剩早期反射）。
2. **`bgm_enabled=false` 静默失效**：`ComposeRequest` 里没有 `bgm_enabled` 字段、
   端点也没把它映射进 `overrides`，于是「本次不要 BGM」被忽略 ——
   A/B 第一轮跑出来的「无 BGM」版本里仍然有音乐（对比数据直接暴露了它）。
   顺带把语义收紧：项目里存着的 `music_engine` **不能**压过 `BGM_ENABLED=false`
   （只有本次请求显式指定引擎才行），并且解析结果权威 —— 结果是「没有曲目」时
   必须**清掉**清单里上一次的曲目，否则「这次不要」会偷偷继续用上一首。
3. **烧字幕分支丢人声**：`burn` + BGM 的滤镜图只 `-map [bgm]`，人声根本没进混音
   （结果会是「只有音乐、没有台词」）。之前的真实项目恰好没有 BGM，所以从没触发过。
   这次重构成同一条 `bgm_mix_filter()`，三种字幕模式共用。

### 实测（真实项目 `e8e40d3d9d29`，67 秒 / 11 镜）

```
配乐：悲伤/压抑 · A 小调 · 66 BPM · 4 段 · 3 个重音（$0）
作曲 + 合成 67.8s 音频：4.5s（86 个音符，纯 Python 单核）
后处理（卷积混响 + 压缩 + 限幅 + 响度归一）：2.0s（ffmpeg）
产出 bgm.wav：67.0s / 峰值 0.534 / RMS -20.4 dBFS（目标 -18 LUFS）
```

人声闪避的精确实测：`tools/music/ducking_harness.py` 用**应用里的真实滤镜链**
（`compose_service.bgm_mix_filter()`）渲染出「固定音量 / 带闪避」两版混音，
再逐样本相减去掉人声（`measure_ducking.py --mode residual`）：

| 版本 | 说话段·音乐 | 长静音段（≥1.2s）·音乐 | 说话-对照 | 静音-对照 |
|------|-------------|------------------------|-----------|-----------|
| 有 BGM（固定音量） | -41.59 dB | -41.49 dB | +0.00 | +0.00 |
| 有 BGM + 人声闪避 | -50.82 dB | -41.49 dB | **-9.23** | **+0.00** |

三个量出来的教训：

1. **只看整体响度会误判**：说话段人声比音乐响 5~14dB，音乐降 9dB 在整体响度上只有零点几 dB。
2. **成片里的 `混音² - 人声²` 估计在深压时不可靠**：音乐被人声压掉 15dB 以上后交叉项占主导，
   同一个 9dB 的闪避会被读成 30dB —— 所以精确口径必须是「同一渲染链、逐样本相减」。
3. **默认门限 -30dBFS 实测只能压 4dB**（几乎听不出来），改成 **-40dBFS** 才是 9dB：
   压缩器的压降 ≈ 人声高出多少 dB × `(1-1/ratio)`。

### 测试

新增 `tests/test_music.py`（19 个：情绪打分口径、段落对齐镜头、重音落在爆发/避开说话窗口、
让路、确定性、LLM 段落建议、WAV 合法性、有起伏、旋律避开说话、IR）
`tests/test_music_service.py`（29 个：三条引擎路径、`cli` 模板校验、缓存指纹与失效、
`afir` 后的声量自检、`_measure_wav`、闪避/音量优先级、LLM 建议降级、设置归一化）
`tests/test_compose.py`（+4 个：闪避滤镜链、FADE 上限、真实 BGM 合成里的 duck 标记）
`tests/test_api.py`（+9 个：`/music` 生成/试听/提示词/缓存、引擎不可用报错、
项目字段往返、端点确实映射了配乐覆盖项、UI 控件标记）。合计 **626 个测试通过**。

## v0.20 · 响度：每句台词的音量对齐（+ 一个分段复用的真 bug）

用户问的：**当前的方案，每个视频（音频）片段，音量是一致的吗？**

量完的答案是：**不一致，极差 15.1dB** —— 逐条配音是独立生成的，音色不同、念法不同。
真实项目（`e8e40d3d9d29`，15 条云端配音）的集成响度从 **-17.0 到 -32.1 LUFS**，
按音色分组更清楚：nova（林悦）-18.2、shimmer（李薇儿）-20.1、onyx（旁白）-21.7、
ash（赵总）-22.9、**coral（张姐）-29.4**。同一音色内部很稳（±1dB），
换个音色就换一个音量 —— 张姐这个音色整片比主角低 11dB。

### 做了什么

| 能力 | 实现 | 位置 |
|------|------|------|
| 响度测量 | ffmpeg `ebur128` 集成响度/真峰值/LRA 解析（gated，不受句首句尾静音干扰） | `utils/loudness.py` |
| 逐条配音归一 | 合成时按条量、补一个**有上限的定值增益**（默认 ±10dB）；增益写进合成报告 | `services/compose_service.py` |
| 念法音量保留 | `volume=+5%` 这类显式念法先换算成 dB 扣掉、归一后加回来（对齐基准，不对齐表演） | `utils/loudness.py::volume_to_db` |
| 成片整体归一 | 混完 BGM 后 `loudnorm` 到 `COMPOSE_TARGET_LUFS`（默认 -16）+ 真峰值限幅 | `services/compose_service.py::_final_loudness_filter` |
| 交付前自检 | 渲染完量一遍成片响度，与目标差 >1.5dB 就写进报告告警 | 同上 |
| 逐次覆盖 | `POST /compose` 可临时传 `audio_normalize` / `compose_loudness_normalize` / `compose_keep_segments` | `main.py` + `pipeline.py` |

### 实测（同一批片段，只切换逐条归一）

逐条渲染出来的**分段**（不是成片，避免镜头边界串音）实测集成响度：

| 版本 | 范围 | 极差 | 标准差 |
|------|------|------|--------|
| 关闭逐条归一 | -32.2 ~ -17.1 LUFS | **15.10 dB** | 4.41 dB |
| 打开逐条归一（默认） | -22.2 ~ -19.7 LUFS | **2.50 dB** | 0.55 dB |

成片整体：-21.9 LUFS（不归一）→ **-16.2 LUFS / 真峰值 -1.5 dBFS**（目标 -16）。

上限为什么是 10dB：真实项目里最安静的音色比主角低 15dB —— 上限给 6dB 时修完还剩 6dB
（听得出来），给 10dB 只剩 2.5dB（听不出来，剩下的是那条需要 +12.1dB 的极端值）。

### 途中修掉的第四个真 bug：分段复用会拿错内容

分段的「渲染参数」原来编码在**标记文件名**里（`.ambient-off.mouth-off.lipsync-on.gain+6.0`），
复用判据是「这个文件名存在 + 分段比输入新」。问题是旧标记不会消失：
上一轮留下的 `.gain+0.0` 标记还在，而分段文件已经被**另一套参数**重渲过 ——
于是「关闭逐条归一」的对比跑复用了「归一之后」的分段，
量出来 12/15 条都是 -20.1（正是归一后的电平），差点得出「关不关都一样」的错误结论。

现在参数写进固定名字的 `{shot}.params` 并比对**内容**，参数一变必然重渲。
这个 bug 从 v0.4（环境音开关）就在，只是以前没有「同一批分段跑两套参数」的对比场景。

### 量响度时踩的三个坑（都写进 README 了）

1. **量错文件**：项目 `audio/` 下同时有旧引擎的 `*.mp3` 和当前引擎的 `*.wav`，
   以 `audio_manifest.json` 的 `path` 为准。第一版统计量了作废的 mp3，
   得出「极差 7.9dB」的错误结论（真实 15.1dB）。
2. **输入端 seek 量短窗口不准**：`-ss` 放在 `-i` 前面在这个 mp4 上落到错误窗口
   （同一段 -32.1 LUFS vs 输出端 -22.0 LUFS）。
3. **按镜头窗口量成片会串音**：镜头边界不是静音边界，上一句的尾巴会压平真实差异
   （15.1dB 被读成 2.1dB）。验证逐条归一必须量逐条分段。

### 测试

新增 `tests/test_loudness.py`（11 个：百分比→dB 的振幅语义、上限裁剪、意图保留、
ebur128 输出解析（含静音与脏数据）、真实测量、归一效果），
`tests/test_compose.py` 新增 5 个（成片响度滤镜、归一接在混音之后、端到端响度落在目标附近、
关掉开关的行为、故意做小声 6dB 的配音应拿到更大增益）。

## v0.21 · 代码 review 的修复（逐条核对）

外部 review 提了 7 个问题和 8 条债务。逐条**复现/核对**之后：6 条确认并修掉、
1 条部分确认（按更实际的方案修）、3 条不成立或已存在。下面按 review 的编号写。

### 确认并修复

| 编号 | 问题 | 复现 | 修法 |
|------|------|------|------|
| B (P0) | `FileStore._load` 无兜底 | ✅ 一个坏 `meta.json` 让 `list_projects()` 抛 `ValidationError` → `/api/projects` 500，其余项目全看不见 | 捕获 `OSError/ValueError`（pydantic 的 `ValidationError` 是 `ValueError` 子类）→ 隔离成 `<名字>.json.bad` + 告警「修好改回即可恢复」→ 返回 `None` |
| C (P0) | 项目锁贯穿整个媒体任务 | ✅ 写用例复现：视频任务卡在生成时，`update_project` 3s 超时都拿不到锁（真实症状：生成期间改标题/删项目/开始合成的请求全部挂住） | 锁只包「读状态 → 落状态」；`generate_clips` / `synthesize_all` 放在锁外，进度回调只做一次原子落盘（同步、不会被其它协程打断）；取消路径与最终状态用短锁重新取 |
| D (P1) | LLM 每次请求新建 `httpx.AsyncClient` | ✅ 每次 `chat` 都 `async with` 新建（video/tts/image 都是实例持有） | provider 持有 client（可注入、有 `_owns_client`）+ `aclose()`；`with_model` 共享同一 client；编排层五个阶段结束后 `await llm.aclose()`（与 video 同一套生命周期，防泄漏） |
| E (P1) | `_repair_json` 无脑替换中文逗号/冒号 | ✅ 注释「中文逗号只可能出现在非字符串语境」对中文剧本产品是错的：`{"text": "你别走，好吗"}` 修完变成 `"你别走,好吗"` | 新增 `_map_outside_strings()`：只在字符串字面量**之外**替换（智能引号仍整篇替换，它们本身就是被误用的定界符） |
| F (P1) | `?token=` 查询串鉴权 | ⚠️ 部分确认 | 写接口/JSON 接口**只认 `X-API-Token` 头**；只读产物路径仍接受 `?token=` —— 因为 `<video src>` / `<a download>` / `<img>` 根本带不了请求头，一律只认头会让「开了 token 就看不到成片、下不了文件」（这个坑今天就存在：UI 从来没给这些地址加过 token）。顺带把前端补成 `withToken(url)`，并给「绑 0.0.0.0 又没设 token」加启动告警 |
| G (P1) | 测试里 `wait_for_job` 未 await | ✅ pytest 有 `RuntimeWarning: coroutine never awaited`；靠 mock 跑得快才碰巧过 | 改用已有的同步 `wait_video_job()`；现在 `pytest -W error::RuntimeWarning` 全绿 |

另外补了 review 提到的 P2 里两条：`ProjectStatus` 上「Phase 2+ 预留（没有任何入口会写入）」
的过时注释（媒体链路 v0.4 就全落地了）、以及一条**密钥护栏**用例
（`tests/test_secrets.py`：扫描所有 git 跟踪文件，出现真实形态的 `sk-or-v1-` 就失败；
并断言 `.env` 始终被忽略且未被跟踪）。

### 核对后**不成立**的

| 编号 | review 的说法 | 实际情况 |
|------|---------------|----------|
| A | 「工作区 `.env` 有真实 key，建议轮换」 | 事实成立（key 确实在 `.env` 里，73 字符真实形态），但**从来没有进过 git**：`.gitignore:8` 忽略、`git log -S "sk-or-v1-"` 命中的 3 个提交全是 `tests/` 里的 `sk-or-v1-DUMMY…` 占位串（形态短、非十六进制）。所以「轮换」是运维建议，不是代码缺陷；仓库侧加了护栏用例。**建议仍然有效：对外分享前只带 `.env.example`** |
| P2 | `_locks` 永不清理 | ❌ 已有清理：`delete_project` 里 `self._locks.pop(project_id, None)`（pipeline.py:2017） |
| P2 | 「`main.py` 双读配置」 | ❌ `get_settings()` 是带缓存的单例，模块级那次只是给中间件/启动日志用，行为一致（review 自己也标注了「读者容易误会」） |

剩下的 P2 是**已知债务、不打算现在做**：`pipeline.py` 2018 行（按 stage 拆分收益有限、
风险不小，状态机集中在一处反而好读）、`static/index.html` 2592 行单页 UI、
Job/Batch 状态仅在内存（多 worker 场景，README 已写明）、依赖锁文件（当前只有
`pyproject.toml` 的下界约束）。

### 测试

新增 `tests/test_file_store.py`（5）、`tests/test_llm_openrouter.py`（8）、
`tests/test_secrets.py`（2），`tests/test_state_machine.py` 加 3 个锁粒度/生命周期用例
（含「短操作不能被长任务挡住」和「视频在跑时不能开始合成」的正反两面），
`tests/test_api.py` 修掉未 await 的 flaky 调用并加 token 策略用例。
合计 **668 个用例通过**，`pytest -W error::RuntimeWarning` 全绿（`ruff check src tests tools` 干净）。

### 端到端冒烟：这一轮改动**事后补跑**了一次（$0）

用户问「这些改动有端到端生成果视频片段吗」—— 诚实回答：**当时没有**。
最后一次真正生成片段是 09-11（真模型、真花钱，`clips/*.mp4` 时间戳都在那天），
最后一次真实成片合成是 09-14 17:44（v0.20 的响度对比），而 v0.21 提交在 18:15：
v0.21 这批改动（锁粒度、文件隔离、token 策略、LLM 生命周期）当时只有单测 +
活服务只读冒烟，没有跑过「生成片段」。

于是补了 `tools/e2e/mock_pipeline_e2e.py`：mock 视频/配音（$0）起独立服务，
走真实 HTTP 接口从「分镜已确认」跑到成片。实测（`--force`，12 项检查全过）：

| 环节 | 结果 |
|------|------|
| 视频片段 | 15/15 成功，落盘 15 个**真 H.264**（540x960，抽样 5.0s） |
| 配音 | 15/15 成功，15 条可播放音频（44.0s 语音） |
| 配乐 | `engine=generate`，intro/bed×2/outro 四段 + 和弦进行，本机作曲 $0 |
| 成片 | `final.mp4` 67.0s、1080x1920 h264 + aac 96kHz、**-16.0 LUFS**（目标 -16）、TP -12.8 dBFS |
| token 策略 | 不带 `X-API-Token` 的写请求 **401**；`?token=` 只读下载 **200** |
| 坏 `meta.json` | `/api/projects` **200**、坏项目隔离成 `.json.bad`、其余项目照常列出 |

**这次冒烟没覆盖的**：文本链路（圣经/剧本/分镜）与真实 provider —— 前者要花钱调 LLM
（`--source` 项目的产物是复制过去的），后者按秒计费。所以 v0.21 的
「LLM client 复用 + `aclose()`」目前仍只有 mock transport 的单测覆盖，
**没有**在真实 OpenRouter 上跑过一次；闪避深度这次也没重量（那个数字来自 v0.19 的真实成片 -9.23 dB）。

### 方案 B：真跑 3 个镜头（$1.9512）

用户选了「真花钱但小口径」：只重生成 3 个镜头，把**真 provider 的画面 + 真 TTS** 这条
mock 冒烟碰不到的路走通。工具 `tools/e2e/real_shot_e2e.py`，
做法是复制项目 `e8e40d3d9d29` → 新项目 `e2eb00000001`、
只删 `scene_1_shot_1 / scene_2_shot_2 / scene_3_shot_2` 的片段与配音 →
只补这 3 条（其余 12 镜复用**已经付过钱**的片段），**源项目一个字节没动**
（片段仍是 09-11 的时间戳、账本仍是 $24.2544、45 条记录）。

preflight 先给的报价：3 镜 = 15 计费秒 × $0.13 = **$1.95**（若整片 15 镜全跑是 $10.27）。
实测账本增量 **$1.9512**（视频 3 次 $1.95 + 云端 TTS 3 条 $0.001236），
累计 **$26.2057 / $30**。

| 环节 | 结果 |
|------|------|
| 视频片段 | 3/3 成功（`minimax/hailuo-3`，1440x2560，5.167s，与旧片段同规格、内容不同） |
| 配音 | 3/3 成功（云端 TTS，`script_mode=off` 避免额外 LLM 调用） |
| 逐帧口型 | 11/11 个有台词的镜头本地重画嘴部（wav2lip-onnx，$0）；3 个镜头的配音被延后对齐 0.08~0.46s |
| 配乐 | 本机现写（悲伤/压抑 · A 小调 · 66BPM · 4 段），$0 |
| 成片 | `final.mp4` 67.0s、26.55MB、1080x1920 h264 + aac 96kHz + **burn 字幕**（用带 libass 的便携 ffmpeg） |
| 成片响度 | **-16.1 LUFS / TP -1.5 dBFS**（目标 -16） |
| 逐条归一 | 15 条增益 -2.6~+10.0dB；**14 条落在 -19.7~-20.1 LUFS**，极差 3.3dB |
| 人声闪避 | **-10.42dB**（说话时音乐比对照低）/ **+0.00dB**（长静音窗），residual 口径，判据 ≥4dB 达标 |

**新发现（v0.20 那个上限现在成了唯一的限制）**：极差 3.3dB 里只有一条是坏的 ——
张姐音色的 `scene_2_shot_2` = **-23.0 LUFS**，它的增益已经被顶到 `AUDIO_NORMALIZE_MAX_DB=10`
上限（这一条需要约 +13dB）。其余 14 条的极差只有 **0.4dB**。
也就是说「对齐算法」已经做到头了，剩下的差距是**故意留的上限**在保护表演 ——
想完全拉平把上限调到 13 即可（重合成 $0，分段参数变了会自动重渲）。


## v0.21.1 · 取消任务漏记账（真实事故 $1.95）+ 角色「照着脸画」

### 取消任务不记账（真金白银漏记）

**怎么发现的**：做一个《新三国》动漫化短片时，9 个镜头跑到 3 个（3 × $0.65 = $1.95）
用户叫停 —— 查账本发现**一条视频记录都没有**。片段是 provider 真按秒计过费的，
而账本里只有 LLM 与定妆照的 $0.2431。

**根因**：`_run_video_job` / `_run_audio_job` 只在**正常结束**后调用 `_record_video_cost`
（按片段上的 `cost_recorded` 标记记账），`except asyncio.CancelledError` 分支只收尾状态、
不记账。这些片段随后被删掉（换定妆照重跑），连 `cost/reconcile` 的补记账机会都没有 ——
**账本少一笔比多一笔危险得多**，因为预算门控是按账本放行的。

**修法**：取消分支里也按同一套「`cost_recorded` 标记」补记一次（视频 + 配音两条路径），
配音那段顺手抽成 `_record_audio_cost()` 与视频对称。因为标记在，续跑不会重复计费。

**注意取消的时序**：`jobs.cancel()` 会**立刻**把任务置成 `cancelled` 再 `task.cancel()`，
所以协程的收尾（含记账）是在状态落定**之后**才跑完的 —— 测试里必须等账本落地
（`test_cancelled_video_job_still_records_cost` 里先轮询账本再断言）。

**测试**：新增 `test_cancelled_video_job_still_records_cost`
（跑完 ≥1 镜后取消 → 账本里必须出现这部分钱、标记已记账 → 续跑跑完剩下的镜头后
账本总额正好等于「全部镜头 × 单价」，既不漏记也不重复计）。测试用
`FakeVideoProvider` 新增的 `actual_cost_usd` 模拟 provider 实报扣费。
本项目的 $1.95 已手工补记进账本（`cost_service.video_entry(..., actual_usd=1.95)`）。

### 角色「照着一张具体的脸画」：`tools/image/restyle.py`

需求是「拿剧照转成漫画风当定妆照」。图片 provider 本来就支持参考图输入
（`input_references`，`supports_reference_image=True`），所以这是**一次图生图**：
照片当参考图 + 风格化提示词（含「必须保留五官特征」的硬约束），$0.04/张、约 20 秒。

- 预设 `anime` / `comic` / `ink`，可用 `--prompt` 全覆盖
- `--project/--character` 转完直接调上传接口装成角色定妆照
- 用我们自己生成的一帧真实风画面做过对照验证：转出来的动漫脸保留了脸型、眉形、
  发际线与服装细节（对照图：`.e2e/anime/compare.jpg`）
- **边界**：只处理有权使用的图。把别人的剧照转成二次元再公开传播，同时涉及摄影作品版权
  与演员肖像权 —— 所以「从网上扒剧照」这一步不由 agent 代劳，需要用户自己提供文件

### 顺带

- HTML/README 里「参考图为什么有效」那段有一段重复的正文，删掉
- 用例 668 → **669**（`ruff check src tests tools` 干净）
