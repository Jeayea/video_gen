# videogen

AI 短剧自动化流水线：从一句创意、一份大纲、一部剧本或一部小说出发，自动完成
**故事圣经 → 剧本 → 分镜 → 视频片段 → 配音 → 配乐 → 成片（含字幕）**，每个关键节点都可以人工审改。

> 项目状态、已知限制、风险和各功能的实测记录见 [AGENTS.md](AGENTS.md)；
> 设计文档见 [DESIGN.md](docs/DESIGN.md)；逐版修复记录见 [FIXES.md](docs/FIXES.md)。

## 功能

- **四种输入**：主题 / 故事大纲 / 完整剧本 / 小说原文（自动切章，按章挑着改编）
- **科普讲解**（建项目时选内容类型）：先整理带出处的知识底稿，再写旁白；一句旁白一个镜头，每句标出依据的事实，片头自动加「本视频由 AI 生成」
- **故事圣经与剧集资产**：角色、场景、关键道具；定妆照、场景图、道具图可 AI 生成或上传，生成视频时作为参考图送进视频模型
- **长篇大纲**：先写 20+ 章的全篇骨架，只把勾选的章展开成剧本
- **剧本与分镜**：逐场写剧本并做一致性校验；分镜自带英文视频 prompt、运镜、地点、出镜角色；任意一场 / 一镜可按修改意见重写
- **视频生成**：默认 mock（免费占位片）；接 OpenRouter 上的真实视频模型（默认 MiniMax H3）。同场相邻镜头编成一段一次生成，按段计费
- **声音**：默认用视频原声；也可按角色 TTS 配音（Edge 免费 / 云端支持自然语言念法）。配乐可按分镜在本机作曲或从曲库选，说话时自动压低音乐，响度自动对齐
- **成片**：FFmpeg 合成，软字幕 / 烧录字幕 / 外挂 SRT·VTT，可整片变速，可选逐帧口型
- **成本控制**：生成前先报价，可设预算上限；按 provider 实际扣费记账
- **工作台**：Web 界面 + REST API，后台任务可查看进度、取消、重启后恢复；支持批量生产

## 安装

需要 Python 3.11+（macOS / Linux）。ffmpeg 可选：没装时会带一个便携版。

```bash
git clone https://github.com/Jeayea/video_gen.git && cd video_gen
./run.sh                      # 第一次会自动安装，然后打开 http://127.0.0.1:8001
```

第一次运行 `run.sh` 会调用 `./install.sh`：建 `.venv`、按 `requirements.lock` 装锁定版本的依赖（带哈希校验）、
生成 `.env` 并提示粘贴 `OPENROUTER_API_KEY`（不回显），最后跑一遍环境自检。脚本可以重复跑，已有的 `.env` 不会被覆盖。

- 只填 `OPENROUTER_API_KEY` 就能跑通全流程。视频默认 `VIDEO_PROVIDER=mock`，不花钱
- `.env.example` 只有最常改的几项；全部配置项在 [.env.full.example](.env.full.example)，要改哪项就把那一行抄进 `.env`
- 随时自检（key 有没有配、ffmpeg 能不能烧字幕、口型环境装没装；不联网、不花钱）：`.venv/bin/videogen doctor`
- 启动也可以用 `.venv/bin/videogen serve [--host ... --port ... --reload]`
- 可选：`python -m videogen.tools.demo_bgm` 生成 4 首演示 BGM 到 `./assets/bgm`
- 服务默认只监听 `127.0.0.1`。改监听地址用 `HOST=0.0.0.0 PORT=8001 ./run.sh`，**暴露到局域网前先设置 `API_TOKEN`**

## 使用

打开 `http://127.0.0.1:8001`，按左侧阶段导航一步步往下走：

1. **新建项目**：选输入方式，填标题、内容、画面比例、目标时长和语言（中文 / English）。小说直接粘贴原文
   ![新建项目](docs/screenshots/02-create.png)
2. **故事圣经**：生成角色、场景、道具，在卡片上生成或上传参考图
   ![圣经与资产](docs/screenshots/03-assets.jpg)
3. **剧本**：生成并检查剧本后确认。长篇和小说项目要先勾选要拍的章
4. **分镜**：生成后可以逐镜编辑，确认后进入视频阶段
   ![分镜](docs/screenshots/05-storyboard.png)
5. **视频**：先看预估花费，确认后在后台生成。失败的片段可以单独重试，不满意的段可以「重做这段」，换下来的旧片段在「历史版本」里可以恢复
6. **配音、配乐与成片**：生成配音和配乐，选字幕样式后合成，页面里可以预览并下载 MP4 和字幕
   ![成片](docs/screenshots/07-compose.jpg)

确认过的阶段可以「退回」修改，下游已生成的媒体会随之作废。顶部「批量生产」页可以一次提交多个主题，
批量会跳过人工确认，量产前先单条跑通。

### 科普讲解

新建项目时内容类型选「科普讲解」，再填目标观众和参考资料（可选，强烈建议）。输入方式是题目 / 要点大纲 / 完整旁白
（完整旁白一个字都不改，只切镜头、标事实）。和短剧的区别：

- **知识底稿**代替故事圣经：一条条事实，每条带出处。有参考资料时事实只从资料里取，出处自动核对是不是资料里的原句；
  在底稿面板上逐条「确认 / 驳回」。出处核对只证明引文真实存在，结论对不对仍要人看
- **剧本**是旁白，每句后面标出依据的事实；引用了被驳回或不存在的事实会拦住确认
- **分镜**一句一镜：镜头时长按这句念多久定，模型只设计画面，不许改旁白、不在画面里写公式和数据
- **画面**默认每句一张 AI 示意图（插画风格）+ 本机推拉，按张计费（约 $0.04/张）；在视频阶段可以切成视频模型
- **语气**（严谨 / 轻松 / 玩梗）和**插图画风**（杂志插画、扁平卡通、手绘漫画、水彩、复古图鉴、古希腊陶瓶画、
  罗马马赛克、中国水墨，或自填英文画风）：新建时可选，留空则生成知识底稿时按题目推荐，之后在面板上随时改。
  「玩梗」只放开开场、过渡和比喻，讲事实的句子照底稿说；换画风后已画好的图下次生成时全部重画
- 默认 TTS 旁白、`box` 字幕、更低的配乐；合成时镜头按旁白实际说完的时刻裁剪
- 片头前 3 秒烧录「本视频由 AI 生成」，成片元数据里也写入 AI 生成说明（项目的 `ai_label` 可关）。字样和时长发布前请对照平台规则核对
- 批量生产只有「有参考资料、引用的事实全部通过出处核对、剧本检查没有 ERROR」才自动往下跑，否则停在剧本阶段等人核对

设计见 [docs/EXPLAINER_DESIGN.md](docs/EXPLAINER_DESIGN.md)。

### 接入真实视频模型

在 `.env` 里设置：

```bash
VIDEO_PROVIDER=openrouter
OPENROUTER_VIDEO_MODEL=minimax/hailuo-3   # 任意 OpenRouter 视频模型
BUDGET_USD=12                              # 强烈建议先设预算，0 = 不限制
```

模型的分辨率、时长档位、单价都在运行时从 OpenRouter 读取，换模型不用改代码。
视频按秒计费，生成前务必看一眼报价：

```bash
curl -s localhost:8001/api/projects/$PID/preflight   # 预估计费秒数、金额、预算余额
curl -s localhost:8001/api/projects/$PID/cost        # 实际花费账本
```

按 token 计费的模型（如 Seedance）会按分辨率自动换算成每秒单价；实在读不到单价时要手填 `VIDEO_PRICE_PER_SECOND`，否则设了预算会拒绝开工。
各模型的实测对比和注意事项见 [AGENTS.md](AGENTS.md)。

### 常用配置

完整列表见 [.env.full.example](.env.full.example)。

| 想做什么 | 配置 |
|----------|------|
| 换 LLM | `OPENROUTER_MODEL` |
| 按角色 TTS 配音（默认用视频原声） | `AUDIO_DUB_MODE=tts`，`TTS_PROVIDER=edge\|openrouter` |
| 配乐来源 | `BGM_ENGINE=generate\|library\|cli\|none` |
| 字幕 | `COMPOSE_SUBTITLE_MODE=soft\|burn\|none`，样式 `COMPOSE_SUBTITLE_STYLE=soft\|box\|doc\|classic` |
| 整片变速 | `COMPOSE_SPEED`（0.5~4） |
| 关掉段落分镜，一镜一次生成 | `VIDEO_SEGMENT_MODE=false` |
| 同场下一段接着上一段的最后一帧拍（只认首帧的模型） | `VIDEO_KEYFRAME_CHAIN=true` |
| 逐帧口型 | 先跑 `tools/lipsync/install.sh`，再设 `LIPSYNC_ENGINE=wav2lip-onnx` |

### API

界面上的操作都有对应的 REST 接口，启动后访问 `/docs` 查看。设置了 `API_TOKEN` 时，写接口要带
`X-API-Token` 请求头。

## 开发

```bash
./install.sh --dev                                  # 加装 pytest / ruff
.venv/bin/python -m pytest -q                       # 离线测试，不调用付费服务
.venv/bin/ruff check src tests tools
.venv/bin/python tools/e2e/mock_pipeline_e2e.py --source <源项目 id>   # 免费的端到端冒烟
```

`.env` 和 `projects/`（媒体产物）不进仓库。开发约定见 [AGENTS.md](AGENTS.md)。
