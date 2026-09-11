# FIXES — v0.2 修复记录

> 对照 [REVIEW.md](REVIEW.md) 的问题清单逐条修复。验证方式：71 个离线测试（`FakeLLM` 注入）+ `ruff` + 一次真实 OpenRouter 端到端跑通。

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
