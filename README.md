# videogen

剧本生成 + AI 短剧自动化流水线：`创意输入 → 故事圣经 → 逐场剧本 → 分镜 → 视频片段 → 配音 → 成片（FFmpeg 合成 + 字幕）`

> 设计文档见 [DESIGN.md](DESIGN.md)；历史问题审计见 [REVIEW.md](REVIEW.md)；本轮修复记录见 [FIXES.md](FIXES.md)。

## 当前实现状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| 项目 / 文件存储 | ✅ | `projects/{id}/*.json`，原子写 |
| 故事圣经 Story Bible | ✅ | 角色 / 设定 / 关键事实 / 风格，LLM 生成 |
| **长篇剧集大纲（20+ 章）** | ✅ | 先出全篇骨架（因果链 / 章末钩子 / 预算），再分批扩写正文；**按章选做**：只把勾选的章展开成剧本 |
| 逐场剧本 + World State | ✅ | 逐场注入 bible + 前场摘要 + 世界状态，逐场落盘 |
| 一致性校验 | ✅ | 人名/数字/时间线/时长 + 分镜层校验 |
| 分镜 + 英文 video_prompt | ✅ | 每场 2-5 镜，时长自动对齐剧本 |
| 分镜档位感知（少而长） | ✅ | 有真实 provider 时按最短计费档位决定每场几个镜头（H3 → 每镜 ≥5 秒），从源头避免浪费 |
| 视频片段生成 | ✅ | `VideoProvider` 抽象：`mock`（免费占位片段）+ `openrouter`（**真实视频模型**，默认 MiniMax H3） |
| 配音 TTS | ✅ | `TTSProvider` 抽象：`edge`（免费，音色池小）/ `openrouter`（云端 gpt-audio-mini，**支持自然语言念法**）/ `mock` |
| 每句念法（情绪表演） | ✅ | LLM 看画面逐镜写念法：`instruction`（云端自然语言）+ `rate/pitch`（Edge），`AUDIO_LLM_SCRIPT=perform` 可只给念法不改台词 |
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
| 背景/场景一致性 | ✅ | `Shot.location` + 场景 `visual_prompt` 锚点注入 + `VIDEO_BACKGROUND_DIRECTIVE`（禁止空墙） |
| 口型对齐（同一句话） | ✅ | 台词进视频 prompt（`VIDEO_PROMPT_DIALOGUE`），模型原生表演与我们的配音落在同一句上 |
| 逐帧口型（按配音重画嘴部） | ✅ | 可选后处理 `LIPSYNC_ENGINE=wav2lip-onnx`：独立环境、本地 CPU、$0；换引擎（LatentSync 等）只需填 `LIPSYNC_CMD` |
| 画面运动信息进念法 | ✅ | 本地实测运动曲线（纯标准库、免费）交给配音导演：「动作最猛处最重，静止段放轻」 |
| 名字 ↔ 国家 ↔ 脸对齐 | ✅ | 名字推国籍（中日韩/欧美）→ 该国的「平均脸/主角脸」锚点进图片与视频 prompt；一部片子一张脸谱 |

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
| `perform` | **一个字都不改文案**，只给逐句念法（云端 TTS 用自然语言） | 一次 LLM 调用/场 |

除了抽帧，还会把**画面运动实测**一起发过去（`AUDIO_LLM_MOTION=true`，本地计算、免费）：

```
画面运动（本地实测：6fps 采样 64×64 灰度，相邻帧平均变化，共 31 帧 / 5.00s）：
- 整体画面：整段轻微（平均强度 0.0281，峰值 1.67s）；起伏：1.00–1.83s 强烈起伏（平时的 2.5 倍）
- 说话人区域（画面下半部中央）：1.17–1.83s 强烈起伏（平时的 2.4 倍）
- 念法建议：动作最猛在 1.17–1.83s（强烈起伏），这段把音量和语速顶上去，让声音跟着动作走
```

为什么要加它：**静态抽帧只知道画面里是谁、在哪，说不出「一句话里哪一段动得最猛」**，
于是念法从头到尾一个速度、和画面节奏对不上。运动曲线补上这一块，念法才能同节奏。

判读分两级（阈值是在 8 个真实片段上量出来的）：**整段活跃度**用曲线均值分档
（真实片段实测 0.008~0.090，对应 几乎静止 / 轻微 / 明显 / 剧烈），
**起伏窗口**以中位数为基线找 `≥1.6×` 的连续窗口（真实片段峰均比 1.36~4.07）。
只看相对峰值是不够的 —— 说话的人整段都在动、峰均比只有 1.5，按相对峰值分档会把一整段
全标成「剧烈运动」，等于没说。

```bash
# .env：让 LLM 给所有没台词的镜头补旁白（关掉抽帧更便宜）
AUDIO_LLM_SCRIPT=narration
AUDIO_LLM_VISION=true
AUDIO_LLM_FRAMES_PER_SHOT=1
AUDIO_LLM_MOTION=true     # 画面运动实测进念法（纯本地，免费）
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

### 实测：整片 15 个镜头全量重跑后的结果（2026-09-11）

`scene_1_shot_3` / `scene_2_shot_3` / `scene_4_shot_4` 三个镜头的对照实验确认参考图有效后，
把剩下 12 个旧片段也全部重跑，整片 15/15 都带上了参考图：

| | 结果 |
|---|---|
| **主角林悦（13 个镜头）** | 从「素颜便装」到「T 台华服」到「桂冠逆袭」，**全片同一张脸**（发色/脸型/五官一致），变化只来自妆容与服装 |
| 双人镜头（`s1_2` / `s4_1` / `s4_3`） | 主角稳定；**第二个角色改善有限**（见下面限制 17） |
| 配角单人镜头（`s3_2` 赵总 / `s3_3` 李薇儿） | 与各自定妆照接近；赵总在 `s3_2`（单人）与 `s4_1`（双人）之间仍有可见差异 |

花费：整轮 **$10.27**（首轮 3 镜 $1.95 + 12 镜 $8.32，其中含 2 个 7 秒镜头；
另有 4 个片段因本机 DNS 抖动提交失败，**失败不计费**，补跑 $3.12）。
账本口径：`video_ledger_usd $20.02`、`video_unrecorded_usd $0.00`（无漏记）。

一个值得记下的操作细节：**compose 在片段不齐时直接拒绝**（400「视频片段不完整」），
所以那次 DNS 抖动没有产出一条缺料的成片 —— 补跑完 15/15 后才重新合成。

### 参考图为什么有效

同一个项目跑了一次对照实验：把 3 个含林悦正脸的镜头（`scene_1_shot_3` / `scene_2_shot_3` /
`scene_4_shot_4`）重置为待生成后重跑 —— 这是**唯一**变量（prompt / 模型 / 秒数都没变）：


同一个项目跑了一次对照实验：把 3 个含林悦正脸的镜头（`scene_1_shot_3` / `scene_2_shot_3` /
`scene_4_shot_4`）重置为待生成后重跑 —— 这是**唯一**变量（prompt / 模型 / 秒数都没变）：

| | 修复前 | 带参考图重跑后 |
|---|---|---|
| `scene_1_shot_3` | 棕色齐肩发、圆脸 | 黑短发，与定妆照同一张脸 |
| `scene_2_shot_3` | 黑色高马尾、脸型不同 | 同一张脸（只是表情/光影不同） |
| `scene_4_shot_4` | 另一个人 | 同一张脸 + 后期造型（桂冠/妆容），人物弧光成立 |

花费 $1.95（3 × 5 秒 × $0.13），preflight 预估 $1.95 **与实际完全一致**。
对比图：`projects/<id>/consistency_before.png` / `consistency_after.png`。

注意：H3 的模型元数据里列了 `reference_images` SKU，但这次 3 个带 `input_references`
的镜头实收仍是 $0.65/镜头（= 5 秒 × $0.13），**没有观测到附加费** ——
账本一律以 provider 实报的 `usage.cost` 为准。

生成定妆照的 prompt 有两条踩过坑的硬规则（`build_portrait_prompt`）：

- **一个中文字符都不许有**：图片模型会把 prompt 里的文字画进画面（真实事故：
  中文名和风格名被当成海报标题 / 年龄标注印在图上）。所以名字、中文 `role`、
  `style.genre` 都不进 prompt（`role` 映射成 protagonist / antagonist / …），
  并显式写上 `no text, no typography, no labels, ... no poster layout`。
- **不说 "character design sheet" / "poster"**：这类词会把画面带成带排版标注的海报，
  而不是一张能锁住五官的干净证件照。

角色设定里没写人种时，整组角色容易「各长各的」——用 `IMAGE_PROMPT_SUFFIX` 统一基调
（英文，例如 `East Asian cast, Chinese television drama`）；它是追加到 prompt 末尾的额外约束。

另外 `refs/<cid>.*` 里有文件但圣经没登记（并发写坏 / 手工拷贝）时，
`references/plan` 会把它列进 `orphans`，生成入口会**免费补登记**，不会重复扣钱。

### 名字 ↔ 国家 ↔ 脸：中文名就该长中国脸

**问题**：模型接到 `A 28-year-old woman` 时按自己的先验画 —— 那个先验偏向**欧美化**的五官。
于是「林悦」长了一张欧美脸，或者一个剧组里一半日式一半欧美。

**为什么不能直接把名字写进 prompt**：图片模型会把 prompt 里的中文**画进画面**
（真实事故：中文名「林悦」+ 风格「都市复仇」被当成海报标题印在图上）。
所以名字必须先在本地翻成**英文的外貌锚点**：

```
Chinese cast — East Asian features of a typical Chinese television drama lead,
an average everyday Chinese face (straight black hair, dark brown eyes, warm light-tan skin),
not mixed-race and not Westernised
```

**怎么从名字判断国家**（`utils/region.py`，纯本地、免费、有置信度）：

| 线索 | 判据 | 置信度 |
|---|---|---|
| 假名 | 名字里有平假名/片假名 | 0.95 日本 |
| 谚文 | 名字里有谚文 | 0.95 韩国 |
| 文本提示 | 描述里出现「日本」「东京」「首尔」… | 0.9（最硬） |
| 日本姓氏 | 开头 2-3 字命中日本姓氏表（田中/佐藤/铃木…） | 0.8 |
| 罗马字姓氏 | 拉丁转写的日文/韩文姓氏表 | 0.75 / 0.7 |
| 中文姓氏 | 首字命中中国姓氏表 | 0.6 |
| **共用姓** | 金/李/林/姜/崔…（中日韩都常见） | **0.4 → 跟随整个剧组** |
| 拉丁兜底 | 姓氏不在表里 | 0.6 欧美 |

两层「对齐」：

1. **一部片子一张脸谱**：全剧组按「置信度 × 角色重要性（主角/反派 2、配角 1、龙套 0.5）」
   投票选出主国家；分不出国籍的角色（`金敏俊`）跟随它 —— 在国剧里是中国人，在韩剧里是韩国人。
2. **说得清的按自己来**：`田中由纪` 即使在全是中国人的剧组里也按日本画（置信度 0.8 够硬）。
   角色自己写了 `region` 字段、或配置里 `IMAGE_REGION_OVERRIDE=cn/jp/kr/west`，优先级最高。

三层落地（`region` 字段会写进故事圣经，人工可改）：

- **圣经生成**：提示词要求「名字、地区、外貌三者必须对齐」，并让模型填 `region` 字段
- **定妆照**：`build_portrait_prompt` 注入该角色的国家锚点（角色已写明人种 / `IMAGE_PROMPT_SUFFIX`
  里写了人种时**不叠加**，避免自相矛盾）
- **视频 prompt**：`appearance_anchor` 补一句整个剧组的人种锚点 —— 没有参考图时，
  这是唯一能拦住「中文名长出欧美脸」的东西

```ini
IMAGE_REGION_AUTO=true      # 关掉就完全不介入
IMAGE_REGION_DEFAULT=cn     # 名字看不出国籍时的兜底
IMAGE_REGION_OVERRIDE=      # 强制整片按某国（留空 = 自动）
```

顺带修掉一个真 bug：`appearance_anchor` 以前会把**中文名**写进视频 prompt
（`林悦: A young woman…`），同样有被画成画面文字的风险，现在只给外貌、不给名字。

**局限**：这是**启发式**，不是人口学判断 —— 日本名写成汉字且不在姓氏表里（`森鴎外`）、
生僻拼音姓（没进表）都可能判错；共用姓必须靠同剧其他角色或人工指定。
名字与设定本来就不一致时（中文名 + 「日本留学生」人设），以**描述里的国家**为准。

### 免费的那一半：文本锚点

参考图是「锁脸」的硬手段，但**没有参考图时也别让文字描述漂移**：分镜 LLM 有时把
`character.visual_prompt` 抄进 `video_prompt`、有时只写一句 "a young woman"。
`build_prompt()` 现在会读圣经，把在场角色**没被提到过的**外貌描述确定性地补进 prompt
（提到了就跳过，不重复），并追加 `same characters as before, consistent face and hairstyle`。
这条不花钱，对 mock/任何 provider 都生效。

## 配音：每个角色有自己的声音

`Character.voice_id` + `audio_manifest.voice_map` 一起决定「谁用什么声音」，选角规则在
`services/voice_catalog.py`（**确定性**：同样输入永远同样选角）：

1. **性别线索分强弱**：显式性别词（男/女/woman/man，权重 2）> 亲属称谓（姐/父/母亲，权重 1）。
   **身份词（反派 / 总裁 / 主角 / 导师）不参与性别判定** —— 真实事故：`_MALE_HINTS` 里混进了
   「反派」，22 岁的女反派因此被配了男声 `zh-CN-YunxiNeural`
2. **按性别 → 角色定位 → 年龄 → 口音代价打分**，先在「同性别、未占用」的音色里挑最好的
3. **绝不给两个角色同一个声音**：音色池用尽时复用最合适的那个，并用 `voice_delivery`
   （音高/语速）区分，同时在 `audio_manifest.warnings` 里写明原因
4. **年龄明显不匹配会重配**：真实样本里 45 岁的杂志主编挂着「清朗少年感」的云希 →
   自动换成沉稳的云扬（音色池空了就保留原音色，靠念法区分）
5. 选中的音色**写回圣经** `voice_id`，角色卡片上能看能改，重跑不换人

```bash
# 只看选角、不花钱（不调 TTS）
curl -s localhost:8001/api/projects/$PID/voices | python3 -m json.tool
```

**Edge TTS 中文只有 8 个音色**（4 女含 2 个方言、4 男），这是硬约束：一部戏里超过
2 个女角色就必然要「复用 + 念法区分」。想彻底避免，只能接入付费 TTS 或手工挑音色。

片子已经 `done` 之后想换音色：`POST /api/projects/{id}/audio/reopen` 退回配音阶段
（成片会作废），改完重做配音再合成 —— 没有这个入口时状态机会直接拒绝重做配音。

### 「声音太像 AI」的两个真实原因

1. **所有台词的念法都是空的**。`voice_direction` 只在配音文案模式（`AUDIO_LLM_SCRIPT`）
   里才会生成，而默认模式是 `off` —— 于是一整部戏的台词都用同一个语速/音高念出来，
   没有情绪、没有停顿。这不是 TTS 的锅，是**没给表演指令**
2. **Edge 的表达力上限就在那里**：`rate` / `pitch` 只能调「快慢高低」，
   调不出「忍着哭」「冷笑」「心虚」，而且中文没有 SSML 风格（`mstts:express-as`）可用

所以 v0.15 加了**云端配音**（`TTS_PROVIDER=openrouter`，用你已有的 OpenRouter key）：

```bash
# .env
TTS_PROVIDER=openrouter
OPENROUTER_TTS_MODEL=openai/gpt-audio-mini   # $2.4/M 音频 token，整片通常 < $0.05
TTS_AUDIO_FORMAT=mp3
AUDIO_LLM_SCRIPT=perform                     # 只给念法，不改台词
AUDIO_LLM_VISION=true                        # 让模型看着画面给念法
```

- 请求走 `POST /chat/completions` + `modalities:["text","audio"]`，`audio.voice` 是
  **云端音色名**（nova / shimmer / coral / sage / alloy / echo / onyx / ash / verse /
  ballad / fable），与 Edge 的名字**完全不通用**
- **念法用自然语言写**：`VoiceDirection.instruction`，例如
  「压抑的哭腔，语速慢，句尾发颤，像在忍着不哭」。它作为系统提示里的表演要求发给模型 ——
  这是 Edge 的 `rate/pitch` 做不到的那一层
- 选角仍然用 Edge 那份**带元数据的音色表**（性别/年龄/音色性格）打分，再把选中的音色
  **换成同气质的云端音色**；云端池更大（女 5 / 男 6），Edge 那边「音色用尽只能复用」的
  角色到云端常常还能分到没人用的音色；换 provider 会把旧音色名自动重配（否则接口 400）
- **成本按 provider 报的 `usage.cost` 记账**，不靠估算（`audio_manifest.clips[].provider_usd`）
- 模型回读文本（`transcript`）会留痕：它自己改词/加解释时能查出来

新增 `AUDIO_LLM_SCRIPT=perform` 模式：**一个字都不改台词**，只根据画面给每个镜头写念法。
改台词和给念法是两件事，混在一起做容易把好台词改坏。

```bash
# 只要念法（不改台词、不动文案），仍需抽帧发给模型
curl -s -X POST localhost:8001/api/projects/$PID/audio/generate \
  -H 'Content-Type: application/json' \
  -d '{"script_mode":"perform","use_vision":true}'
```

## 表演与镜头：表情自然 / 动作流畅

原来视频 prompt 里**丢了两个关键信息**，v0.14 补上：

| 分镜字段 | 原来 | 现在 |
|---|---|---|
| `camera`（如「近景 · 推」） | **完全没进 prompt** —— 运镜信息 100% 丢失 | 翻成英文镜头指令：`medium close-up, slow dolly in` |
| `mood`（如「委屈，不甘」） | 中文直接拼进英文 prompt（模型读不懂，还有被画进画面的风险） | 映射成英文情绪词：`wronged, resentful`；没收录的词直接丢弃 |
| 表演/连贯性 | 无 | 每个镜头追加 `VIDEO_PERFORMANCE_DIRECTIVE`：克制的微表情、自然眨眼呼吸、平滑连续运动、稳定构图、单镜到底不切、不许变形，避免夸张舞台腔 |
| 风格 | 无 | 可选 `VIDEO_STYLE_PROMPT`（英文） |

分镜提示词也同步收紧：情绪镜头优先中景/近景（大特写会让模型把表情演成舞台剧）、
一个镜头只写一个连续动作、表演用克制的词（subtle / restrained）。

### 口型对不上：因为我们从来没告诉模型该说什么

**根因**：视频 prompt 里**完全没有台词**。H3 自带音频（`generate_audio`），它会**自己编一段话**
在说 —— 嘴按它编的那句话动；我们再把 TTS 配音叠上去，两段话的字都不一样，口型必然对不上。
（更早的版本还会把它编的那段话当「环境音」以 -20dB 混进去，等于两个人在说话。）

v0.15 的修法是把台词写进 prompt：

```
林悦 speaks this exact Mandarin line: "薇儿，为什么？" — natural lip-sync matched to the
Mandarin speech, mouth movement and breathing follow the line, no on-screen text or captions
```

这样模型的原生表演与我们的配音落在**同一句话**上。默认开启（`VIDEO_PROMPT_DIALOGUE=true`），
可以关掉回到「让模型自己编」。

三种方案的真实对照（同一个镜头、同一句话，各 $0.65，共 $1.95；原生音轨抽出来做过转写）：

| 方案 | 模型原生说出口的话 | 原生说话区间 | 我们的配音区间 | 口型 |
|---|---|---|---|---|
| ① 现状：prompt 无台词 + TTS 配音 | **「你怎么能这样对我」** | 2.76–3.98s | 0.02–1.34s | ❌ 模型在说**另一句话**，嘴 2.7 秒后才动 |
| ② 台词进 prompt + TTS 配音 | 「为什么」 | 1.22–4.60s | 0.02–1.34s | ⚠️ 话对上了，开头仍错位 ≈1.2 秒 |
| ③ 台词进 prompt + 模型原生语音 | 「薇儿，为什么」 | 0.98–3.72s | 同一轨 | ✅ 零错位，但音色由模型每镜头决定 |

### 采用的方案：②+ 本地口型对齐（不换音色、不重烧视频）

③ 的口型最好，但与「每个角色有自己的声音」冲突。所以最终落地的是 **②再加一层本地对齐**：
合成时把配音**延后**到「画面里的人开口」的那一刻（`adelay`）。

- 判据在 `utils/audio_align.py`：用**相对自身峰值的能量阈值**找说话起点。
  不能用 ffmpeg `silencedetect` 的绝对 dB —— 实测片头底噪约 -45dB，
  绝对阈值会把「还没说话」判成「已经在说」（真实样本里把 2.76s 的开口误判成 0）
- 上限 `COMPOSE_MOUTH_ALIGN_MAX_SEC`（默认 1.5 秒），且必须满足
  `起点 + 配音时长 ≤ 片段时长`：尾音会被切掉时宁可不对齐，也不能丢字
- 全片 15 镜重合成实测：**10/15 需要对齐，延后 0.08~1.20 秒**，成片仍是 67.0 秒（没切掉任何尾音）

```
COMPOSE_ALIGN_DUB_TO_MOUTH=true    # 口型对齐（默认开）
COMPOSE_MOUTH_ALIGN_MAX_SEC=1.5    # 最大延后量
```

**局限**：这是「起点对齐」，不是逐帧对齐 —— 一句话内部的节奏仍由 TTS 决定。
样本与测量数据：`projects/<id>/ab_lipsync/`（`lipsync_compare.mp4` + 四份原始片段 + `measure.json`）。

### 逐帧口型：按配音把嘴逐帧重画（v0.16）

起点对齐解决不了「一句话内部的节奏」，所以要**逐帧重画**。落地方式是可选后处理：
引擎跑在**独立环境**（主环境一个依赖都不加），按镜头调用。

```
LIPSYNC_ENGINE=wav2lip-onnx      # none（默认）| wav2lip-onnx | cli
LIPSYNC_SCOPE=dialogue           # 只做「说话人就在画面里」的镜头（推荐）
```

- **内置引擎**：Wav2Lip 的 ONNX 版，`onnxruntime + numpy + opencv`（约 120MB，
  **不需要 torch**），Apple Silicon 的 CPU 上 5 秒 1440×2560 的镜头约 5 秒。
  装在 `.venv-lipsync`，用 `tools/lipsync/install.sh` 一键装 + 自检。
- **cli 引擎**：填一个带 `{video} {audio} {out}` 的命令模板就能换成 LatentSync /
  MuseTalk 这类要 CUDA 的引擎，其余不用改。
- **镜头筛选**：默认只做「有台词**且**说话人就在画面里」的镜头 —— 旁白镜头里那个人
  本来就不该跟着旁白动嘴，说话人不在画面里（过肩/听者反应）替他动嘴更是穿帮。
- **与起点对齐互斥**：做过逐帧口型的镜头**不再**把配音整体延后，否则嘴和声音会错开。
- 产物落 `output/lipsynced/`（重跑可复用），逐镜统计落 `output/lipsync_report.json`。

整片实测（`projects/e8e40d3d9d29`，11 个有台词的镜头）：

```
成功 11/11 个镜头     人脸检出率 100%     逐镜 4.7~6.2 秒，整片 62.5 秒（本地 CPU，$0）
成片 67.0 秒，音画差 +0.04 秒
```

**三个必须知道的坑**（写代码时都踩过，详见 `tools/lipsync/README.md`）：

1. Wav2Lip 的 6 通道输入必须是「**下半张脸抹掉 + 原图**」。两路都给原图时，模型会直接从
   输入里「抄」出嘴 —— 输出与输入最大差异只有 **0.011**，口型**静默失效**却毫无报错。
   现在脚本会统计 `mean_patch_change`，低于 0.01 直接判失败。
2. 用 `Path.resolve()` 转绝对路径会**跟随软链**：venv 的 `bin/python` 是软链，
   解析后就变成系统解释器、venv 被绕过（`No module named 'cv2'`）。改用 `os.path.abspath`。
3. 配音比画面短时不能「重复最后一帧 mel」（原版做法），否则嘴会停在最后一个口型上张着不动。
   这里补 0（静音 mel）→ 说完就闭嘴。

**局限**：模型只输出 96×96 的嘴，放大回 1440 宽偏软（已用「只贴改过的嘴唇 + 皮肤保留
原分辨率 + 高频细节叠回」缓解）；头姿/眼神/身体仍是原片段的节奏；要更清楚得接
GFPGAN/CodeFormer 或换 LatentSync。自动判据也试过：四种便宜的像素统计量连
「模型原生语音」这个基准都只有 r≈0.5、最佳滞后 0.1~0.25 秒，量不准嘴的开合，
所以没有做成通过/不通过，只保留引擎自检 + 人工看帧。


## 背景/环境：为什么画面里常常只有一面空墙

**根因**：故事圣经里每个场景都有 `visual_prompt`（「A bustling modern office with glass
partitions…」），但它**从来没进过视频 prompt** —— 分镜 LLM 只写一句
`The office lighting is harsh and cold.`，模型于是把背景画成一面空墙。

v0.15 补了三层：

1. **`Shot.location`**：分镜必须标出这一镜发生在哪个场景（loc id），系统校验它真实存在
2. **`location_anchor()`**：把该场景的 `visual_prompt` 作为 `setting: …` 注入视频 prompt；
   同一场景的多个镜头背景因此保持一致（和角色外貌锚点同一套机制，已出现过的不重复写）
3. **`VIDEO_BACKGROUND_DIRECTIVE`**（默认开）：每个镜头都追加「有层次、有陈设、有材质、
   有方向光、有景深，禁止空墙/纯影棚」的统一要求

分镜提示词也加了硬性要求：`environment` 必须写到**具体的可见物**，
只写 `in an office` 的 prompt 视为不合格。长篇大纲展开时还会把章里的环境线索
（`OutlineScene.environment`）与「这一场在整篇里的位置」一起带进分镜提示词。
真实项目里这一层立刻见效：`scene_1` 的环境线索是
「大学教学楼前，草坪修剪整齐，喷泉水花飞溅…阳光透过法国梧桐洒在红砖墙上」。

## 长篇剧集大纲：先写全篇，再挑章拍

**「剧情太简略、太平淡」的根因**：原来是「一个 45 秒短片 → 直接写 4 场 → 开拍」。
4 场的体量装不下钩子、转折、高潮与代价，模型只能写套话式的推进；而且每一场都是**孤立生成**的，
没有全篇因果链兜着，写出来自然是流水账。

v0.15 把创作拆成两步：

```
① 长篇大纲（一次调用给骨架，再按 3-4 章一批写正文）
   title / logline / synopsis / theme
   + 20 章：标题 · 一句话剧情 · 300-500 字正文 · 4-8 个情节点 · 章末钩子
            涉及角色/场景 · 建议场次 · 时长预算
② 选章展开：勾选想拍的 1-N 章 → 只把这几章展开成「分场大纲」→ 逐场生成剧本
```

为什么要分两阶段：20 章一次写完必然被输出长度截断（模型会开始糊弄），
所以**骨架一次给全**（保证因果链完整），**正文分批写细**（每批 3-4 章，写完即落盘，可中断续跑）。

- **因果链强制**：提示词要求「每一章都是因为上一章的某个结果才发生」「阻力要逐章升级」
  「至少 5 章要有不可逆的大翻转」；展开成场次时，全篇弧光与章内情节点都会进上下文
- **只对选中的章花钱**：视频是按秒计费的（H3 $0.13/秒），1 章 ≈ 2-3 场 ≈ 25-35 秒 ≈ **$3.5-4.5**。
  UI 上每个章卡片都标了场次/秒数，勾选后会实时算出「选中 N 章 ≈ X 秒 ≈ ≈$Y 视频费」
- **追加语义**：已经展开过的章不会重复生成（重复点「生成」不花钱）；之后想续写第 3 章，
  再加一章即可，前面的场次原样保留（新增场次会作废下游分镜与媒体）
- 项目的 `target_duration_sec` 会**自动对齐**到「已展开章的预算之和」，
  时长归一化、成本估算、结构校验都以它为准

```bash
# 1) 生成长篇大纲（20 章，只花 LLM 的钱 ≈$0.05）
curl -s -X POST localhost:8001/api/projects/$PID/story-outline/generate \
  -H 'Content-Type: application/json' -d '{"chapters":20}' | python3 -m json.tool

# 2) 只看已生成的大纲
curl -s localhost:8001/api/projects/$PID/story-outline | python3 -m json.tool

# 3) 只把第 1、2 章展开成剧本（已展开的章会被跳过）
curl -s -X POST localhost:8001/api/projects/$PID/script/generate \
  -H 'Content-Type: application/json' -d '{"chapters":["ch_1","ch_2"]}'

# 4) 想清空重来（会作废下游分镜与媒体）
curl -s -X POST localhost:8001/api/projects/$PID/script/generate \
  -H 'Content-Type: application/json' -d '{"chapters":["ch_1"],"force":true}'
```

没有长篇大纲的项目**保持老行为**（一次性分场大纲），所以老项目不受影响。

## 剧本严谨：两层检查 + 拦确认

**第一层：确定性修复**（不花 LLM、不改创作）


`consistency_service.repair_storyboard()` 在分镜生成后、校验前跑一遍，修掉**不需要创作者决策**
的问题：

- **说话人不在画面里** → 把说话人补进 `character_in_frame`。这一条同时影响三件事：
  该镜头拿不到这个角色的参考图（脸会漂）、外貌锚点不会写上他、而且「人不在画面却在说话」
  本身就是穿帮。真实项目里 15 个镜头有 3 个中招

不修的（缺 video_prompt、悬念没闭合、时长异常）仍然照报，留给人工或重新生成。

**第二层：结构校验 + LLM 剧本医生**（`SCRIPT_STRICT` / `SCRIPT_LLM_REVIEW`，默认都开）

| 检查 | 级别 | 判据 |
|---|---|---|
| 空场（既无台词也无概述） | ERROR | 机器一定能看出来 |
| 相邻两场内容一字不差（纯注水） | ERROR | 机器一定能看出来 |
| LLM 剧本医生报的硬伤 | ERROR/WARNING | 钩子 / 目标与阻力 / 转折 / 高潮 / 收束 / 因果链 / 人物动机 |
| 结尾仍以悬念问句收尾 | WARNING | 正常台词里问句很多，**不能**当证据拦人 |
| 全剧找不到对抗语汇 | WARNING | 同上，只是提示「可能没戏」 |
| 时间线倒退、悬念未闭合 | WARNING/INFO | 见 `world_state` |

**ERROR 会拦住「确认剧本 / 确认分镜」**，错误信息直接给出问题与建议；
确认没问题可以带 `force=true` 强制继续（UI 会先弹原因再问你是否强制）。
LLM 评审失败一律降级（只记 warning），绝不会因为它把生成链路打死 ——
评审的 token 与生成合并记在同一条 `script` 账里。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查（含 LLM key 是否配置，**不返回密钥**） |
| POST/GET | `/api/projects` | 创建 / 列表 |
| GET/PUT/DELETE | `/api/projects/{id}` | 详情 / 改标题比例时长 / 删除 |
| POST | `/api/projects/{id}/bible/generate` | 生成故事圣经 |
| GET/PUT | `/api/projects/{id}/bible` | 读取 / 全量覆盖（仅剧本确认前） |
| POST/GET | `/api/projects/{id}/story-outline/generate` · `/story-outline` | **长篇剧集大纲**（20+ 章骨架 + 逐章正文；只花 LLM） |
| POST | `/api/projects/{id}/script/generate` | 生成剧本（body 可带 `chapters:["ch_1","ch_2"]` 只展开选中章；`force` 清空重来。失败后重试即续跑） |
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
| GET | `/api/models` | 可选模型 / 单价 / 预算 / **两套音色表**（Edge + 云端）/ 默认章数（前端下拉用） |
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
│                             #   / lipsync（逐帧口型，可插拔引擎）/ compose（FFmpeg 合成）
│                             #   / cost（记账）
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
                              #   / motion（画面运动实测）/ audio_align（口型起点对齐）
                              #   / lip_sync（口型诊断测量）/ av_sync（音画时长自检）

tools/lipsync/                # 逐帧口型的独立环境与脚本（不进主环境依赖）
├── install.sh                # 建 .venv-lipsync + 下载权重 + 自检
├── wav2lip_onnx.py           # Wav2Lip ONNX 推理（不需要 torch）
├── wav2lip_mel.py            # 音频前端（纯 numpy，可独立验证）
├── check_mel.py              # 与真 librosa 逐格对比（验证 mel 实现）
└── build_compare.py          # 拼方案对比片（每个部件音画严格等长）
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
    不同镜头里就是不同的人（这不是 bug，是文生视频的固有行为）。H3 元数据里列了
    `reference_images` SKU（$0.04/镜头），但实测 3 个带参考图的镜头实收仍是 $0.65/镜头，
    没有附加费 —— 以 provider 实报为准
16. **AI 生成的定妆照不等于本人**：它只是「把这个角色的文字设定画成一张稳定的脸」，
    第一次生成的脸就是这一版片子里的脸；不满意就重画一张（换图意味着已生成的片段要对齐重跑）
17. **Edge 中文音色池很小**：通用普通话只有 2 个女声 + 4 个男声（另有 2 个方言女声）。
    同性别角色超过池子容量时必然要「复用音色 + 音高/语速区分」，听感上仍能分辨，
    但不如各自独立音色干净 —— 想彻底解决要接付费 TTS
18. **一镜多图里，第二张参考图的实际影响较弱**：双人镜头现在会把两个角色的参考图都传进
    `input_references`（v0.13 起），但实测第二个角色（尤其当他在画面里占比小、
    只是「凑上前递名片」这类次要动作时）仍会与自己的定妆照有偏差 ——
    这是视频模型内部对多图参考的权重问题，本项目只能保证「把两张图都准确地送进去」。
    想让配角也严格锁死，可行方向是**把双人镜头拆成单人镜头**（分镜层面）
19. **烧字幕是重编码**：`burn` 会让整片重新编码一遍（比 `soft` 慢、且视频流不再是无损 copy）；
    需要「随时刻换字幕/多语言」就别烧，用 `soft` + `.vtt` 预览
20. **长篇大纲是 LLM 写的**：20 章的因果链仍可能有一两处硬拗 —— 骨架与实际场次都允许人工
    编辑；「章数越多越像流水账」是模型能力的上限，本项目只能把结构约束写死
21. **按章展开会作废下游**：新增场次后旧分镜与已生成的片段不再对应，会被清掉（重新花钱）；
    所以建议**先选定要拍的章、再开始烧视频**，别出一半再回头加章
22. **云端 TTS 的中文带一点非母语味道**：`openai/gpt-audio-mini` 的情绪表达力明显强于 Edge，
    但发音不如 Edge 的中文神经音色「地道」；两者可以随时切换（换引擎会自动重配音色名，
    `AUDIO_LLM_SCRIPT` 与念法都会保留）
23. **逐帧口型只改嘴**：头姿、眼神、身体动作仍是原片段的节奏；原片段整个人在演另一句话的
    时候，光靠重画嘴救不回来（所以「台词进 prompt」这一层仍然必要）。嘴只有 96×96，
    特写放大回 1440 宽偏软，要更清楚得接 GFPGAN/CodeFormer 或换 LatentSync（要 CUDA）
24. **逐帧口型不做「不说话的人」**：默认只处理「有台词且说话人在画面里」的镜头。
    旁白镜头不动嘴（画面里的人本来就不该跟着旁白说），说话人不在画面里（过肩/听者）
    也不动嘴 —— 替他动嘴是穿帮。要全做就 `LIPSYNC_SCOPE=all`
25. **口型质量没有自动判据**：试过四种便宜的像素统计量，连「模型原生语音」这个基准都只有
    r≈0.5、最佳滞后 0.1~0.25 秒，量不准嘴的开合。所以只保留引擎自检
    （`mean_patch_change`，能抓住「静默失效」）+ `output/lipsync_report.json` 里的
    改写时段 + **人工看帧**。要做成自动判据得另上 SyncNet 或人脸关键点
27. **国家判断是启发式的**：中文/日文/韩文名字靠姓氏表 + 假名/谚文推断，不是人口学判断。
    日本名写成汉字且不在姓氏表里、生僻拼音姓、以及中日韩**共用**的汉字姓（金/李/林…）
    都可能判错 —— 共用姓靠「同剧其他角色投票」或人工写 `region` 解决。
    名字与设定本来就不一致时（中文名 + 「日本留学生」人设），以描述里的国家为准
28. **`concat -c copy` 会吃掉音频空洞**：每个部件的音轨时长必须严格等于画面时长，
    否则 mp4 表达不了空洞、后面所有音频整体提前（真实事故：对比片漂移 7.154 秒，
    听感就是「语音和视频是分开的」）。成片现在会自动做这项检查并写进报告

## 安全

- `.env` 不要提交，也不要贴到任何地方；泄漏后请立即到 OpenRouter 轮换
- 所有对外异常/日志都经过 `scrub_secrets()` 脱敏，密钥不会进入日志、前端或报告
- 服务默认只监听 `127.0.0.1`；对外暴露时请设置 `API_TOKEN`
