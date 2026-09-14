# tools/e2e · 不花钱的端到端冒烟

```bash
.venv/bin/python tools/e2e/mock_pipeline_e2e.py          # 跑完自动关服务
.venv/bin/python tools/e2e/mock_pipeline_e2e.py --keep   # 保留服务，自己去 UI 点
.venv/bin/python tools/e2e/mock_pipeline_e2e.py --help   # 源项目 / 端口 / 超时
```

解决什么问题：单元测试能证明每个函数对，但证明不了「整条链路接得起来」。
真实跑一遍要按秒付费（本项目一次 15 镜 ≈ $9.75）；这里把两个花钱的环节换成 mock：

| 环节 | 冒烟用的实现 | 产出 |
|------|--------------|------|
| 视频片段 | `VIDEO_PROVIDER=mock` | ffmpeg 画的**真 H.264**（540x960，纯色 + 横向扫过的白条） |
| 配音 | `TTS_PROVIDER=mock` | 按字数估时长的**可播放**音频 |
| 配音文案 LLM | 全关（`AUDIO_LLM_*`） | —— |
| 配乐 | 本机作曲（`BGM_ENGINE=generate`） | 真 WAV |
| 合成 / 响度 | 本机 ffmpeg | `output/final.mp4` |

文本链路（圣经 → 剧本 → 分镜）不跑：那要调真 LLM。脚本把 `--source` 项目
（默认 `e8e40d3d9d29`）的这些产物复制进新项目，并把状态置成 `storyboard_confirmed`，
从「生成视频片段」开始跑完整条媒体链路。

## 安全边界（不满足就别改）

- 服务用**独立 `PROJECTS_DIR`**（默认 `<repo>/.e2e/projects`）起在 8099，
  绝不读写 `projects/` 里的真实项目；`.e2e/` 已在 `.gitignore` 里
- `VIDEO_PROVIDER` / `TTS_PROVIDER` 都切 mock，`AUDIO_LLM_*` 全关 —— 任何一项漏掉
  都可能变成真花钱的调用（`server_env()` 里集中设置）
- 摘要里会算出 `$0`：产物全是本机渲染

## 它顺带验的两件容易回归的事

1. **token 策略**：写接口不带 `X-API-Token` 必须 401；只读产物 `?token=` 必须 200
   （`<video src>` / `<a download>` 带不了请求头，所以只读路径保留查询串鉴权）
2. **坏数据不影响全局**：放一个 `{"status": "NOT_A_STATUS"}` 的 `meta.json`，
   `GET /api/projects` 必须 200、坏项目被隔离成 `.json.bad`、其余项目照常列出

结果落在 `.e2e/report.json`；服务日志在 `.e2e/server.log`。

## 它证明不了什么

画面好不好看、配音像不像人、口型对不对、闪避压了多少 dB —— 这些要真模型/真素材，
冒烟只能证明**接线**是通的。别把 12 项全过当成「片子能看」。

---

# real_shot_e2e.py · 真花钱的小口径端到端（方案 B）

```bash
# 1) 先看账：复制源项目 + 调 preflight，一分钱不花
.venv/bin/python tools/e2e/real_shot_e2e.py --shots scene_1_shot_1,scene_2_shot_2,scene_3_shot_2
# 2) 确认过再真跑（真钱，必须显式 --yes）
.venv/bin/python tools/e2e/real_shot_e2e.py --shots <同上> --yes --force
# 3) $0 重量一遍（改过测量口径/想看最新数字时用，不播种、不调任何花钱接口）
.venv/bin/python tools/e2e/real_shot_e2e.py --verify-only
```

它补的是 mock 冒烟的盲区：**真 provider 的画面 + 真 TTS**。做法是
「复制源项目 → 只删掉指定 N 个镜头的片段与配音 → 让流水线只补这几条」，
其余镜头复用**已经付过钱**的片段，所以花费只跟 N 有关，而且**源项目一个字节都不动**
（复制到新 id，源项目只读）。

安全阀：

- 不带 `--yes` 就只播种 + 打印 preflight 的实测报价，**不会调用任何花钱接口**
- preflight 是权威口径（问 provider 的单价与档位），不是脚本自己估的
- 项目账本按项目累计，预算不够时流水线自己会拒绝开工
- `--verify-only` 走完全本机测量：只读已有产物，不联网、不花钱

跑完会量三件事并写进 `.e2e/real/report.json`：成片响度（对比目标 LUFS）、
逐条分段响度极差（v0.20 的逐条归一）、人声闪避深度（复用 `tools/music/*`，
residual 逐样本相减，精确口径）。

