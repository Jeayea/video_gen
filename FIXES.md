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
