# DESIGN — 剧本生成 + AI 短剧自动化

> v0.1 · 2026-09-12 · draft

---

## 1. 项目目标

构建一条从「创意输入」到「成片」的半自动流水线，宽高比可配置：

```
主题 / 大纲 / 完整剧本
        │
        ▼
   ┌─ 剧本生成 ──────────┐   ← LLM (OpenRouter)
   │  分场景 · 分镜头脚本  │
   └────────┬────────────┘
            │  [人工确认]
            ▼
   ┌─ 分镜 & 视频 Prompt ─┐   ← LLM
   │  每镜画面描述 + 运镜   │
   └────────┬────────────┘
            │  [人工确认]
            ▼
   ┌─ 视频片段生成 ───────┐   ← 可插拔适配器
   │  N 段可配比例短片     │
   └────────┬────────────┘
            │
   ┌─ 配音 TTS ───────────┐   ← AI TTS
   ├─ BGM 生成/匹配 ──────┤   ← AI BGM / 曲库
   └────────┬────────────┘
            │  [人工确认]
            ▼
   ┌─ 合成输出 ───────────┐   ← FFmpeg
   │  可配比例成片 + 字幕  │
   └─────────────────────┘
```

**核心原则**

| 原则 | 说明 |
|------|------|
| 文本层便宜跑通 | 剧本/分镜/prompt 用 OpenRouter 低成本模型，快速迭代 |
| 视频层可插拔 | 抽象 VideoProvider 接口，先接一个，后续换/加不影响上游 |
| 分阶段确认 | 每个关键节点停一下让人审，避免浪费视频生成额度 |
| 产物可追溯 | 每一步的输入/输出都落盘为 JSON，方便回放和 diff |

---

## 2. 系统架构

### 2.1 分层

```
┌─────────────────────────────────────────────────┐
│  UI 层                                          │
│  Web Dashboard (React / 简单 HTML)              │
│  展示各阶段产物 · 确认/修改/重新生成             │
├─────────────────────────────────────────────────┤
│  编排层 (Orchestrator)                          │
│  状态机管理 · 阶段流转 · 人工确认门控            │
├─────────────────────────────────────────────────┤
│  服务层                                         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│  │ Script   │ │ Video    │ │ Audio    │        │
│  │ Service  │ │ Service  │ │ Service  │        │
│  └──────────┘ └──────────┘ └──────────┘        │
│  ┌──────────┐ ┌──────────┐                      │
│  │ Compose  │ │ Project  │                      │
│  │ Service  │ │ Store    │                      │
│  └──────────┘ └──────────┘                      │
├─────────────────────────────────────────────────┤
│  外部依赖                                       │
│  OpenRouter · VideoProvider · TTS · BGM · FFmpeg│
└─────────────────────────────────────────────────┘
```

### 2.2 技术选型

| 层 | 选择 | 理由 |
|----|------|------|
| 后端 | Python 3.11+ / FastAPI | AI 生态丰富，异步友好 |
| LLM | OpenRouter (OpenAI 兼容) | 便宜模型多，一键切换 |
| 视频生成 | 抽象接口 + 具体适配器 | 先 mock / 接一个便宜的 |
| TTS | OpenAI TTS / Edge-TTS / Fish Audio | 后面接哪个都行，同样抽象 |
| BGM | 先用免版税曲库 + AI 生成(后续) | MVP 先曲库匹配 |
| 合成 | FFmpeg | 行业标准，拼接/字幕/混音 |
| 存储 | 本地文件系统 (JSON + 媒体文件) | MVP 不引入数据库 |
| 前端 | 先 FastAPI 内置静态页 / 后续 React | 快速可用优先 |
| 部署 | 本地开发 → Docker | 先跑通再容器化 |

### 2.3 目录结构

```
video_gen/
├── README.md
├── docs/DESIGN.md
├── pyproject.toml
├── .env.example
├── src/
│   └── videogen/
│       ├── __init__.py
│       ├── config.py              # 配置管理
│       ├── main.py                # FastAPI 入口
│       ├── orchestrator/
│       │   ├── __init__.py
│       │   ├── state_machine.py   # 阶段状态机
│       │   └── pipeline.py        # 流水线编排
│       ├── services/
│       │   ├── __init__.py
│       │   ├── script_service.py      # 剧本生成（含 bible 优先策略）
│       │   ├── storyboard_service.py  # 分镜
│       │   ├── video_service.py       # 视频片段
│       │   ├── audio_service.py       # TTS + BGM
│       │   ├── compose_service.py     # 成片合成
│       │   └── consistency_service.py # 连续性校验
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── llm/
│       │   │   ├── base.py
│       │   │   └── openrouter.py
│       │   ├── video/
│       │   │   ├── base.py        # VideoProvider ABC
│       │   │   ├── mock.py        # 测试用
│       │   │   └── (kling.py)     # 后续接入
│       │   ├── tts/
│       │   │   ├── base.py
│       │   │   └── edge_tts.py    # 免费起步
│       │   └── bgm/
│       │       └── library.py     # 本地曲库匹配
│       ├── models/
│       │   ├── __init__.py
│       │   ├── project.py         # 项目数据模型
│       │   ├── script.py          # 剧本模型
│       │   ├── storyboard.py      # 分镜模型
│       │   ├── bible.py           # Story Bible 模型
│       │   └── world_state.py     # World State 模型
│       └── utils/
│           ├── __init__.py
│           ├── ffmpeg.py
│           └── file_store.py
├── projects/                       # 运行时产物目录
│   └── {project_id}/
│       ├── meta.json              # 项目元信息
│       ├── story_bible.json       # 不可变事实（角色/设定/风格）
│       ├── world_state.json       # 可变状态（随场景更新）
│       ├── script.json            # 剧本
│       ├── storyboard.json        # 分镜
│       ├── consistency_report.json # 一致性校验结果
│       ├── clips/                 # 视频片段
│       ├── audio/                 # 配音 + BGM
│       └── output/                # 成片
└── tests/
    └── ...
```

---

## 3. 数据模型

### 3.1 核心 JSON Schema

**Project (meta.json)**
```json
{
  "id": "uuid",
  "title": "都市复仇短剧",
  "created_at": "2026-09-12T10:00:00Z",
  "status": "script_review",
  "aspect_ratio": "9:16",
  "target_duration_sec": 45,
  "input_mode": "theme | outline | full_script",
  "input_text": "用户原始输入"
}
```

> **`aspect_ratio` 可选值**：`9:16`（竖屏·抖音/快手）、`16:9`（横屏·B站/YouTube）、`1:1`（方形）。创建项目时选定，贯穿全流程——影响分镜 video_prompt、视频生成参数、以及最终合成输出尺寸。默认 `9:16`。

**Script (script.json)**
```json
{
  "title": "逆袭",
  "logline": "一句话梗概",
  "characters": [
    {
      "id": "char_1",
      "name": "林晚",
      "description": "25岁女白领，外表柔弱内心坚韧",
      "visual_prompt": "a young Chinese woman, 25, long black hair, office attire, determined eyes"
    }
  ],
  "scenes": [
    {
      "id": "scene_1",
      "heading": "INT. 办公室 - 日",
      "summary": "林晚被同事当众羞辱",
      "dialogue": [
        {"speaker": "char_1", "line": "这个项目是我做的。"},
        {"speaker": "char_2", "line": "你？别开玩笑了。"}
      ],
      "duration_sec": 12,
      "emotion": "压抑/隐忍"
    }
  ]
}
```

**Storyboard (storyboard.json)**
```json
{
  "shots": [
    {
      "id": "shot_1",
      "scene_id": "scene_1",
      "shot_no": 1,
      "duration_sec": 4,
      "camera": "中景 · 固定",
      "visual_description": "林晚站在会议室中央，同事围坐，气氛压抑",
      "character_in_frame": ["char_1"],
      "mood": "紧张",
      "video_prompt": "Medium shot, a young Chinese woman standing in a modern office meeting room, colleagues sitting around a long table looking at her coldly, tense atmosphere, cinematic lighting, 4 seconds",
      "narration": null,
      "dialogue_text": "这个项目是我做的。",
      "dialogue_speaker": "char_1"
    }
  ]
}
```

### 3.2 阶段状态机

```
DRAFT
  → SCRIPT_GENERATED
  → SCRIPT_REVIEW        ← 人工确认/修改
  → STORYBOARD_GENERATED
  → STORYBOARD_REVIEW    ← 人工确认/修改
  → VIDEO_GENERATING     ← 批量生成片段
  → VIDEO_READY
  → AUDIO_GENERATING     ← TTS + BGM
  → AUDIO_READY
  → COMPOSING            ← FFmpeg 合成
  → DONE / FAILED
```

每个 `*_REVIEW` 状态是一个**门控点**：用户确认后才进入下一阶段；也可以带着修改意见「重新生成」。

---

### 3.3 故事圣经 & 世界状态（连续性保障）

LLM 逐场景生成时最容易丢的四类信息：**人名/称呼**、**时间线**、**数字/金额**、**人物关系与已知信息**。解法是两份结构化文档 + 生成时注入 + 事后校验。

#### Story Bible（story_bible.json）— 不可变事实

剧本确认后生成，后续生成/重生成时**只读注入，不被 LLM 改写**。

```json
{
  "characters": [
    {
      "id": "char_1",
      "name": "林晚",
      "aliases": ["小林", "林总监"],
      "age": 25,
      "role": "主角",
      "description": "女白领，外表柔弱内心坚韧",
      "visual_prompt": "a young Chinese woman, 25, long black hair...",
      "voice_id": "zh-CN-XiaoxiaoNeural",
      "personality": ["隐忍", "聪明", "不服输"],
      "relationships": [
        {"target": "char_2", "type": "敌对", "note": "抢过她的项目"}
      ]
    }
  ],
  "settings": [
    {"id": "loc_1", "name": "星辰科技办公室", "description": "现代写字楼，落地窗", "visual_prompt": "modern open-plan office..."}
  ],
  "facts": [
    {"id": "fact_1", "content": "林晚的工号是 A-2071", "type": "number"},
    {"id": "fact_2", "content": "故事发生在 2026 年春天", "type": "timeline"},
    {"id": "fact_3", "content": "林晚母亲住院，医药费 30 万", "type": "number"},
    {"id": "fact_4", "content": "赵明远是林晚的直属上司", "type": "relationship"}
  ],
  "style": {
    "genre": "都市复仇",
    "tone": "压抑中带爽感",
    "pacing": "快节奏，每场有反转",
    "taboo": ["超现实", "血腥"]
  }
}
```

#### World State（world_state.json）— 可变状态

每写完一场自动更新，记录「此刻谁在哪、谁知道什么、状态如何」。

```json
{
  "current_scene": "scene_3",
  "timeline": "故事第 2 天 · 下午",
  "character_states": [
    {
      "character_id": "char_1",
      "location": "loc_1",
      "physical": "正常",
      "emotional": "愤怒但克制",
      "knows": ["fact_1", "赵明远抢了她的方案"],
      "possesses": ["手机", "项目草稿"],
      "active_goals": ["证明方案是自己的"]
    }
  ],
  "open_threads": [
    "林晚还没找到证据",
    "母亲的医药费还差 20 万"
  ],
  "established_facts_this_session": [
    "scene_2 中林晚当众说出了工号 A-2071"
  ]
}
```

#### 生成时注入（Prompt 策略）

生成**第 N 场**时，system prompt 携带：

```
┌─ 固定注入（只读）─────────────────────────┐
│  1. Story Bible（全部角色/设定/事实/风格）  │
│  2. 前一场摘要（scene N-1 的 summary +     │
│     对白要点 + 结尾状态）                  │
│  3. 当前 World State                       │
│  4. 大纲中本场的预期内容                   │
└──────────────────────────────────────────┘
  → 生成 scene N 的完整内容
  → 附带 world_state 更新指令
```

规则：
- **人名只能用 bible 里的 id + name**，禁止 LLM 新造角色（新角色需显式声明并写入 bible）
- **数字/金额/时间**引用时必须从 `facts` 取值，或新增 fact
- **称呼变化**（如叫「林总监」）写入该场 dialogue，同时更新 character 的 aliases
- 每场生成完，LLM 输出一个 `world_state_delta`，系统 merge 进 world_state.json

#### 事后校验（Consistency Validator）

生成完所有场次后，跑一遍自动校验：

| 检查项 | 方法 |
|--------|------|
| 人名一致 | 扫描全部对白/旁白，人名必须在 bible.characters 的 name+aliases 里 |
| 数字一致 | 提取数字/金额/日期，与 facts 交叉比对，冲突则告警 |
| 时间线 | scene 的 heading 时间标注 + timeline 是否单调递进 |
| 人物在场 | dialogue speaker 必须在该场 character_in_frame 或已交代离场 |
| 称呼突变 | 同一角色在无铺垫情况下称呼突变（如突然叫全名）→ 警告 |

校验结果写入 `consistency_report.json`，UI 上标红问题场次，用户可选择「重新生成该场」或「手动修改」。

---

### 3.4 连续性在流水线中的位置

```
剧本生成
  ├─ 先生成 Story Bible（角色/设定/事实/风格）
  ├─ 再逐场生成，每场注入 bible + 前场摘要 + world_state
  └─ 生成完毕 → 自动更新 world_state → 一致性校验
        │
        ▼  [人工确认剧本 + 校验报告]
分镜生成
  ├─ 注入 bible（角色 visual_prompt 保持一致）
  └─ 逐 scene 生成 shots，继承该场 world_state
```

---

## 4. 核心流程详解

### 4.1 剧本生成 (Script Service)

**输入模式**

| 模式 | 说明 |
|------|------|
| `theme` | 用户给主题/关键词，LLM 从零生成 |
| `outline` | 用户给大纲，LLM 扩写成分场剧本 |
| `full_script` | 用户贴完整剧本，LLM 结构化解析 |

**Prompt 策略**
- System prompt 固定短剧风格约束（节奏快、冲突密、每场有钩子）
- 输出强制 JSON schema（用 OpenRouter 的 structured output 或 prompt 约束 + 校验重试）
- **先生成 Story Bible，再逐场生成**（详见 §3.3）：每场注入 bible + 前场摘要 + world_state，保证人名/数字/时间线连续
- 每场生成后输出 `world_state_delta`，系统 merge 更新
- 全部场景生成完毕后跑一致性校验，报告写入 `consistency_report.json`

**模型选择**：OpenRouter 便宜档，如 `google/gemini-2.0-flash-001` 或 `anthropic/claude-3.5-haiku`（按成本选）

### 4.2 分镜生成 (Storyboard Service)

- 输入：已确认的 script.json
- LLM 为每个 scene 生成 2-5 个 shot
- 每个 shot 包含：时长、运镜、画面描述、台词/旁白、**video_prompt**
- `video_prompt` 是英文的、面向视频模型的画面描述，包含：
  - 主体 + 动作 + 环境 + 光线 + 风格
  - 构图提示随 `aspect_ratio` 变化（竖屏强调上下构图/人物全身，横屏强调横向空间）
  - 时长提示

### 4.3 视频生成 (Video Service)

**VideoProvider 抽象接口**

```python
class VideoProvider(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        duration_sec: int,
        aspect_ratio: str = "9:16",
        reference_image: Path | None = None,
    ) -> VideoClip:
        """生成一段视频，返回本地文件路径 + 元信息"""
        ...

    @abstractmethod
    async def check_status(self, task_id: str) -> GenerationStatus:
        ...
```

**MVP 实现**
1. `MockVideoProvider` — 返回纯色/占位视频，跑通全流程
2. 第一个真实适配器（后续选定：可灵 API / Runway / 其他）

**并发策略**
- 每个 shot 独立生成，可并发（注意 API rate limit）
- 失败重试 N 次，仍失败则标记该 shot，不阻塞其他 shot

### 4.4 配音 + BGM (Audio Service)

**时长档位（v0.11）**
- 分镜生成前读视频 provider 的 `duration_options()`（H3 = 5~15 秒），
  每场镜头数 = `plan_shot_count(场时长, min, max, target)`，写进提示词当硬约束
- 生成后 `apply_duration_tier()` 兜底：合并超编镜头（沿用「不丢对白」规则）+ `fit_durations()` 重分配
- 无档位（mock）时保持原行为：LLM 自己拆 2-5 镜 + `normalize_storyboard_durations()`

**TTS**
- 每个有 `dialogue_text` 或 `narration` 的 shot 生成一段语音
- 角色可分配不同 voice_id（先简单映射，后续支持音色克隆）
- MVP 用 Edge-TTS（免费）或 OpenAI TTS
- 文案来源可以是分镜原文，也可以是 LLM 看着画面写的（v0.9，见 §10）：
  抽帧 → 多模态 `chat_json` → 写回 `shot.narration / dialogue_text` + 念法建议 → 再合成

**BGM**
- 曲库（v0.4）：本地免版税曲库 + 按 `mood` 标签匹配
- 本机现写（v0.19）：`BGM_ENGINE=generate` —— 由分镜情绪/时长/台词 + 画面运动实测
  写出**配乐谱**（色调/调式/BPM/段落/和弦/配器/重音），再用纯标准库合成 WAV，
  过 `afir` 卷积混响 + 压缩 + 响度归一；段落切点对齐镜头、运动爆发处给重音、
  说话窗口让路。详见 AGENTS.md「配乐」
- AI 音乐模型（v0.19）：`BGM_ENGINE=cli` + `BGM_CLI_CMD` 模板（`{out}`/`{prompt}`），
  提示词由配乐谱生成（英文 + 段落时间轴）

**音画对齐**
- TTS 生成后测量时长，可微调 shot duration 以对齐
- 若 TTS 比视频长：拉长该 shot 或截断（可配置策略）

### 4.5 合成输出 (Compose Service)

FFmpeg 流水线：
1. 逐 shot：视频 + 对应 TTS 音轨 → 中间片段
2. 拼接所有中间片段
3. 混入 BGM：`volume` + 淡入淡出 + `atrim`，默认再叠一层**人声闪避**
   （`[voice]asplit` → `sidechaincompress`，说话时压低音乐、说完 0.4s 抬回；
   ffmpeg 没有这个滤镜就退回固定音量并告警）。生成的曲目不再 `-stream_loop`，
   它是整片长度、自然收尾
4. 可选：烧录字幕（srt）
5. 输出 MP4，分辨率按项目 `aspect_ratio` 决定（9:16 → 1080×1920，16:9 → 1920×1080，1:1 → 1080×1080）

---

## 5. API 设计

```
POST   /api/projects                    创建项目（body: title, input_mode, input_text, aspect_ratio, target_duration_sec）
GET    /api/projects/{id}               获取项目状态
GET    /api/projects/{id}/script        获取剧本
PUT    /api/projects/{id}/script        修改剧本
POST   /api/projects/{id}/script/generate   触发剧本生成
POST   /api/projects/{id}/script/confirm    确认剧本 → 进入分镜

GET    /api/projects/{id}/storyboard    获取分镜
PUT    /api/projects/{id}/storyboard    修改分镜
POST   /api/projects/{id}/storyboard/generate
POST   /api/projects/{id}/storyboard/confirm

POST   /api/projects/{id}/video/generate    触发视频生成
GET    /api/projects/{id}/video/status      视频生成进度

POST   /api/projects/{id}/audio/generate    触发配音+BGM
POST   /api/projects/{id}/compose           触发合成
GET    /api/projects/{id}/output            下载成片
```

---

## 6. 配置

`.env`：
```bash
# LLM
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=google/gemini-2.0-flash-001

# Video Provider (后续填)
VIDEO_PROVIDER=mock          # mock | kling | runway | ...
KLING_ACCESS_KEY=
KLING_SECRET_KEY=

# TTS
TTS_PROVIDER=edge            # edge | openai
OPENAI_API_KEY=              # openai tts 时用

# 角色图（AI 定妆照 = H3 下唯一的角色一致性锚点）
IMAGE_PROVIDER=openrouter
OPENROUTER_IMAGE_MODEL=bytedance-seed/seedream-4.5   # $0.04/张
IMAGE_PRICE_USD=0.04

# 路径
PROJECTS_DIR=./projects
FFMPEG_PATH=ffmpeg
FFMPEG_AUTO_UPGRADE=true      # 主 ffmpeg 缺能力时自动换候选（如烧字幕要 libass）
```

---

## 7. MVP 里程碑

### Phase 1 — 文本流水线（最快出价值）
- [ ] 项目创建 + 文件存储
- [ ] OpenRouter LLM 接入
- [ ] 三种输入模式的剧本生成
- [ ] 分镜 + video_prompt 生成
- [ ] 简单 Web UI：输入 → 看剧本 → 确认 → 看分镜
- [ ] JSON 结构校验 + 重试

### Phase 2 — 媒体流水线
- [ ] VideoProvider 抽象 + Mock 实现
- [ ] Edge-TTS 配音
- [ ] 本地 BGM 曲库匹配
- [ ] FFmpeg 合成基础版
- [ ] 端到端：主题 → 成片（mock 视频）

### Phase 3 — 真实视频接入
- [ ] 选定并接入第一个真实 VideoProvider
- [ ] 并发生成 + 进度追踪 + 失败重试
- [ ] 音画时长对齐
- [ ] 字幕烧录

### Phase 4 — 打磨
- [ ] 角色一致性（reference image / seed）
- [ ] 多模型对比切换
- [ ] 成本估算 & 额度控制
- [ ] 批量生产模式

---

## 8. 风险与开放问题

| 风险 | 应对 |
|------|------|
| 视频 API 贵且慢 | 分阶段确认避免浪费；先 mock 跑通 |
| 角色一致性差 | 用 reference image / 固定 seed；prompt 里锁定外观描述 |
| LLM 输出 JSON 不稳定 | structured output + schema 校验 + 自动重试 |
| 音画时长对不齐 | TTS 先行测时长，反向调整 shot duration |
| 各家视频 API 差异大 | VideoProvider 抽象层隔离 |

---

## 9. 下一步

1. ~~确认本 design doc~~
2. ~~初始化 Python 项目骨架~~
3. ~~实现 Phase 1（文本流水线 + 最小 UI）~~
4. 实现 Phase 2：Mock 视频 + TTS + FFmpeg 合成，跑通第一个端到端 demo

---

## 10. 实现状态（v0.21，2026-09-14 更新）

### 已落地

- **Phase 1 全部完成**：故事圣经 → 逐场剧本（注入 bible + 前场摘要 + world_state）→ 一致性校验 → 分镜（英文 video_prompt）→ Web UI 分阶段确认
- **Phase 2 视频生成已完成**：`VideoProvider` 抽象 + `MockVideoProvider`（ffmpeg 产出真实占位 MP4）、
  按镜头并发生成、失败重试与隔离、断点续跑、后台任务队列（进度/取消/单项目互斥）、
  UI 内嵌播放与失败镜头重试
- 状态机与本文档 §3.2 的差异：新增 `SCRIPT_CONFIRMED` / `STORYBOARD_CONFIRMED` 两个显式确认态，取代原先把 `SCRIPT_GENERATED` 当「已确认」用的写法；旧数据在读取时自动迁移
- 时长控制：大纲阶段下发每场预算，生成后按 `target_duration_sec` 归一化，分镜再按场对齐
- LLM 输出强制过 Pydantic schema，失败回灌错误重试；脏字段（`7.5` / `"约10秒"` / `null`）容错兜底
- 生成失败可续跑：逐场/逐镜落盘 + `outline.json` + `video_manifest.json`，重试只补缺失部分

- **Phase A（配音 + 合成）已完成（v0.4）**：`TTSProvider`（Edge-TTS 真实配音 / Mock 离线占位）、
  按角色分配音色并可单独换音色、音画时长对齐（拉长镜头 + 合成时冻结末帧）、本地 BGM 曲库按情绪匹配、
  FFmpeg 分段渲染 → concat → 混音 → 字幕（soft 内嵌 / burn 烧录 / none 只导 srt）、
  UI 第 5/6 阶段（试听、进度、成片播放与下载）
- 状态机补全：`VIDEO_GENERATING → VIDEO_READY → AUDIO_GENERATING → AUDIO_READY → COMPOSING → DONE`，
  配音与合成同样是带进度/取消/互斥的后台任务；上游文本一变就作废下游媒体

- **Phase B（打磨）已完成（v0.5）**：
  - 成本：按阶段记 token / 秒数 / 字符 → USD 账本（`cost_report.json`），
    `BUDGET_USD` 预算门控在开工前拦「已花 + 预估」，`/api/projects/{id}/cost` 可查
  - 批量：`BatchManager` + `Pipeline.run_chain`，一次提交多主题跑完整链路，
    `stop_after` 可停在 storyboard / video / audio / compose，单条失败不影响其它条目
  - 多模型：项目级 `llm_model` 或单次请求 `{"model": ...}` 切换，候选与单价走配置，
    账本记录每阶段实际模型
  - 角色一致性：参考图（base64 上传 → `refs/`）与 seed 透传到 `VideoRequest`，
    路径越界/超限会拒绝；mock provider 用参考图取色，便于验证参数真的传到了 provider

- **真实视频模型接入已完成（v0.6）**：`VIDEO_PROVIDER=openrouter` +
  `OPENROUTER_VIDEO_MODEL=minimax/hailuo-3`（MiniMax H3）。走 OpenRouter 统一的异步视频接口，
  参数按模型能力自动对齐（时长档位/分辨率/比例/seed/参考图），单价从价目表读并与预算门控联动

- **配音文案交给多模态 LLM（v0.9）**：`AUDIO_LLM_SCRIPT=off|narration|polish|full`。
  视频片段抽帧 → 与故事圣经/剧本/分镜一起发给多模态模型 → 写回 `shot.narration / dialogue_text`，
  并可给逐句念法（`rate`/`volume`/`pitch`）。按场调用、断点续跑（同模式不重复花钱）、
  任何失败都降级用原文案继续配音；token 记在 `CostStage.AUDIO` 并纳入预算门禁

- **成本前置 + 短镜头合并（v0.10）**：`GET /preflight` 把「按模型档位实际计费多少秒 / 单价 /
  预算投影 / 换模型会不会全量重跑 / 合并镜头能省多少」在动手前算清楚；UI 在生成视频前弹确认，
  免费（mock）且无需重跑时不打扰。`POST /storyboard/merge-shots`（默认 dry_run）把短镜头
  合并成更少更长的片段，**有对白的镜头绝不合并**，已有可用片段时需显式 `force`。
  同时修掉「换 provider 时预算门控按 0 个待生成镜头放行」的漏洞（见 FIXES v0.10）

- **记账口径修正（v0.12）**：视频/音频的记账从「和上次清单做集合差」改为
  「条目上的 `cost_recorded` 标记」，修掉「换 provider 全量重跑 / 文案改写后重合成」
  静默漏记（真实事故：实花 $9.75、账本 $0.0011）；新增 `POST /cost/reconcile` 幂等补齐，
  `/cost` 暴露 `video_actual_usd` / `video_ledger_usd` / `video_unrecorded_usd` 三个口径；
  另外只有**真的重跑过片段**才作废成片（依据 `VideoClip.generated_at`）

- **Phase B（v0.13）**：
  - **字幕真的看得见**：字幕模式三种都额外导出 `output/subtitles.vtt`（浏览器只认 VTT，
    既不认 SRT 也不渲染 mov_text 软轨），Web 面板用 `<track>` 预览；
    `GET /subtitles.vtt` 对老项目就地由 srt 转换，不必重新合成
  - **ffmpeg 能力感知**：`resolve_ffmpeg_for(required)` 按需要的滤镜在候选
    （`FFMPEG_PATH` → `PATH` → `imageio-ffmpeg` 便携版）里挑 exe，主 ffmpeg 没有 libass
    就自动换带 libass 的那个来烧字幕，并把 `subtitle_ffmpeg` 写进报告；
    都没有才降级 + 告警。`FFMPEG_AUTO_UPGRADE=false` 可关
  - **AI 生成角色定妆照**：`providers/image`（OpenRouter `POST /api/v1/images` + mock）→
    `services/character_image_service.py` 把 `character.visual_prompt` 组装成
    「正面 / 中性表情 / 干净背景 / 无文字」的定妆照 prompt，落盘 `refs/<cid>.png` 并写回圣经；
    之后每个镜头由 `input_references` 带给视频模型 —— 这是 H3 下**唯一**能锁住五官的手段
    （H3 `seed: false`）。记账新增 `CostStage.IMAGE`；预算不足直接 402

- **Phase B（v0.14）**：
  - **配音按人物选角**：`services/voice_catalog.py` 成为选角的唯一事实来源
    （性别线索加权、身份词不参与判定、按定位/年龄/口音代价打分、撞音色才用念法区分、
    年龄明显不符就换），选中的音色写回 `Character.voice_id`
  - **表演与镜头进 prompt**：`camera`（景别×运镜）与 `mood`（中文情绪词映射表）翻成英文、
    `VIDEO_PERFORMANCE_DIRECTIVE` 追加到每个镜头；分镜提示词同步收紧
  - **剧本严谨两层**：`repair_storyboard()` 做确定性修复（说话人不在画面里 → 补进去），
    `review_structure()` 让 LLM 当剧本医生审钩子/转折/高潮/因果/动机；
    `SCRIPT_STRICT=true` 时 ERROR 会拦住「确认剧本 / 确认分镜」（可 `force`）

- **Phase B（v0.15）**：
  - **长篇剧集大纲 + 按章展开**：新增 `story_outline.json`（`StoryOutline` /
    `StoryChapter`）与两阶段生成 —— 骨架一次给全（保证因果链完整）、正文按 3-4 章一批扩写
    （避免被输出长度截断）；`POST /script/generate {"chapters":[...]}` 只展开选中的章，
    已展开的章不重复生成，`target_duration_sec` 自动对齐到已展开章的预算之和。
    只为「选中的章」烧视频额度 —— 这是长篇形态下的成本闸门
  - **`partial` 语义**：只展开部分章时，结尾悬念不算硬伤
    （`validate_script(partial=)` / `review_structure(partial=)`），避免「选前两章必然被拦」
  - **云端 TTS**：`providers/tts/openrouter_audio.py`（`openai/gpt-audio-mini`）。
    音频输出必须 `stream: true` 且只支持 `pcm16` → SSE 逐块收 base64，标准库封 WAV；
    念法用自然语言（`VoiceDirection.instruction`），新增 `AUDIO_LLM_SCRIPT=perform`
    （只给念法、一个字不改台词）。**模型会自己加戏**，所以有两道防线：
    提示词写死「只念原文」+ 转写文本校验（不符就重试一次）+ 时长守卫（超限直接失败）。
    云端选角把 Edge 音色名映射到同气质云端音色，换引擎自动重配
  - **背景/环境**：`Shot.location` + `location_anchor()`（把场景 `visual_prompt` 注入
    视频 prompt，同场景跨镜头一致）+ `VIDEO_BACKGROUND_DIRECTIVE`（禁止空墙）
  - **口型对齐**：分两步 ——
    ① `dialogue_directive()` 把这一镜的台词写进视频 prompt（`VIDEO_PROMPT_DIALOGUE`），
    让模型的原生表演与我们的配音落在**同一句话**上（真实对照：不写台词时模型会编
    「你怎么能这样对我」这种完全不同的句子）；
    ② `utils/audio_align.py` 找出画面里人声的起点，合成时用 `adelay` 把配音延后到那一刻
    （全片实测 10/15 个镜头需要，延后 0.08~1.20s），不重烧视频也不换音色

### 尚未实现（后续可做）

- 多模型**对比留档**（同一阶段两个模型的产物并排对比）：目前只支持切换与记账
- ~~分镜阶段的「模型时长档位感知」~~ **已完成（v0.11）**：生成分镜前先读 provider 的时长档位，
  按「每镜 ≥ 最短、≤ 最长、贴近目标时长」算出每场镜头数并写进提示词；生成后再用
  `apply_duration_tier` 做确定性兜底（合并超出计划的镜头 + 重分配时长，合不动就告警）。
  详见 §4.2 与 AGENTS.md「分镜阶段就少而长」
- 任务/批量状态仅在进程内存里（项目状态已落盘，重启会自动回收）
- 一个镜头多音轨混音
- 配乐只做「音乐床」：没有主题动机随剧情变形（leitmotif）、没有按角色区分的音色、
  没有自动跟拍剪辑点做转场音效（当前重音只落在镜头切换与运动爆发上）
- ~~视频**运动**信息进入文案生成~~ **已完成（v0.16）**：本地实测运动曲线（6fps/64×64 灰度、
  纯标准库）随抽帧一起发给配音导演，念法跟着画面节奏走。判读分「整段活跃度（按均值分档）」
  与「起伏窗口（按中位数 1.6×）」。详见 AGENTS.md「配音文案」与 `utils/motion.py`
- ~~**逐帧口型对齐**~~ **已完成（v0.16）**：`LIPSYNC_ENGINE=wav2lip-onnx` 走可插拔引擎
  （内置 Wav2Lip ONNX，独立环境 `.venv-lipsync`、不需要 torch、本地 CPU、$0；
  LatentSync/MuseTalk 这类 CUDA 引擎走 `cli` + `LIPSYNC_CMD` 模板），
  默认只做「有台词且说话人在画面里」的镜头，与「把配音整体延后」互斥。
  实测 11/11 镜头、整片 62.5 秒。画质靠「只贴改过的嘴唇 + 皮肤保原分辨率 + 高频叠回」。
  **尚未做**：口型质量的自动判据（试过四种像素统计量，连基准都测不准，见 FIXES v0.16）、
  脸部修复（GFPGAN/CodeFormer）
- **名字 ↔ 国家 ↔ 脸对齐（v0.17）**：名字推国籍（假名/谚文/姓氏表/文本提示，带置信度）→
  该国的「平均脸/主角脸」英文锚点进定妆照与视频 prompt；一部片子一张脸谱（全剧组投票），
  `Character.region` 与 `IMAGE_REGION_OVERRIDE` 可人工压过。见 `utils/region.py` 与 AGENTS.md「名字 ↔ 国家 ↔ 脸」
- **国家/人物设定可在生成圣经时声明（v0.18）**：`Project.cast_region` + `Project.bible_notes`
  进提示词并写进 `StoryBible.cast_region`，下游以此为权威依据（名字启发式只做冲突提醒）。
- **按分镜现写配乐（v0.19）**：`BGM_ENGINE=generate` —— `utils/music.py` 作曲 + 合成
  （纯标准库，44.1kHz 立体声，波表/包络/噪声打击），`services/music_service.py` 负责
  引擎解析、`afir` 卷积混响、响度归一、缓存指纹与**交付前自检**（后处理把音乐弄没就退回干燥音轨）。
  配乐谱 `MusicPlan` 是结构化产物（段落/调式/BPM/配器/重音/说话窗口/人话说明），
  `POST/GET /api/projects/{id}/music` 可生成、试听、导出外部模型提示词；UI 面板显示
  段落表 + 音频播放器 + 引擎/情绪/音量/闪避开关
- **健壮性与并发修正（v0.21，外部 review 核对后修复）**：
  `FileStore._load` 坏文件隔离（一个坏 `meta.json` 不再让 `/api/projects` 500）；
  项目锁只包状态迁移（长任务期间改标题/删项目/开始合成不再挂住），并发仍由 job 互斥 +
  状态机保护；LLM provider 复用 `httpx.AsyncClient` 并由编排层按阶段 `aclose()`；
  `_repair_json` 只在字符串外替换中文标点（不再改写台词）；
  鉴权改为「写接口只认请求头 + 只读产物允许 `?token=`（`<video>`/`<a download>` 带不了头）」；
  测试里未 await 的 `wait_for_job` 修掉（`-W error::RuntimeWarning` 全绿）；
  新增密钥护栏用例（跟踪文件里出现真实形态 key 即失败）。
- **响度对齐（v0.20）**：逐条配音先量 EBU R128 集成响度，再补一个**有上限的**定值增益
  （`AUDIO_NORMALIZE` / `AUDIO_TARGET_LUFS=-20` / `AUDIO_NORMALIZE_MAX_DB=10`）；
  成片整体 `loudnorm` 到 `COMPOSE_TARGET_LUFS`（默认 -16 LUFS）+ 真峰值限幅，
  并在交付前量一遍写进报告。实测极差 15.10dB → 2.50dB、成片 -21.9 → -16.2 LUFS。
  另外修掉分段复用标记的 bug（参数原来只写在文件名里，会拿错内容）。
- **人声闪避（v0.19）**：合成时 `sidechaincompress` 用人声当 side chain（`BGM_DUCK`，
  可逐次覆盖）；`tools/music/measure_ducking.py` 逐窗口实测
  （真实项目：说话段音乐 -9.2dB、静音段 +0.1dB、人声不变）
  见 AGENTS.md「名字 ↔ 国家 ↔ 脸」与 FIXES v0.18
- **音画同步自检**：`utils/av_sync.py` 在成片阶段核对「音轨时长 ≈ 视频时长」，
  抓「`concat -c copy` 吃掉音频空洞」这类事故（真实事故：对比片漂移 7.154 秒）

> §2.3 的目录结构是目标形态，当前实际结构见 [README.md](../README.md#代码结构)；
> 修复记录见 [FIXES.md](FIXES.md)。

