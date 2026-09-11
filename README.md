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
| 分镜档位感知（少而长） | ✅ | 有真实 provider 时按最短计费档位决定每场几个镜头（H3 → 每镜 ≥5 秒），从源头避免浪费 |
| 视频片段生成 | ✅ | `VideoProvider` 抽象：`mock`（免费占位片段）+ `openrouter`（**真实视频模型**，默认 MiniMax H3） |
| 配音 TTS | ✅ | `TTSProvider` 抽象 + `EdgeTTSProvider`（真实中文配音）+ `MockTTSProvider`（离线） |
| 音画对齐 | ✅ | 配音比镜头长时按配音**拉长镜头**，合成时冻结末帧补足，不重烧视频额度 |
| 配音文案（LLM 看画面写/改） | ✅ | `AUDIO_LLM_SCRIPT=off\|narration\|polish\|full`，把片段抽帧发给多模态 LLM 写旁白/改台词，结果写回分镜 |
| BGM 曲库 | ✅ | 扫 `BGM_DIR` + `bgm.json` 元数据，按镜头情绪自动选曲 |
| FFmpeg 合成 + 字幕 | ✅ | 分段渲染 → concat → BGM 混音 → 字幕（soft 内嵌 / burn 烧录 / none 只导 srt） |
| 后台任务队列 | ✅ | 提交即返回 job id，进度查询 / 取消 / 单项目互斥 / 重启恢复 |
| 手工编辑 / 分阶段确认门控 | ✅ | Web UI + REST |
| 成本统计与预算 | ✅ | 按阶段记 token / 秒数 / 字符 → USD 账本，超预算直接拒绝开工 |
| 成本前置 preflight | ✅ | 动手前先算账：镜头数 / **按模型档位的真实计费秒数** / 单价 / 预算余额 / 花钱前二次确认 |
| 短镜头合并（省钱） | ✅ | 把 <最短档位的镜头合并成更少更长的片段（`/storyboard/merge-shots`，默认只预览） |
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

## 分镜阶段就「少而长」（视频档位感知）

视频模型按**最短档位**计费（H3 最短 5 秒），所以「3 秒的镜头」其实也要付 5 秒的钱。
与其事后合并，不如**生成分镜时就把档位写成硬约束**：

```bash
# 不需要配置：有真实 provider 时自动从模型能力表读档位（5~15 秒）
VIDEO_SHOT_TARGET_SEC=0     # 0 = 自动取最短档位 × 1.6（H3 → 约 8 秒）
VIDEO_MAX_SHOT_SEC=0        # 0 = 用模型最长档位
```

对每一场戏，先算出**该拆几个镜头**（每个镜头都 ≥ 最短、≤ 最长，并尽量贴近目标时长），
再把「本场只拆 N 个、每个约 X 秒」写进给 LLM 的提示里：

| 场次时长 | 档位 5~15s | 拆几个镜头 | 计费 |
|----------|-----------|-----------|------|
| 3 秒 | 5~15s | 1 | 5 秒（按 5 秒排，不浪费） |
| 9 秒 | 5~15s | 1 | 9 秒 |
| 20 秒 | 5~15s | 2 | 20 秒 |
| 45 秒 | 5~15s | 5 | 45 秒 |

生成后还有一道**确定性兜底**（`apply_duration_tier`）：镜头数超了就按「不丢对白」的规则合并掉，
再把本场时长重新分配到各镜头；实在合不动（不同说话人的对白）就保留并写进 `tier_warnings`
—— 宁可多几个镜头，也不丢谁的声音。`storyboard.duration_tier` / `tier_warnings` 会随分镜一起落盘，
UI 也会显示（`每镜都不短于 5 秒` + `本片 N 镜 · 计费 M 秒 ≈ $X`）。

对现有项目（15 镜 / 45 秒 / 5 场）的效果：

```
旧分镜：15 镜 × 3 秒 → 计费 75 秒 → $9.75
新分镜： 5 镜 × 9 秒 → 计费 45 秒 → $5.85   （省 $3.90，且没有浪费的档位差）
```

> 代价是镜头语言变粗（每场一刀 vs 多刀）。想要更碎的剪辑就把 `VIDEO_MIN_SHOT_SEC=1`
> 或把 `VIDEO_SHOT_TARGET_SEC` 调小 —— 工具负责把代价标出来，取舍由你定。

## 记账口径（别让账本骗你）

成本面板里的视频花费有**三个数**，任何一个对不上都说明有问题：

| 字段 | 含义 |
|------|------|
| `video_actual_usd` | 片段上 provider 报的实测扣费合计（`usage.cost`，最权威） |
| `video_ledger_usd` | 账本里视频阶段记了多少 |
| `video_unrecorded_usd` | 还没入账的部分；> 0 时 UI 会显示「漏记 $X」+ 一键补齐 |

记账用的是**条目上的「已记账」标记**（`VideoClip.cost_recorded` / `AudioClip.cost_recorded`），
不是「和上次清单做集合差」—— 后者在「换 provider 全部重跑」「LLM 改写文案后重新合成」这类
**同 id 重跑**场景下会静默漏记（v0.12 修的真实事故：花了 $9.75，账本只有 $0.0011）。
重新生成/重新合成时会清掉标记（重跑就是重新计费），记账后打标记并落盘，
所以「产物已落盘、账本还没写」时进程挂掉也不会重复计费。

```bash
# 给老版本漏记的项目补齐（幂等：不调 API、不动媒体、不动状态）
curl -X POST localhost:8000/api/projects/<id>/cost/reconcile
```

## 花钱之前先看账（preflight）

真实视频模型按秒计费、还有最短档位，所以「分镜 45 秒」经常不是「买 45 秒」：

```
15 个镜头 × 平均 3 秒 = 分镜 45 秒
H3 最短 5 秒/镜头 → 实际计费 15 × 5 = 75 秒 × $0.13 = $9.75   ← 多付了 66%
```

`GET /api/projects/{id}/preflight` 把这件事在动手前说清楚，UI 在点「生成视频片段」前也会弹确认：

- 这一跑会生成几个片段、**按模型档位实际计费多少秒**、单价（优先问 provider，问不到才用配置）、预估 USD
- **换 provider / 模型会不会把已就绪的片段全部重跑**（按钮文案会跟着变成「重新生成全部 N 个片段」）
- 预算够不够、本次之后还剩多少、不够还差多少（不够就直接不提交）
- **合并短镜头能省多少**（`merge_plan`），并给出可执行的一键操作

```bash
# 预览（只算不写）：镜头数、时长、省多少钱、会不会丢旁白
curl -X POST localhost:8000/api/projects/<id>/storyboard/merge-shots -d '{}' -H 'Content-Type: application/json'

# 真的合并（已有可用片段时必须带 force=true —— 那意味着已付费的片段会作废）
curl -X POST localhost:8000/api/projects/<id>/storyboard/merge-shots \
     -d '{"dry_run": false, "min_sec": 5}' -H 'Content-Type: application/json'
```

合并的规矩（安全优先）—— 一个镜头能不能被吸收，只看**对白会不会被弄丢**：

| 被吸收的镜头 | 能否吸收 | 原因 |
|--------------|----------|------|
| 没有对白 | ✅ | 旁白在主镜头需要时接过去，否则记录丢弃 |
| 有对白，主镜头一个字都没有 | ✅ | 对白直接接管这条配音 |
| 有对白，且主镜头是**同一说话人** | ✅ | 两句并成一条配音（缺句号会自动补） |
| 有对白，主镜头已有旁白 / 说话人不同 | ❌ | 一条配音只能有一个声音 |

- 顺序不能乱：只合并**连续**的镜头
- 合并后镜头变少 → 片段/配音/成片全部作废，状态回到「分镜已确认」
- 默认 `dry_run=true`；已有可用片段时默认拒绝（`force=true` 才动）

真实项目实测（15 镜 / 45 秒 / H3）：

```
合并前：15 个片段 → 计费 75 秒 → $9.75
合并后：12 个片段 → 计费 64 秒 → $8.32   （省 $1.43，一条台词都没丢）
```

省的钱来自「3 秒的镜头也要买 5 秒」：镜头变少，浪费的档位差就变少。想省更多就得在**分镜阶段**
就少分几个镜头（那是创作决定，工具只把账算给你看）。

> 取舍很清楚：**少而长**更省钱，**多而短**镜头语言更丰富。工具只把账算给你看，不替你决定。

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

## 字幕：为什么「有字幕」却看不见

`soft` 模式把字幕写进 mp4 的 **mov_text 软字幕轨**（`ffprobe` 能看到 `codec_type=subtitle`），
同时导出 `output/subtitles.srt`。问题是**浏览器不渲染 mov_text，也完全不认 SRT**：

| 场景 | soft（mov_text） | burn（烧进画面） |
|------|------------------|------------------|
| Chrome / Safari 的 `<video>` | 看不见 | 看得见 |
| 微信 / 抖音 / 手机相册 | 看不见 | 看得见 |
| QuickTime / VLC | 要手动开字幕菜单 | 看得见 |
| 二次编辑 | 可开关、可换字幕 | 已经烧死，改不了 |

所以本项目的 Web 面板**永远额外导出一份 `output/subtitles.vtt`**，播放器用
`<track kind="subtitles">` 叠加显示 —— 这条路径和成片里有没有硬字幕无关，
`soft` / `burn` / `none` 都能预览（老项目没有 `.vtt` 文件时，
`GET /api/projects/{id}/subtitles.vtt` 会就地由 srt 转换，不需要重新合成）。

### burn 需要 libass，而 Homebrew 的 ffmpeg 常常没有

`brew install ffmpeg` 的精简构建里没有 `--enable-libass`，`-filters` 里查不到
`subtitles` 滤镜，于是「选了 burn 却被静默降级成 soft」。v0.13 起改成
**能力感知的 ffmpeg 选择**：

1. 先看主 ffmpeg（`FFMPEG_PATH` → `PATH`）有没有 `subtitles`
2. 没有就按候选顺序找下一个：`imageio-ffmpeg` 的便携版**带 libass**（`pip install imageio-ffmpeg`）
3. 找到就用它烧，并在 `compose_report.warnings` 与 `subtitle_ffmpeg` 里写清楚用了哪个二进制
4. 都没有才降级，并且一定留下「没有 libass」的告警

```bash
# 看当前能不能烧（以及会烧的话用哪个二进制）
curl -s localhost:8001/api/health | python3 -m json.tool | grep ffmpeg
#   "ffmpeg_can_burn_subtitles": true,
#   "ffmpeg_burn_path": ".../imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1"
```

不想要这个自动行为就 `FFMPEG_AUTO_UPGRADE=false`（保持「主 ffmpeg 说不行就不行」）。

## 角色一致性：从「AI 生成定妆照」到每个镜头

真实视频模型（含 H3）**每个镜头都是独立生成**的：不给参考图，它每次都会重新「想」一张脸，
于是同一个人在 15 个镜头里能长出 15 张脸。H3 的 `seed` 是 `false`，所以**唯一的身份锚点就是参考图**。

链路（v0.13 完整可用）：

```
角色设定（bible.characters[].visual_prompt）
   └─ AI 生成定妆照 ──> projects/<id>/refs/char_1.png
        （图片模型：BYTEDANCE-SEED/SEEDREAM-4.5，$0.04/张）
             └─> 每个镜头作为 input_references 传给视频模型（+$0.04/镜头）
```

- **AI 生成**：`POST /api/projects/{id}/characters/{cid}/reference/generate`
  （`force=false` 时已有图就跳过，不花钱；`use_reference=true` 可基于已有图再出一个角度）
- **批量补齐**：`POST /api/projects/{id}/characters/references/generate`（默认只补缺失的，
  单角色失败不影响其它角色）
- **先看价再决定**：`GET /api/projects/{id}/characters/references/plan` 只算不写——
  缺几张、预估多少钱、prompt 长什么样，**不调 API**
- **也可以自己上传**：`POST .../reference`（base64），UI 角色卡片上直接选文件

```bash
# 4 个角色大约 $0.16，先看 plan 再决定
curl -s localhost:8001/api/projects/$PID/characters/references/plan | python3 -m json.tool
curl -s -X POST localhost:8001/api/projects/$PID/characters/references/generate \
     -H 'Content-Type: application/json' -d '{}'
```

记账进 `image` 阶段（按张数 + provider 实报 `usage.cost`），预算不足会直接 402 拒绝开工。

生成定妆照的 prompt 有两条踩过坑的硬规则（`build_portrait_prompt`）：

- **一个中文字符都不许有**：图片模型会把 prompt 里的文字画进画面（真实事故：
  中文名和风格名被当成海报标题 / 年龄标注印在图上）。所以名字、中文 `role`、
  `style.genre` 都不进 prompt（`role` 映射成 protagonist / antagonist / …），
  并显式写上 `no text, no typography, no labels, ... no poster layout`。
- **不说 "character design sheet" / "poster"**：这类词会把画面带成带排版标注的海报，
  而不是一张能锁住五官的干净证件照。

另外 `refs/<cid>.*` 里有文件但圣经没登记（并发写坏 / 手工拷贝）时，
`references/plan` 会把它列进 `orphans`，生成入口会**免费补登记**，不会重复扣钱。

### 免费的那一半：文本锚点

参考图是「锁脸」的硬手段，但**没有参考图时也别让文字描述漂移**：分镜 LLM 有时把
`character.visual_prompt` 抄进 `video_prompt`、有时只写一句 "a young woman"。
`build_prompt()` 现在会读圣经，把在场角色**没被提到过的**外貌描述确定性地补进 prompt
（提到了就跳过，不重复），并追加 `same characters as before, consistent face and hairstyle`。
这条不花钱，对 mock/任何 provider 都生效。

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
| POST | `/api/projects/{id}/storyboard/merge-shots` | 合并短镜头省钱（默认 `dry_run` 只预览） |
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
| GET | `/api/projects/{id}/final` · `/subtitles` · `/subtitles.vtt` | 下载成片 MP4 / 字幕 SRT / WebVTT（浏览器 `<track>` 预览用，老项目就地转换） |
| GET | `/api/projects/{id}/preflight` | **成本前置**：镜头数 / 计费秒数 / 单价 / 预估 USD / 预算投影 / 省钱建议 |
| GET | `/api/models` | 可选模型 / 单价 / 预算（前端下拉用） |
| GET | `/api/projects/{id}/cost` | 成本账本：按阶段的花费、token、秒数、字符 + 预估 |
| POST/GET/DELETE | `/api/projects/{id}/characters/{cid}/reference` | 角色参考图（base64 上传 / 下载 / 删除） |
| GET | `/api/projects/{id}/characters/references/plan` | 角色图计划：缺几张 / 预估花费 / prompt 预览（**不调 API**） |
| POST | `/api/projects/{id}/characters/{cid}/reference/generate` | **AI 生成该角色定妆照**（`force` 覆盖已有图，`use_reference` 走图生图） |
| POST | `/api/projects/{id}/characters/references/generate` | **批量补齐缺失的角色图**（单角色失败不影响其它） |
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
- **预估与扣费同一个口径**：`preflight`、预算门控、真正下单三处都走 provider 的
  `price_per_second()` + `planned_seconds()`；换 provider 时三处**都**按「全部重跑」算
  （曾经只有真正下单那处知道要全跑，预算门控被绕过 —— 见 FIXES v0.10）
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
11. **合并短镜头是有损的**：镜头变少 = 镜头语言变粗；同说话人的两句会并成一条配音（中间没有
    剪辑点），被吸收镜头的旁白在主镜头已有文案时会丢（报告里逐条列出）。
    它是省钱工具，不是「优化」，花钱与艺术之间的取舍由你定
12. **preflight 是估算**：LLM 文案的 token 数按经验值拍，真实扣费以 provider 的 `usage` 为准；
    视频按「档位 × 单价」算，不含未来的调价，**也不含参考图附加费**
    （H3 的 `reference_images` = $0.04/镜头，账单以 provider 实报为准）；
    角色图另按 `IMAGE_PRICE_USD` 计
13. **分镜档位感知会改变创作结果**：按最短档位少分镜头意味着剪辑点变少；不需要就
    `VIDEO_MIN_SHOT_SEC=1` 关掉（mock provider 本来就没有档位，不受影响）
14. **成片可能比分镜长**：片段真实时长由视频模型决定（H3 最短 5 秒），分镜写 3 秒也会拿到
    5 秒的画面，成片因此变长（也因此按 5 秒计费）—— 想严丝合缝就用分镜档位感知
15. **角色一致性靠参考图，不靠 seed**：H3 的 `seed` 是 `false`，没有参考图时同一个角色在
    不同镜头里就是不同的人（这不是 bug，是文生视频的固有行为）；参考图是按镜头额外计费的
    （H3 的 `reference_images` SKU = $0.04/镜头）
16. **AI 生成的定妆照不等于本人**：它只是「把这个角色的文字设定画成一张稳定的脸」，
    第一次生成的脸就是这一版片子里的脸；不满意就重画一张（换图意味着已生成的片段要对齐重跑）
17. **烧字幕是重编码**：`burn` 会让整片重新编码一遍（比 `soft` 慢、且视频流不再是无损 copy）；
    需要「随时刻换字幕/多语言」就别烧，用 `soft` + `.vtt` 预览

## 安全

- `.env` 不要提交，也不要贴到任何地方；泄漏后请立即到 OpenRouter 轮换
- 所有对外异常/日志都经过 `scrub_secrets()` 脱敏，密钥不会进入日志、前端或报告
- 服务默认只监听 `127.0.0.1`；对外暴露时请设置 `API_TOKEN`
