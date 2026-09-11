# videogen 现状体检报告

> 检查日期：2026-09-11 · 检查方式：源码通读 + 离线桩测试（FakeLLM）+ 真实 API 端到端跑通（OpenRouter / gemini-2.5-flash，本地 127.0.0.1:8123）
> 结论：**Phase 1 的「圣经 → 剧本 → 分镜」文本链路已能真实跑通；Phase 2-4（视频/配音/合成）完全未实现，且状态机在分镜确认后进入死锁。**
>
> ⚠️ 本文件是当时的审计快照，**其中的 P0/P1/P2 问题已在 v0.2 全部修复**，逐条对照见 [FIXES.md](FIXES.md)。

---

## 1. 已实现的功能（实测可用）

| 能力 | 状态 | 证据 |
|------|------|------|
| 项目 CRUD + 文件落盘（`projects/{id}/*.json`） | ✅ | `POST/GET /api/projects` |
| Story Bible 生成（角色/设定/事实/风格） | ✅ 真实 LLM | 9.5s 返回，结构正确 |
| 大纲 → 逐场剧本生成（含 bible + 前场摘要 + world_state 注入） | ✅ 真实 LLM | 30s 生成 5 场，`world_state.json` 正确 merge |
| 一致性校验报告 | ⚠️ 部分 | 只报出 `open_threads` 1 条，漏检严重 |
| 分镜生成（含英文 `video_prompt`） | ✅ 真实 LLM | 36s 生成 18 镜，video_prompt 全英文 |
| 单文件 Web UI（`static/index.html`，782 行） | ✅ | `/` 返回 200 |
| 剧本/分镜/圣经手工编辑（整份 JSON + 逐场/逐镜表单） | ✅ | UI + `PUT` 接口 |

**未实现（DESIGN 中的 Phase 2-4）**：`video_service` / `audio_service` / `compose_service` / `VideoProvider` / `TTSProvider` / BGM 曲库 / FFmpeg 合成 / 字幕 —— 代码里**一行都没有**，`providers/video|tts|bgm/` 全是空 `__init__.py`；`config.py` 的 `video_provider`、`tts_provider`、`ffmpeg_path` 无人读取。

---

## 2. P0 · 阻断级问题（会卡死或损坏项目）

### P0-1 分镜确认后项目永久死锁
`orchestrator/pipeline.py:125` 把确认分镜后的状态写成 `VIDEO_READY`，但视频阶段不存在。

实测（真实项目）：
```
POST /storyboard/confirm      -> status = video_ready
POST /video/generate          -> 404
POST /audio/generate          -> 404
POST /compose                 -> 404
POST /storyboard/generate     -> 400 当前状态 ProjectStatus.VIDEO_READY 不允许生成分镜
POST /storyboard/confirm      -> 400 当前状态 ProjectStatus.VIDEO_READY 不能确认分镜
POST /script/confirm          -> 400 当前状态 ProjectStatus.VIDEO_READY 不能确认剧本
```
→ 项目到此**再也无法前进也无法回退**，只能手改 `meta.json`。

### P0-2 时长完全失控（最关键的质量问题）
`prompt` 里写了「项目目标时长 45 秒（共 N 场，请合理分配）」，但没有任何强制或校验。

实测：
- 目标 45s → `script.json` 5 场**每场都是 45s**，合计 **225s（超 5 倍）**
- 分镜跟着放大：18 镜合计 **180s**
- `Script.total_duration_sec` / `Storyboard.total_duration_sec` 属性**从未被调用**，`target_duration_sec` 只在 prompt 里出现过一次

### P0-3 LLM 单个字段类型不符 → 整个项目报废
`models/script.py:15` `duration_sec: int` 直接吃 LLM 原始值，Pydantic 不做容错：

| LLM 输出 | 结果 |
|----------|------|
| `7.5` | ❌ ValidationError（小数不允许） |
| `"约10秒"` | ❌ ValidationError |
| `null` | ❌ ValidationError |
| `"10"` | ✅ 侥幸通过 |

而 `script_service.generate_script()` 是**全部场次跑完才 `save_script`**，任何一场的任何一个字段出错 → 抛异常 → `pipeline.py:81` 把项目标 `FAILED`，**前面已生成的场次全部丢弃**，且没有重试。工作区里 2 个 `status=failed` 的项目就是这类产物（有 bible、无 script）。

### P0-4 JSON 解析太脆，无重试
`providers/llm/openrouter.py:70 _extract_json` 只处理「整段被 ``` 包裹」一种情况：

| 模型输出 | 结果 |
|----------|------|
| `{"a":1}` | ✅ |
| ` ```json\n{...}\n``` ` | ✅ |
| `Here is the JSON:\n{...}` | ❌ JSONDecodeError |
| `{...}\nHope this helps!` | ❌ Extra data |
| `{"a":1,}` | ❌ Illegal trailing comma |

真实使用中 gemini 加一句客套话就会命中。没有「schema 校验失败 → 把错误回灌重试」的循环 —— 而 DESIGN §8 明确要求了这一条。

### P0-5 状态机语义错乱
`pipeline.py:85-94` `confirm_script()` 连续赋值 4 次，最终把状态写成 `SCRIPT_GENERATED`（名字是"已生成"，被当作"已确认"用）：
```python
project.status = ProjectStatus.STORYBOARD_GENERATED  # 立刻被覆盖
project.status = ProjectStatus.SCRIPT_REVIEW         # 立刻被覆盖
project.status = ProjectStatus.SCRIPT_GENERATED      # 实际生效
```
后果（实测）：**确认剧本后，再点「生成剧本」会重新生成并退回 `script_review`，确认被静默丢弃**（`generate_script` 的白名单包含 `SCRIPT_GENERATED`），白白烧一次 token（实测 28s）。

同类问题：`confirm_storyboard` 写 `VIDEO_READY`（见 P0-1）；`SCRIPT_GENERATED` / `SCRIPT_REVIEW` / `STORYBOARD_GENERATED` / `STORYBOARD_REVIEW` 四个状态职责重叠。

### P0-6 PUT 接口无状态门控，可覆盖已确认产物
`main.py:99/131/172` 三个 `PUT` 直接 `model_validate` + 落盘，不检查状态。

实测：项目处于 `video_ready` 时
```bash
PUT /api/projects/{id}/script -d '{"title":"tampered","logline":"x","scenes":[]}'
-> 200 OK，已确认的剧本被清空
```
且 FileStore 非原子写、无版本备份、无并发锁，无法回滚。

---

## 3. P1 · 工程与安全问题

| # | 问题 | 位置 | 影响 |
|---|------|------|------|
| P1-1 | **真实 API Key 明文放在 `.env`**（内容已隐去） | `.env:2` | 建议**立即到 OpenRouter 后台轮换**；`.gitignore` 虽忽略 `.env`，但目录不是 git 仓库，无泄露兜底 |
| P1-2 | `run.sh` 绑 `--host 0.0.0.0` 且**无任何鉴权** | `run.sh:4` | 同局域网任何人都能调用接口，直接消耗你的 OpenRouter 额度；应默认 `127.0.0.1` |
| P1-3 | **零测试**：`tests/` 只有一个空 `__init__.py` | `tests/` | 状态机/P0-3/P0-4 这类回归无人拦截 |
| P1-4 | `LLMProvider` 抽象缺 `chat_json`，所有 service 却都在调它；provider 硬编码 `OpenRouterProvider()` | `providers/llm/base.py:12`、3 个 service | 抽象形同虚设，换模型/加 provider 要改业务代码；类型检查失效 |
| P1-5 | 静态目录从未挂载，UI 路径靠猜 | `main.py:218` `store.base.parent / "static"` | `GET /static/*` → 404；一旦 `PROJECTS_DIR` 指向别处，UI 直接白屏 |
| P1-6 | 无原子写、无并发保护；长任务同步阻塞在请求里 | `utils/file_store.py:37` | 并发写同一 JSON 会损坏；一场 30s+，6 场可能触发网关超时，且无进度查询 |
| P1-7 | 错误信息用 enum repr 暴露给前端 | `pipeline.py:40` 等 | UI 显示 `当前状态 ProjectStatus.VIDEO_READY ...`，应为 `.value` |
| P1-8 | 死代码 / 无用依赖 | `script_service.py:21 BIBLE_SYSTEM`（与 `bible_service.py:12` 重复且从未使用）、`main.py:8 StaticFiles`、`main.py:11 ProjectStatus`、`file_store.py:3 json`、`pyproject.toml` 的 `jinja2` | 误导后续开发；`ruff` 报 10 条 |
| P1-9 | 无 README，DESIGN 承诺的目录结构与实际不符 | 根目录 | DESIGN §2.3 列了 `video_service/audio_service/compose_service/ffmpeg.py` 等，实际不存在 |
| P1-10 | 无 `.env` 校验、无重试/退避、超时写死 120s、429 直接判 FAILED | `config.py`、`openrouter.py:43` | key 为空时只报 401 裸栈；一次限流毁掉整个项目 |

---

## 4. P2 · 质量与设计缺陷

1. **一致性校验名不副实**（`consistency_service.py`）
   - 无分镜校验：DESIGN §3.3 要求的「speaker 必须在该场 `character_in_frame`」完全没实现
   - `_check_numbers` 用子串匹配 `num_str not in known_facts_text`，`"30 万"` vs fact `"30万"` 会误报
   - `_check_names_in_text` 正则 `(?:^|[\s，。！？、])((?:小)?[一-龥]{2,4})(?:说|道|问|...)` 会命中「我知道他说」里的「知道」之类假阳性
   - `_check_timeline` 只看 `第N天`，heading 写成 `INT. 办公室 - 日` 时完全失效（实测 5 场无任何时间线检查）
2. **`video_prompt` 混入内部 id**：实测输出 `"Medium shot of char_3, a 45-year-old Asian man..."`。视频模型不认识 `char_3`，应注入 bible 的英文 `visual_prompt`（`storyboard_service._format_brief_bible` 把 `[char_1]` 前缀一起喂给了模型）
3. **world_state merge 语义不符**：`open_threads` 是**整体替换**而非合并（`world_state.py:48`），`list(set(...))` 导致 `knows/possesses` 顺序随机，diff 不可复现
4. **对白混入舞台提示**：实测 `"（看向窗外，眼神坚定）未来，才刚刚开始。"` 直接进 `dialogue.line`，TTS 会念出括号内容，且无任何清洗/校验
5. `Project.output_resolution`（`project.py:48`）从未被使用（因为合成没写）
6. 无成本/用量统计（DESIGN Phase 4 提了，现在是盲跑）
7. UI 状态标签 CSS 只覆盖 6 个状态，`video_ready` 等无样式；`statusTag` 直接拼 class 名

---

## 5. 建议的修复方案（按性价比排序）

### 批次 A —— 止血（半天，先让流程不卡死、不烧钱）
1. **加状态**：`ProjectStatus` 增加 `SCRIPT_CONFIRMED` / `STORYBOARD_CONFIRMED`，去掉 `confirm_*` 里的连续赋值；`generate_script` 白名单收紧为 `{DRAFT, FAILED}`，已确认后必须显式「退回重生成」（新接口 `POST /script/reopen`）。
2. **加统一门控**：`Pipeline` 里加 `_require(project, allowed: set[ProjectStatus], action: str)`，`PUT` 接口也走它（`script_review`/`storyboard_review` 阶段才允许改），错误信息用 `project.status.value`。
3. **时长硬约束**：outline 阶段就把预算下发——`每场 ≈ target/场数`，把 `duration_budget` 写进每场的 prompt；生成后校验 `abs(total - target)/target > 0.2` 时按比例归一化并记 issue（`Script.total_duration_sec` 终于派上用场）。分镜同理，按 `scene.duration_sec` 分配。
4. **字段容错**：给 `Scene/Shot.duration_sec` 加 `field_validator(mode="before")`，正则抽数字、四舍五入、非法回退默认值。
5. **密钥与暴露面**：轮换 OpenRouter key；`run.sh` 改 `--host 127.0.0.1`；`config.py` 启动时校验 `openrouter_api_key` 非空并在 `/health` 暴露。

### 批次 B —— 鲁棒性（1-2 天）
6. **`_extract_json` 重写**：先找第一个 `{` 到最后一个 `}` 的配平区间再 `json.loads`，失败则正则去尾逗号，仍失败抛可识别异常。
7. **重试闭环**：`chat_json` 接 `schema: type[BaseModel]`，校验失败把 Pydantic 报错回灌给模型重试 2 次；HTTP 层对 429/5xx 指数退避。
8. **逐场落盘 + 断点续跑**：`generate_script` 每场写完就写 `scenes/scene_N.json`，重跑时跳过已完成场次。
9. **原子写 + 锁**：`FileStore._save` 改 `tempfile` + `os.replace`；`Pipeline` 按 `project_id` 加 `asyncio.Lock`。
10. **异步任务化**：`POST .../generate` 立即返回 `job_id`，后台跑，`GET .../jobs/{id}` 查进度（也为 Phase 2 的视频轮询铺路）。

### 批次 C —— 结构与补齐（2-3 天）
11. `chat_json` 提到 `LLMProvider` ABC；`get_llm()` 读 `Settings.llm_provider`，支持注入（测试就能全离线跑）。
12. `app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parents[2] / "static"))`，并在 `pyproject.toml` 加 `[tool.setuptools.package-data]`。
13. 补 `tests/`：FakeLLM fixture + 状态机全路径测试 + `_extract_json` 表驱动测试（本次用的桩代码可直接改造成测试）。
14. `video_prompt` 只注入英文 `visual_prompt`，剥掉 `[char_N]` id；对白清洗括号内容；`open_threads` 改真合并、`knows/possesses` 改有序去重。
15. 删死代码（`BIBLE_SYSTEM`、无用 import、`jinja2` 依赖），补 README，修正 DESIGN §2.3 目录结构。

### 批次 D —— Phase 2/3（DESIGN 原计划）
16. `VideoProvider` ABC + `MockVideoProvider`（纯色占位）→ `video_service` 并发 + 失败重试 → `TTSService`(edge-tts) → BGM 曲库 → `compose_service`(ffmpeg) → 打通 `theme → 成片`。

---

## 6. 附：本次验证手段（可复现）

- 离线桩：`FakeLLM` 注入 `bible/script/storyboard` service，跑通全状态机并暴露 P0-5/P0-6
- 真实 E2E：`uvicorn videogen.main:app --port 8123` + `curl` 建项目 → 生成圣经(9.6s) → 生成剧本(30.1s) → 确认 → 再次生成(28.4s，被错误放行) → 生成分镜(35.9s) → 确认 → 暴露 P0-1
- 产物分析：`script.json` 总时长 225s（目标 45s）、`storyboard.json` 18 镜 180s、`video_prompt` 含 `char_3`
- 测试期间创建的项目 `cca411b323bf` 已删除，工作区已复原；测试服务已停止
