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
├── DESIGN.md
├── README.md
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

**TTS**
- 每个有 `dialogue_text` 或 `narration` 的 shot 生成一段语音
- 角色可分配不同 voice_id（先简单映射，后续支持音色克隆）
- MVP 用 Edge-TTS（免费）或 OpenAI TTS

**BGM**
- MVP：本地免版税曲库 + 按 `mood` 标签匹配
- 后续：AI BGM 生成 API

**音画对齐**
- TTS 生成后测量时长，可微调 shot duration 以对齐
- 若 TTS 比视频长：拉长该 shot 或截断（可配置策略）

### 4.5 合成输出 (Compose Service)

FFmpeg 流水线：
1. 逐 shot：视频 + 对应 TTS 音轨 → 中间片段
2. 拼接所有中间片段
3. 混入 BGM（音量 ducking，旁白时降低）
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

# 路径
PROJECTS_DIR=./projects
FFMPEG_PATH=ffmpeg
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

## 10. 实现状态（v0.6，2026-09-11 更新）

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

### 尚未实现（后续可做）

- 多模型**对比留档**（同一阶段两个模型的产物并排对比）：目前只支持切换与记账
- 分镜阶段的「模型时长档位感知」：现在由 provider 抬到最短档再裁切，
  更省钱的写法是分镜直接产出 ≥5 秒的镜头（少而长）
- 任务/批量状态仅在进程内存里（项目状态已落盘，重启会自动回收）
- 一个镜头多音轨混音、配音情感/语速的逐句覆盖

> §2.3 的目录结构是目标形态，当前实际结构见 [README.md](README.md#代码结构)；
> 问题审计与修复记录见 [REVIEW.md](REVIEW.md) 与 [FIXES.md](FIXES.md)。

