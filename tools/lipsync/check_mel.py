#!/usr/bin/env python3
"""验证 `wav2lip_mel.py` 的 mel 前端和**真 librosa** 逐格一致。

为什么值得单独写一个：mel 是口型的驱动信号，实现对不上就会「嘴在动、但动的是别的音」，
而这种错误看画面是看不出来的（只会觉得「有点怪」）。这个脚本把两边的频谱图逐格相减。

它只需要 numpy（`wav2lip_mel.py` 本身也不依赖 cv2/onnxruntime），
所以可以在这两个环境里跑：

    # 临时环境（含 librosa）
    python3 -m venv /tmp/melvenv && /tmp/melvenv/bin/pip install librosa
    /tmp/melvenv/bin/python tools/lipsync/check_mel.py --audio dub.wav

    # 或者本来就装了 librosa 的环境
    python tools/lipsync/check_mel.py --audio dub.wav
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wav2lip_mel import (
    N_FFT,
    N_MELS,
    SAMPLE_RATE,
    mel_spectrogram,
    stft,
)


def read_16k(path: Path) -> np.ndarray:
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
        ],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> int:
    parser = argparse.ArgumentParser(description="对照 librosa 验证 mel 前端")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--tolerance", type=float, default=1e-4)
    args = parser.parse_args()

    reference_wav = Path(args.audio)
    y = read_16k(reference_wav)
    mine = mel_spectrogram(y)
    print(
        f"自实现 mel：shape={mine.shape} dtype={mine.dtype} "
        f"范围=[{mine.min():.3f}, {mine.max():.3f}]"
    )

    try:
        import librosa
    except ImportError:
        print("没有装 librosa，只输出了自实现结果（想对比就 pip install librosa）")
        return 0

    # 关键：参考实现也用**同一段信号**（ffmpeg 解出来的 16k），否则比的是两个
    # 重采样器（ffmpeg vs soxr）而不是 mel 前端
    y_ref = y
    mel_basis = librosa.filters.mel(
        sr=SAMPLE_RATE, n_fft=N_FFT, n_mels=N_MELS, fmin=55.0, fmax=7600.0
    )
    # 预加重别漏：Wav2Lip 的 audio.py 是 _stft(_preemphasis(y))，
    # _preemphasis 用 scipy.signal.lfilter([1, -0.97], [1], y)
    emphasized = np.append(y_ref[:1], y_ref[1:] - 0.97 * y_ref[:-1])
    spec = librosa.stft(y=emphasized, n_fft=N_FFT, hop_length=200, win_length=800)
    # 对应 Wav2Lip audio.py：_amp_to_db(_linear_to_mel(|D|)) - ref_level_db → _normalize
    log_mel = 20.0 * np.log10(np.maximum(1e-5, mel_basis @ np.abs(spec))) - 20.0
    reference = np.clip((log_mel - (-100.0)) / 100.0, 0.0, 1.0)

    frames = min(mine.shape[1], reference.shape[1])
    diff = np.abs(mine[:, :frames] - reference[:, :frames])
    print(f"librosa 参考：shape={reference.shape}")
    print(f"逐格最大差 {diff.max():.3e} / 平均差 {diff.mean():.3e}")
    print(f"超过 0.01 的格子占比 {100 * (diff > 0.01).mean():.4f}%")

    # 顺带对一下 STFT 本身（mel 之外，帧数/尺度错了也会在这里暴露）
    ref_spec = np.abs(librosa.stft(y=y_ref, n_fft=N_FFT, hop_length=200, win_length=800))
    my_spec = np.abs(stft(y_ref))
    n = min(ref_spec.shape[1], my_spec.shape[1])
    spec_diff = np.abs(my_spec[:, :n] - ref_spec[:, :n])
    print(f"STFT 幅度谱最大差 {spec_diff.max():.3e}（{my_spec.shape} vs {ref_spec.shape}）")

    # 另外报一下「经过各自的重采样器」后的差异：这一项天然更大，属于预期
    y_soxr = librosa.load(str(reference_wav), sr=SAMPLE_RATE)[0]
    if len(y_soxr) != len(y):
        print(f"重采样差异：ffmpeg {len(y)} 点 vs soxr {len(y_soxr)} 点（本就不同，属预期）")

    ok = diff.max() < args.tolerance
    print("✅ 与 librosa 一致" if ok else f"❌ 差异超过容差 {args.tolerance}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
