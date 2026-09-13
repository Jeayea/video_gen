# 逐帧口型（Wav2Lip ONNX，本地、免费、不需要 torch）

把画面里的**嘴**按我们的配音逐帧重画。和「口型对齐」（把配音整体延后到画面开口的
时刻）不是一回事，两者是不同层次：

| 方案 | 做什么 | 代价 | 局限 |
|---|---|---|---|
| 口型对齐（v0.15） | 把配音**整体延时**到画面开口 | 本地计算，$0 | 只是起点对齐，一句话内部的节奏对不上 |
| 逐帧口型（本目录） | 按配音**逐帧重画嘴部** | 本地 CPU，$0 | 96×96 的嘴放大回 1440 宽会偏软 |

## 装

```bash
tools/lipsync/install.sh          # 建 .venv-lipsync + 下载权重（约 145MB）+ 自检
```

然后在 `.env` 里打开：

```ini
LIPSYNC_ENGINE=wav2lip-onnx
```

主环境 `videogen` 一个依赖都不加：引擎跑在独立环境里，按进程调用。

## 为什么是 ONNX 版

原版 Wav2Lip 要 torch + librosa + scipy + opencv（3GB 起），权重还在 Google Drive 上。
这里只需要 `onnxruntime + numpy + opencv-python-headless`（约 120MB），
Apple Silicon 的 CPU 上 **5 秒 1440×2560 的镜头约 5 秒**（15 个镜头整片 62.5 秒）。

## 用法

```bash
# 单镜头
.venv-lipsync/bin/python tools/lipsync/wav2lip_onnx.py \
    --video clip.mp4 --audio dub.wav --out synced.mp4 --report synced.json

# 整片：直接走合成流水线（compose 会自动逐镜头调用）
LIPSYNC_ENGINE=wav2lip-onnx .venv/bin/python -m uvicorn videogen.main:app
```

产物落在 `projects/<id>/output/lipsynced/<shot>.synced.mp4`（不进临时目录清理，
重跑合成直接复用），逐镜统计写在 `output/lipsync_report.json`。

想换 LatentSync / MuseTalk（需要 CUDA + torch）就改用 `cli` 引擎，
填一个带 `{video} {audio} {out}` 占位符的命令模板即可，其余不用改。

## 三个必须写下来的坑（都是真实跑才暴露的）

### 1. 6 通道输入必须是「下半张脸被抹掉 + 原图」

原版 `inference.py` 里是 `img_masked[:, img_size//2:] = 0`，作用在 HWC 的第 1 轴（高），
也就是把**行 48:96（下半张脸、嘴）置 0**，再和原图拼成 6 通道。

这一条写错会**静默失效**：两路都给原图时，模型直接从输入里「抄」出嘴，
输出与输入的最大差异只有 **0.011** —— 口型完全没生效，但流程一切正常、无报错。
用遮罩版本时同一位置的 mel 灵敏度是 **0.514**，且上半张脸原样保留（不会出现缝合线）。

> 现在的防护：脚本会统计 `mean_patch_change`（贴回区域相对输入的平均改动量），
> 低于 0.01 就报警并让本次口型判失败（`lipsync_service` 里也会拒绝这种结果）。

### 2. 相对路径 + `cwd` + 软链

- 命令带着 `cwd=项目目录` 跑时，`.venv-lipsync/bin/python` 会被解析成
  「项目目录/.venv-lipsync/bin/python」→ `FileNotFoundError`；
- 用 `Path.resolve()` 转绝对路径会**跟随符号链接**：venv 的 `bin/python` 是指向
  系统 python 的软链，resolve 之后就变成系统解释器、venv 被绕过
  → `ModuleNotFoundError: No module named 'cv2'`。

现在统一用 `os.path.abspath`（只补目录、不解析软链），且不传 `cwd`。

### 3. 音频比画面短时不能「重复最后一帧 mel」

原版在 mel 不够时重复最后一帧，配音只有 1.3 秒、镜头 5 秒时，嘴会一直停在最后一个
口型上（张嘴不动）。这里补 **0**（归一化后正好是静音 mel）→ 说完就闭嘴，
顺带解决了「尾音早没了嘴还在动」。

## 画质处理

模型只输出 96×96 的人脸，直接整块贴回 1440 宽等于 15 倍上采样（蜡像 + 底边一条硬缝），
所以做了三件事：

1. **只贴模型真正改过的地方**（`--mask-mode lips`）：用生成图与原图的差值做遮罩，
   皮肤保持原分辨率，只有嘴唇参与替换；
2. **嘴唇核心区一定替换**（`--core 0.62`）：只用差值遮罩会把轻声/闭口音的细微动作
   吃掉（实测出现「配音已经开始了但嘴前 0.4 秒没动」）；
3. **高频细节叠回**（`--detail-gain 0.35`）+ 轻锐化：把原图的毛孔/唇纹叠回生成结果。

裁剪默认用 `--crop-mode mouth`（以嘴为中心的小方框）：模型分辨率固定 96×96，
**框越小，嘴占的像素越多**。整脸裁剪用 `--crop-mode face --paste-top 0.55`。

## mel 前端：与 librosa 逐格一致

mel 是口型的驱动信号，实现对不上就会「嘴在动、但动的是别的音」——这种错误看画面看不出来。
所以把 `wav2lip_mel.py`（纯 numpy，不需要 cv2/onnxruntime）单独拆出来，
用 `check_mel.py` 和真 librosa 逐格相减：

```bash
python3 -m venv /tmp/melvenv && /tmp/melvenv/bin/pip install librosa
/tmp/melvenv/bin/python tools/lipsync/check_mel.py --audio <任意配音.wav>
```

实测（`tts_ref.wav`，149 帧）：

| 环节 | 与 librosa 的最大差 |
|---|---|
| 窗函数（周期 Hann） | 4.4e-16 |
| mel 滤波器组（Slaney 归一化） | 1.6e-09 |
| STFT 幅度谱 | 5.6e-06 |
| **完整 mel 频谱图** | **8.9e-08**（超过 0.01 的格子 0.0000%） |

途中修掉两处（都是这条对比才暴露的）：

1. **补边方式**：librosa ≥ 0.10 默认 `pad_mode="constant"`（补 0），0.9 及更早是 `"reflect"`。
   原来用了 reflect → 与今天的 librosa 最大差 **0.61**；换 constant 后 5.6e-06。
   差异只落在前/后各 2 帧的边界窗上，但能对齐就没有理由不对齐。
2. **预加重**：Wav2Lip 的 `audio.py` 是 `_stft(_preemphasis(y))`（`lfilter([1, -0.97], [1], y)`）。
   对比脚本一开始漏了这一步，于是「实现是对的、参考是错的」，看起来像实现有 bug。

## 关于「自动测口型对不对」——试过，没成

想过用便宜的办法给出口型质量的数字，四种像素统计量都试了，拿**模型原生语音**
（天生同步、可作基准）去验：

| 统计量 | 与原生语音的最佳相关（应≈0 滞后） |
|---|---|
| 画面差异 | r=0.25 @ +0.33s |
| 亮度（暗部） | r=0.08 @ -0.50s |
| 暗部占比 | r=0.08 @ -0.50s |
| 垂直边缘能量 | r=0.15 @ +0.50s |

**连基准自己都只有 r≈0.5、而且最佳滞后 0.1~0.25 秒** —— 说明这些二维统计量根本
量不准嘴的开合（特写镜头里摇头、点头、机位微移都会盖过嘴）。用它下结论是不诚实的，
所以没有把它做成「通过/不通过」的判据，只保留：

- 引擎自检 `mean_patch_change`（证明嘴真的被改了）
- `rewrite_activity`（相对原片段「嘴部被改写」的时段，说明哪里真的改过）
- **人工看帧 + 对比片**（最后的判据）

自动判据要么上 SyncNet 这类训练好的音画同步网络，要么接人脸关键点做嘴部开合，
两条路都要额外模型，留作后续。

## 已知限制

- **软**：96×96 的嘴放大回 1440 宽，特写镜头能看出来；要更清楚需要接
  GFPGAN/CodeFormer 做脸部修复，或换 LatentSync 这类扩散模型（要 CUDA）。
- **只改嘴**：头姿、眼神、身体动作仍是原片段的；如果原片段整个人都在演另一句话的
  节奏，光靠重画嘴救不回来。
- **一次一张脸**：多人同框只处理最靠中间/上一帧最近的那张脸。
- **不改变时长**：口型是逐帧重画，镜头时长不变（不会为了配音去拉长画面）。
