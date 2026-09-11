# videogen

剧本生成 + AI 短剧自动化流水线：`创意输入 → 故事圣经 → 逐场剧本 → 分镜 → 视频片段 → 配音 → 成片（FFmpeg 合成 + 字幕）`

> 设计文档见 [DESIGN.md](DESIGN.md)；历史问题审计见 [REVIEW.md](REVIEW.md)；本轮修复记录见 [FIXES.md](FIXES.md)。

## 当前实现状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| 项目 / 文件存储 | ✅ | `projects/{id}/*.json`，原子写 |
| 故事圣经 Story Bible | ✅ | 角色 / 设定 / 关键事实 / 风格，LLM 生成 |
| 逐场剧本 + World State | ✅ | 逐场注入 bible + 前场摘要 + 世界状态，逐场落盘 |
| 一致性校验 | ✅ | 人名/数字/时间线/时长 + 分镜层校验 |
| 分镜 + 英文 video_prompt | ✅ | 每场 2-5 镜，时长自动对齐剧本 |
| 视频片段生成 | ✅ | `VideoProvider` 抽象：`mock`（免费占位片段）+ `openrouter`（**真实视频模型**，默认 MiniMax H3） |
| 配音 TTS | ✅ | `TTSProvider` 抽象 + `EdgeTTSProvider`（真实中文配音）+ `MockTTSProvider`（离线） |
| 音画对齐 | ✅ | 配音比镜头长时按配音**拉长镜头**，合成时冻结末帧补足，不重烧视频额度 |
| 配音文案（LLM 看画面写/改） | ✅ | `AUDIO_LLM_SCRIPT=off\|narration\|polish\|full`，把片段抽帧发给多模态 LLM 写旁白/改台词，结果写回分镜 |
| BGM 曲库 | ✅ | 扫 `BGM_DIR` + `bgm.json` 元数据，按镜头情绪自动选曲 |
| FFmpeg 合成 + 字幕 | ✅ | 分段渲染 → concat → BGM 混音 → 字幕（soft 内嵌 / burn 烧录 / none 只导 srt） |
| 后台任务队列 | ✅ | 提交即返回 job id，进度查询 / 取消 / 单项目互斥 / 重启恢复 |
| 手工编辑 / 分阶段确认门控 | ✅ | Web UI + REST |
| 成本统计与预算 | ✅ | 按阶段记 token / 秒数 / 字符 → USD 账本，超预算直接拒绝开工 |
| 批量生产 | ✅ | 一次提交多个主题自动跑完整链路（可指定跑到哪一步停） |
| 多模型切换 | ✅ | 项目级或单次请求指定模型，候选列表 + 单价由配置给出 |
| 角色一致性 | ✅ | 参考图走 `input_references`（真实模型）或取色（mock），seed 仅在模型支持时透传 |

## 快速开始

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,media]"
cp .env.example .env          # 填入 OPENROUTER_API_KEY
python -m videogen.tools.demo_bgm   # 可选：生成 4 首演示 BGM 到 ./assets/bgm
./run.sh                      # 默认 http://127.0.0.1:8000
```

**配音（TTS）不需要任何配置**：默认 `TTS_PROVIDER=edge`（微软 Edge-TTS，免费、**不用 key**、
只要能联网），音色按角色性别自动分配；`TTS_PROVIDER=mock` 则用本地 ffmpeg 生成占位音、完全离线。
想换成付费 TTS 需要自己实现一个 `TTSProvider`（目前只有 edge / mock 两个实现）。

ffmpeg 解析顺序是 `FFMPEG_PATH` → `PATH` → `imageio-ffmpeg`（`media` 额外依赖，自带便携二进制）。
**三者都没有时视频阶段仍可运行，但只会产出明确标记的占位文件（`media_kind=placeholder`），
绝不假装成功；配音同样降级为占位文件，合成会直接拒绝。**

字幕模式默认 `soft`（mp4 内嵌 `mov_text` 轨，只需要 `mov_text` 编码器）；想烧进画面（`burn`）需要
ffmpeg 带 **libass**。注意 Homebrew 的 `ffmpeg` 9.x 精简构建**没有** libass，此时会自动降级为
`soft` 并在报告里告警；要真的烧字幕，把 `FFMPEG_PATH` 指向带 libass 的构建即可，例如
`pip install imageio-ffmpeg` 附带的那个：

```bash
# 查看当前 ffmpeg 的能力（也能从 GET /api/health 看到）
python -c "import asyncio,videogen.utils.ffmpeg as f; \
  print(asyncio.run(f.ffmpeg_capabilities()) & {'subtitles','mov_text'})"
```

`HOST=0.0.0.0 PORT=8000 RELOAD=0 ./run.sh` 可覆盖监听地址（**暴露到局域网前请设置 `API_TOKEN`**）。

跑测试与静态检查：

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

## 流水线与状态机

```
DRAFT ──生成剧本──▶ SCRIPT_REVIEW ──确认──▶ SCRIPT_CONFIRMED
  ▲                     │                        │
  └────── reopen ───────┴────────────────────────┘
                                                 │
                                   生成分镜 ▼
STORYBOARD_REVIEW ──确认──▶ STORYBOARD_CONFIRMED ──reopen──▶ SCRIPT_CONFIRMED
                                    │
                          生成视频 ▼
VIDEO_GENERATING ─▶ VIDEO_READY ──生成配音──▶ AUDIO_GENERATING ─▶ AUDIO_READY
                                                                      │
                                                          合成成片 ▼
                                              COMPOSING ─▶ DONE（成片 + 字幕）
```

- 每个 `*_REVIEW` 都是人工确认门控；确认后想改，必须先 `reopen`，不会静默覆盖
- 生成失败 → `FAILED`，**已完成的场次/镜头保留**，重试时自动续跑（不会重新烧 token）
- 旧数据里的 `script_generated` / `video_ready` 会在读取时自动迁移到新状态
- 视频阶段是**后台任务**：`POST .../video/generate` 立刻返回 `job`，用
  `GET .../video/status` 查进度；跑的时候项目状态是 `VIDEO_GENERATING`，
  完成后是 `VIDEO_READY`（部分镜头失败也算 ready，失败镜头可在 manifest 里看到并重试）
- 配音与合成同样是**后台任务**：`POST .../audio/generate`、`POST .../compose` 立即返回 `job`
  （分别用 `audio/status`、`compose/status` 查进度），可取消、单项目同类型互斥
- **上游一变，下游媒体立即作废**：改剧本/分镜会清掉 `clips/`、`audio/`、`output/`；
  补跑镜头会作废成片但**保留配音**（配音只跟文本有关，不该重复花钱）
- 音画对齐：合成前把每个镜头时长按配音拉长（`AUDIO_GAP_SEC` 是留白），
  视频片段不够长就用 `tpad` 冻结末帧补足 —— 不重新生成视频
- 合成前强校验：视频或配音有失败/占位文件时**直接拒绝**，绝不产出看起来成功的残缺成片

## 真实视频模型（按秒计费）

默认 `VIDEO_PROVIDER=mock`（不花钱）。要出真实画面，把它切成 `openrouter`：

```bash
VIDEO_PROVIDER=openrouter
OPENROUTER_VIDEO_MODEL=minimax/hailuo-3   # MiniMax H3
BUDGET_USD=12                              # 强烈建议先设预算（见下）
```

走的是 OpenRouter 统一的异步视频接口（`POST /api/v1/videos` → 轮询 → 下载），
用的是你已有的那把 key。能力（分辨率/时长档位/单价/是否支持 seed 与参考图）
是**运行时从 `/api/v1/videos/models` 读的**，不写死，所以换模型不用改代码。

| 模型 | 分辨率 | 时长档位 | 单价 |
|------|--------|----------|------|
| `minimax/hailuo-3`（H3） | 2K | 5~15 秒 | **$0.13/秒**（+$0.04/张参考图） |
| `minimax/hailuo-3-max`（H3 Max） | 480p / 768p | 5~15 秒 | $0.05 / $0.08 每秒 |
| `minimax/hailuo-2.3` | 1080p（仅 16:9） | 6 / 10 秒 | $0.0817/秒 |

**三个必须知道的坑，代码里都处理了：**

1. **最短 5 秒**：H3 不支持 5 秒以下的镜头。我们的分镜常见 2~4 秒，请求会被抬到 5 秒，
   多出来的部分由合成阶段裁掉（画面不会串味），但**计费按 5 秒算** —— 所以
   「45 秒成片、15 个 3 秒镜头」实际计费 **75 秒 ≈ $9.75**。想省钱有两个办法：
   把分镜改成「少而长」（9 镜 × 5 秒 = 45 秒 → $5.85），或者换 `hailuo-3-max` 跑 480p 草稿（$3.75）
2. **不支持 seed**：H3 的 `seed` 能力是 `false`，所以请求里不会带 seed（带了会被 400）。
   角色一致性改用 **参考图**（`input_references`，reference-to-video）：在 UI 的角色卡片上
   传一张图，或 `POST /api/projects/{id}/characters/{cid}/reference`
3. **参考图被拒会自动降级**：万一服务端不接受参考图，会**去掉参考图重试一次**并告警 ——
   宁可这次一致性差一点，也不让整条链路挂掉

预算与对账：账本优先用 OpenRouter 在任务响应里报的**实际扣费**（`usage.cost`，最准）；
拿不到时才退回「价目表单价 × 模型档位换算后的秒数」
（不是配置里的估计值），所以 `GET /api/projects/{id}/cost` 看到的就是实际口径。
超预算时**在提交之前**就会被拒绝（`BudgetExceeded` → HTTP 400），不会"先跑完再发现爆了"。

> 从 `mock` 切到真实模型后，**已就绪的旧片段会自动全部重跑**（清单里记了 provider/model，
> 不一致就作废），所以直接再点一次「生成视频片段」即可，不用手动删文件。
>
> **重试只发生在「生成失败」时**：一旦片段已经生成成功，产物下载/落盘失败**不会**重试
> （重新提交等于再买一份），错误里会带上 job id 与下载地址方便人工捞回。
>
> 单个视频任务跑几分钟是常态；`VIDEO_CONCURRENCY` 控制同时提交几个（默认 3）。
> **取消只是停止轮询，服务端任务可能仍在跑并计费** —— 这是异步视频 API 的固有行为。

## 声音设计（对白 / 环境音 / BGM 三层）

一段成片的声音由三层叠出来，都可以单独关掉：

| 层 | 来源 | 开关 |
|----|------|------|
| 对白 | Edge-TTS（`audio/<shot>.mp3`） | `TTS_PROVIDER=edge\|mock` |
| 环境音 | **视频模型自带的音轨**（H3 等会生成） | `COMPOSE_AMBIENT_MODE=off\|under`，音量 `COMPOSE_AMBIENT_GAIN_DB=-20` |
| BGM | 本地曲库按情绪匹配 | `BGM_ENABLED` / `BGM_ID` / `BGM_GAIN_DB` |

`COMPOSE_AMBIENT_MODE=under` 时，把模型音轨压到 `-20 dB` 再和人声 `amix`（`normalize=0`，
人声音量不被拉低）；某个镜头没有台词时，环境音就是它唯一的音轨。前提是**探测到片段真的有音轨**
—— 这需要 `ffprobe`（`brew install ffmpeg` 会带），探不到就只告警并退回 `off`，不会硬拼一条
ffmpeg 会报错的滤镜图。切换这个开关会让分段重新渲染。

实测（真实 H3 片段，本地 ffmpeg，零 API 费用）：H3 自带音轨 `mean=-19.3dB`，几乎和人声
（`-19.5dB`）一样响，所以 `-20dB` 的衰减是必要的；混音后两次成片的音频**残差为 -49.7dB**，
即环境音落在人声下面约 26dB —— 有氛围但不盖对白。想更"在场"可试 `-12 ~ -14dB`。

## 配音文案（让 LLM 看着画面写台词）

分镜里没台词的镜头，过去是「静默镜头」；打开这个开关后，会把**真实视频片段的抽帧**连同
分镜与剧本一起发给多模态 LLM，让它写旁白、把台词改得更口语，并给出逐句念法建议。

| 模式 | 做什么 | 花什么 |
|------|--------|--------|
| `off`（默认） | 只用分镜里的原文案 | 0 |
| `narration` | 只给「没台词也没旁白」的镜头补旁白 | 一次 LLM 调用/场 |
| `polish` | 只把已有台词/旁白改得更口语、更贴画面 | 一次 LLM 调用/场 |
| `full` | 补旁白 + 改写 + 念法建议（`rate`/`volume`/`pitch`） | 一次 LLM 调用/场 |

```bash
# .env：让 LLM 给所有没台词的镜头补旁白（关掉抽帧更便宜）
AUDIO_LLM_SCRIPT=narration
AUDIO_LLM_VISION=true
AUDIO_LLM_FRAMES_PER_SHOT=1
```

实现要点（都是些踩过的坑）：

- **按场调用**而不是整片一次：一场 2-5 个镜头，图不会堆成一大坨，单场失败也只影响一场
- **结果写回分镜**：`shot.narration / dialogue_text` 直接被改写，字幕因此自动跟着新文案走；
  原文留在 `shot.voice_script_original` 里可人工核对
- **同模式重跑跳过**：镜头带 `voice_script_source=llm:<mode>` 标记，重跑不再花钱；
  想重新生成用 `force_script=true`（UI 有勾选框）
- **抽帧缓存**在 `projects/<id>/frames/<shot>.jpg`，片段作废时一起清掉
- **念法解析**：模型随口写「慢 10%」也能解析成 `+10%`，超出范围会夹到 `-50%~+100%`
- **一律降级**：模型漏镜头、返回坏 JSON、抽帧失败、甚至整段调用失败，都只记 warning
  然后用原文案继续配音 —— 文案阶段永远不会把配音打死
- 文案改动会**触发重新合成该条配音**（否则旧音频会盖住新文案），并作废旧成片

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查（含 LLM key 是否配置，**不返回密钥**） |
| POST/GET | `/api/projects` | 创建 / 列表 |
| GET/PUT/DELETE | `/api/projects/{id}` | 详情 / 改标题比例时长 / 删除 |
| POST | `/api/projects/{id}/bible/generate` | 生成故事圣经 |
| GET/PUT | `/api/projects/{id}/bible` | 读取 / 全量覆盖（仅剧本确认前） |
| POST | `/api/projects/{id}/script/generate` | 生成剧本（失败后重试即续跑） |
| GET/PUT | `/api/projects/{id}/script` | 读取 / 全量覆盖（仅确认前） |
| POST | `/api/projects/{id}/script/confirm` · `/reopen` | 确认 / 退回剧本阶段 |
| POST | `/api/projects/{id}/storyboard/generate` | 生成分镜（需先确认剧本） |
| GET/PUT | `/api/projects/{id}/storyboard` | 读取 / 全量覆盖（仅确认前） |
| POST | `/api/projects/{id}/storyboard/confirm` · `/reopen` | 确认 / 退回分镜阶段 |
| GET | `/api/projects/{id}/outline` | 分场大纲（含每场时长预算） |
| GET | `/api/projects/{id}/world-state` | 世界状态 |
| GET | `/api/projects/{id}/consistency` | 一致性报告 |
| POST | `/api/projects/{id}/consistency/revalidate` | 按当前产物重算报告 |
| POST | `/api/projects/{id}/video/generate` | **提交视频生成任务**（202 + job，异步） |
| GET | `/api/projects/{id}/video/status` | 任务进度 + 片段清单 + 统计 |
| GET | `/api/projects/{id}/video/manifest` | 只取片段清单 |
| POST | `/api/projects/{id}/video/cancel` | 取消正在跑的视频任务 |
| GET | `/api/projects/{id}/clips/{shot_id}` | 取某个镜头的视频文件（路径经越界校验） |
| POST | `/api/projects/{id}/audio/generate` | **提交配音任务**（202 + job；body 可带 `voices` 换音色、`script_mode`/`use_vision`/`force_script` 控制 LLM 文案） |
| GET | `/api/projects/{id}/audio/status` · `/audio/manifest` | 配音进度 / 清单（含音色表、对齐记录） |
| POST | `/api/projects/{id}/audio/cancel` | 取消配音任务 |
| GET | `/api/projects/{id}/audio/{clip_id}` | 试听某条配音 |
| POST | `/api/projects/{id}/compose` | **提交合成任务**（202 + job；body 可覆盖字幕模式/环境音/BGM/画质） |
| GET | `/api/projects/{id}/compose/status` | 合成进度 + 成片报告 |
| POST | `/api/projects/{id}/compose/cancel` | 取消合成任务 |
| GET | `/api/projects/{id}/final` · `/subtitles` | 下载成片 MP4 / 字幕 SRT |
| GET | `/api/models` | 可选模型 / 单价 / 预算（前端下拉用） |
| GET | `/api/projects/{id}/cost` | 成本账本：按阶段的花费、token、秒数、字符 + 预估 |
| POST/GET/DELETE | `/api/projects/{id}/characters/{cid}/reference` | 角色参考图（base64 上传 / 下载 / 删除） |
| POST | `/api/batches` | **批量生产**（202 + batch；`stop_after` 可只跑到 storyboard/video/audio） |
| GET | `/api/batches` · `/api/batches/{id}` · `/api/batches/stages` | 批量列表 / 详情 / 可选阶段 |
| POST | `/api/batches/{id}/cancel` | 取消批量任务 |
| GET | `/api/projects/{id}/jobs` · `/api/jobs/{job_id}` | 任务列表 / 单任务状态 |

接口文档：`/docs`。

## 代码结构

```
src/videogen/
├── config.py                 # pydantic-settings，密钥字段 repr=False
├── main.py                   # FastAPI 入口（静态 UI / 可选 token 鉴权 / 错误映射）
├── models/                   # 数据模型（容错校验器、llm_io schema、video / audio / compose）
├── services/                 # bible / script / storyboard / consistency / video / audio
│                             #   / audio_script（LLM 看画面写配音文案）/ subtitle（SRT 时间轴）
│                             #   / compose（FFmpeg 合成）/ cost（记账）
├── providers/llm/            # LLMProvider 抽象 + OpenRouter 实现（JSON 修复 + schema 重试）
├── providers/video/          # VideoProvider 抽象 + MockVideoProvider（ffmpeg 占位视频）
├── providers/tts/            # TTSProvider 抽象 + EdgeTTSProvider + MockTTSProvider
├── providers/bgm/            # 本地 BGM 曲库（元数据 + 按情绪匹配）
├── orchestrator/
│   ├── pipeline.py           # 状态机、门控、锁、断点续跑、媒体失效、成本记账
│   ├── batch.py              # 批量生产（多主题串/并行跑完整链路）
│   └── jobs.py               # 进程内后台任务队列（进度/取消/互斥/裁剪）
├── tools/demo_bgm.py         # 生成演示用免版权 BGM（ffmpeg 合成）
└── utils/                    # file_store / coerce / duration / ffmpeg / text / redact
```

## 设计要点

- **LLM 输出必须过 schema**：`chat_json(messages, schema=...)` 校验失败会把错误回灌给模型重试，最终仍失败才抛错
- **脏数据容错**：`7.5` / `"约10秒"` / `null` 这类字段由 `utils/coerce.py` 兜住，不会让整个项目报废
- **时长强约束**：大纲阶段下发每场预算，生成后按目标时长归一化，分镜再按场对齐
- **产物可追溯**：每一步都落盘 JSON；写入是原子的（临时文件 + `os.replace`）
- **视频阶段容错**：按镜头并发（`VIDEO_CONCURRENCY`）、失败重试、单镜失败不阻塞其他镜头、
  已就绪的片段不重复生成；缺 ffmpeg 时产出**明确标记**的占位文件
- **进程重启不乱**：启动时把卡在 `VIDEO_GENERATING` / `AUDIO_GENERATING` / `COMPOSING`
  的项目收回到最近一个「产物确实存在」的状态
- **配音容错**：按镜头并发（`TTS_CONCURRENCY`）、单条失败不阻塞其他条目、失败可重跑、
  已就绪的条目（含音色）不重复合成；换音色只重跑那个角色
- **花钱前先问一句**：`BUDGET_USD` > 0 时，每个阶段开工前都会检查「已花 + 预估」；
  视频按秒、配音按字符预估，超了直接拒绝并说明还差多少，绝不事后才发现爆预算
- **换模型不改代码**：项目的 `llm_model` 或单次请求的 `{"model": ...}` 都能切换，
  成本账本会记下每个阶段用的是哪个模型
- **批量生产的边界**：批量**跳过人工确认门控**（这是它的意义），所以它复用同一套
  `run_chain`（文本 → 视频 → 配音 → 合成），单条失败不影响其它条目，失败条目保留项目便于接管
- **声音分层不打架**：对白 / 环境音 / BGM 三层各自有音量与开关，环境音必须**先探测到音轨**
  才混（没有 ffprobe 就明确降级并告警，不猜）
- **字幕时间轴与成片一致**：SRT 按分段时长累加生成，每条字幕只覆盖「说话的那几秒」，
  默认字号 = `min(宽,高) × 5%`（竖屏 1080 宽 ≈ 54px）
- **LLM 不掺和「最后一句」**：配音文案阶段只负责写/改文本，音色、时长对齐、字幕时间轴
  仍然由确定性代码算；文案写坏了最多是「这句不好听」，不会让成片对不上轴
- **多模态消息**：`LLMMessage.content` 支持 `str` 或 OpenAI 风格分片数组，
  抽帧以 data URL 形式发送；文字部分用 `message_text()` 取，记账/日志不会被图片干扰

## 已知限制

1. **单价要自己核对**：默认单价只是常见值，真实账单请改 `.env` 里的 `*_PRICE_*`
2. **批量跳过人工确认**：批量链路不会停下来等人审核剧本/分镜，量产前建议先单条跑通
3. **一个镜头最多一条配音**：分镜同时给了对白和旁白时只念对白（旁白被跳过并记 warning），
   否则两条音轨会在同一镜头里互相盖住
4. **任务状态在内存里**：多 worker 部署时 `GET /jobs/{id}` 可能查不到（项目状态本身已落盘，重启会自动回收）
5. **锁是单进程内的**：多 worker / 多进程部署需要换成文件锁或数据库
6. **合成是串行 CPU 活**：分段渲染并发受 `COMPOSE_CONCURRENCY` 限制，长片会占满 CPU
7. **真实视频模型的固有约束**：最短 5 秒（H3）、不支持 seed、取消不退款；
   真实画面质量取决于 prompt，本项目只负责把参数与流程接对
8. UI 没有鉴权登录页，`API_TOKEN` 通过 `?token=` 或 `localStorage` 传递
9. **配音文案会改写分镜文本**：`narration` / `polish` / `full` 直接写进分镜（原文留在
   `voice_script_original`），属于「人工确认后的产物被程序改动」——不想要就别开这个开关
10. **抽帧是按镜头抽的静态图**：模型看到的是画面而不是运动，动作/运镜类信息它读不到

## 安全

- `.env` 不要提交，也不要贴到任何地方；泄漏后请立即到 OpenRouter 轮换
- 所有对外异常/日志都经过 `scrub_secrets()` 脱敏，密钥不会进入日志、前端或报告
- 服务默认只监听 `127.0.0.1`；对外暴露时请设置 `API_TOKEN`
