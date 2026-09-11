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
| 视频片段生成 | ✅ | `VideoProvider` 抽象 + `MockVideoProvider`（ffmpeg 生成真实 MP4 占位片段） |
| 配音 TTS | ✅ | `TTSProvider` 抽象 + `EdgeTTSProvider`（真实中文配音）+ `MockTTSProvider`（离线） |
| 音画对齐 | ✅ | 配音比镜头长时按配音**拉长镜头**，合成时冻结末帧补足，不重烧视频额度 |
| BGM 曲库 | ✅ | 扫 `BGM_DIR` + `bgm.json` 元数据，按镜头情绪自动选曲 |
| FFmpeg 合成 + 字幕 | ✅ | 分段渲染 → concat → BGM 混音 → 字幕（soft 内嵌 / burn 烧录 / none 只导 srt） |
| 后台任务队列 | ✅ | 提交即返回 job id，进度查询 / 取消 / 单项目互斥 / 重启恢复 |
| 手工编辑 / 分阶段确认门控 | ✅ | Web UI + REST |
| 成本统计 / 批量生产 | ❌ | DESIGN Phase 4（打磨），尚未实现 |

## 快速开始

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,media]"
cp .env.example .env          # 填入 OPENROUTER_API_KEY
python -m videogen.tools.demo_bgm   # 可选：生成 4 首演示 BGM 到 ./assets/bgm
./run.sh                      # 默认 http://127.0.0.1:8000
```

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
| POST | `/api/projects/{id}/audio/generate` | **提交配音任务**（202 + job；body 可带 `voices` 换音色） |
| GET | `/api/projects/{id}/audio/status` · `/audio/manifest` | 配音进度 / 清单（含音色表、对齐记录） |
| POST | `/api/projects/{id}/audio/cancel` | 取消配音任务 |
| GET | `/api/projects/{id}/audio/{clip_id}` | 试听某条配音 |
| POST | `/api/projects/{id}/compose` | **提交合成任务**（202 + job；body 可覆盖字幕模式/BGM/画质） |
| GET | `/api/projects/{id}/compose/status` | 合成进度 + 成片报告 |
| POST | `/api/projects/{id}/compose/cancel` | 取消合成任务 |
| GET | `/api/projects/{id}/final` · `/subtitles` | 下载成片 MP4 / 字幕 SRT |
| GET | `/api/projects/{id}/jobs` · `/api/jobs/{job_id}` | 任务列表 / 单任务状态 |

接口文档：`/docs`。

## 代码结构

```
src/videogen/
├── config.py                 # pydantic-settings，密钥字段 repr=False
├── main.py                   # FastAPI 入口（静态 UI / 可选 token 鉴权 / 错误映射）
├── models/                   # 数据模型（容错校验器、llm_io schema、video / audio / compose）
├── services/                 # bible / script / storyboard / consistency / video / audio
│                             #   / subtitle（SRT 时间轴）/ compose（FFmpeg 合成）
├── providers/llm/            # LLMProvider 抽象 + OpenRouter 实现（JSON 修复 + schema 重试）
├── providers/video/          # VideoProvider 抽象 + MockVideoProvider（ffmpeg 占位视频）
├── providers/tts/            # TTSProvider 抽象 + EdgeTTSProvider + MockTTSProvider
├── providers/bgm/            # 本地 BGM 曲库（元数据 + 按情绪匹配）
├── orchestrator/
│   ├── pipeline.py           # 状态机、门控、锁、断点续跑、媒体失效
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
- **字幕时间轴与成片一致**：SRT 按分段时长累加生成，每条字幕只覆盖「说话的那几秒」，
  默认字号 = `min(宽,高) × 5%`（竖屏 1080 宽 ≈ 54px）

## 已知限制

1. **成本统计与批量生产未实现**（DESIGN Phase 4）：没有 token/USD 账本，也没有一次跑多个主题的入口
2. **一个镜头最多一条配音**：分镜同时给了对白和旁白时只念对白（旁白被跳过并记 warning），
   否则两条音轨会在同一镜头里互相盖住
3. **任务状态在内存里**：多 worker 部署时 `GET /jobs/{id}` 可能查不到（项目状态本身已落盘，重启会自动回收）
4. **锁是单进程内的**：多 worker / 多进程部署需要换成文件锁或数据库
5. **合成是串行 CPU 活**：分段渲染并发受 `COMPOSE_CONCURRENCY` 限制，长片会占满 CPU
6. UI 没有鉴权登录页，`API_TOKEN` 通过 `?token=` 或 `localStorage` 传递

## 安全

- `.env` 不要提交，也不要贴到任何地方；泄漏后请立即到 OpenRouter 轮换
- 所有对外异常/日志都经过 `scrub_secrets()` 脱敏，密钥不会进入日志、前端或报告
- 服务默认只监听 `127.0.0.1`；对外暴露时请设置 `API_TOKEN`
