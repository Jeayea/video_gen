# 配乐工具

配乐引擎本体在 `src/videogen/utils/music.py`（作曲/合成）与
`src/videogen/services/music_service.py`（引擎解析/混响/响度/缓存）。
这里放的是**验证「配乐到底有没有按预期生效」**的两个脚本，都是纯标准库，不需要额外依赖。

## measure_ducking.py —— 量人声闪避

```bash
.venv/bin/python tools/music/measure_ducking.py \
    --voice ab_music/audio/A_nobgm.wav \
    --variant "固定音量=ab_music/audio/B_noduck.wav" \
    --variant "人声闪避=ab_music/audio/C_duck.wav" \
    --reference "固定音量=ab_music/audio/B_noduck.wav" \
    --report ab_music/ducking_report.json
```

它自己找说话窗口（20ms 窗口 RMS，高于噪声底 6dB 且 ≥0.3s），
再逐窗口比较「音乐那一层」的音量。两种口径：

| 口径 | 算法 | 什么时候用 |
|------|------|-----------|
| `--mode energy`（默认） | 音乐能量 ≈ `混音² - 人声²` | 音乐本身就比较响的对照；音乐被压深（>15dB）后**会失真** |
| `--mode residual` | 逐样本相减（`混音 - 人声`） | 两版是同一渲染链、只有效果器开关不同时；**精确** |

判据（写死在脚本里，避免事后编标准）：说话窗口压降 ≥4dB、**长静音窗口（≥1.2s）**
差异 <1.5dB。短间隙里音乐还被压着是压缩器 release 的正常行为，所以不参与判定。

真实项目的实测（67 秒、11 镜）：说话段音乐 **-9.23dB**、长静音段 **+0.00dB**、人声不动。

## ducking_harness.py —— 用真实滤镜链复现一对混音

精确口径需要「同一渲染链、同一人声轨、只有闪避开关不同」的一对文件。这个脚本
调用应用里的 `compose_service.bgm_mix_filter()`（**不是抄一份滤镜串**）渲染出来：

```bash
.venv/bin/python tools/music/ducking_harness.py \
    --voice ab_music/audio/A_nobgm.wav \
    --music output/music/bgm.wav \
    --out ab_music/harness --duration 67.0
```

然后把输出的 `mix_off.wav` / `mix_on.wav` 喂给 `measure_ducking.py --mode residual`。

## 为什么不在成片里直接量

成片音轨里音乐只有 -41dB 左右、人声 -27dB，能量相减法的交叉项占主导：
同一个 9dB 的闪避会被读成 **39dB**（留档见真实项目
`projects/<id>/ab_music/ducking_report_infilm.json`）。工具现在会在估计口径
给出 >18dB 的压降时自己标注「不可信」。
