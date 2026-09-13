#!/usr/bin/env python3
"""Wav2Lip 的音频前端：mel 频谱（只用 numpy，不依赖 librosa/scipy）。

单独拆成一个模块有两个原因：
1. 这一层的正确性直接决定「嘴型对不对得上声音」，必须能**脱离 cv2/onnxruntime 单独验证**；
   验证脚本见 `tools/lipsync/check_mel.py`（拿真 librosa 逐格对比）。
2. 上层 `wav2lip_onnx.py` 只关心画面，音频前端可以整体换掉。

参数与原版 Wav2Lip 的 `audio.py` / `hparams.py` 对齐：
n_fft=800, hop=200, win=800, n_mels=80, fmin=55, fmax=7600, preemphasis=0.97,
ref_level_db=20, min_level_db=-100，mel 归一化到 0..1。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "HOP_SIZE",
    "MEL_STEP",
    "MIN_LEVEL_DB",
    "N_FFT",
    "N_MELS",
    "PREEMPHASIS",
    "REF_LEVEL_DB",
    "SAMPLE_RATE",
    "WIN_SIZE",
    "mel_chunks_for_frames",
    "mel_filterbank",
    "mel_spectrogram",
    "stft",
]

SAMPLE_RATE = 16000
N_FFT = 800
HOP_SIZE = 200
WIN_SIZE = 800
N_MELS = 80
F_MIN = 55.0
F_MAX = 7600.0
PREEMPHASIS = 0.97
REF_LEVEL_DB = 20.0
MIN_LEVEL_DB = -100.0
#: 每个视频帧对应的 mel 窗口长度（原版固定 16）
MEL_STEP = 16


def hz_to_mel(freq: np.ndarray | float) -> np.ndarray:
    """librosa 的 Slaney mel 刻度（htk=False）。"""
    values = np.asarray(freq, dtype=np.float64)
    f_min, f_sp = 0.0, 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = math.log(6.4) / 27.0
    linear = (values - f_min) / f_sp
    logarithmic = min_log_mel + np.log(np.maximum(values, 1e-10) / min_log_hz) / logstep
    return np.where(values >= min_log_hz, logarithmic, linear)


def mel_to_hz(mel: np.ndarray | float) -> np.ndarray:
    values = np.asarray(mel, dtype=np.float64)
    f_min, f_sp = 0.0, 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = (min_log_hz - f_min) / f_sp
    logstep = math.log(6.4) / 27.0
    linear = f_min + f_sp * values
    logarithmic = min_log_hz * np.exp(logstep * (values - min_log_mel))
    return np.where(values >= min_log_mel, logarithmic, linear)


def mel_filterbank(
    sr: int = SAMPLE_RATE,
    n_fft: int = N_FFT,
    n_mels: int = N_MELS,
    fmin: float = F_MIN,
    fmax: float = F_MAX,
) -> np.ndarray:
    """`librosa.filters.mel(norm="slaney")` 的等价实现，形状 (n_mels, 1+n_fft//2)。"""
    fft_freqs = np.linspace(0.0, sr / 2.0, 1 + n_fft // 2)
    mel_points = mel_to_hz(np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2))
    fdiff = np.diff(mel_points)
    ramps = mel_points[:, None] - fft_freqs[None, :]
    lower = -ramps[:n_mels] / fdiff[:n_mels, None]
    upper = ramps[2:] / fdiff[1 : n_mels + 1, None]
    weights = np.maximum(0.0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_points[2 : n_mels + 2] - mel_points[:n_mels])
    return weights * enorm[:, None]


def stft(y: np.ndarray, n_fft: int = N_FFT, hop: int = HOP_SIZE) -> np.ndarray:
    """`librosa.stft` 的等价实现：居中、**零**补边、周期 Hann 窗。

    补边方式要和 librosa 对齐：librosa ≥ 0.10 默认 `pad_mode="constant"`（补 0），
    0.9 及更早才是 `"reflect"`。实测（`check_mel.py`）用 reflect 时与今天的 librosa
    最大差 0.61，换成 constant 后是 5.6e-06 —— 差异只在前/后各 2 帧的边界窗上，
    但既然能对齐就没有理由不对齐。
    """
    pad = n_fft // 2
    padded = np.pad(y, (pad, pad), mode="constant")
    window = np.hanning(n_fft + 1)[:-1]  # 周期 Hann（= scipy get_window(fftbins=True)）
    frames = np.lib.stride_tricks.sliding_window_view(padded, n_fft)[::hop]
    frames = frames[: 1 + len(y) // hop]
    return np.fft.rfft(frames * window, n=n_fft, axis=1).T  # (1+n_fft//2, T)


def mel_spectrogram(y: np.ndarray) -> np.ndarray:
    """Wav2Lip 的 `Audio.melspectrogram`：返回 (80, T)，取值 0..1。"""
    basis = mel_filterbank()
    emphasized = np.append(y[:1], y[1:] - PREEMPHASIS * y[:-1])
    mel = basis @ np.abs(stft(emphasized))
    log_mel = 20.0 * np.log10(np.maximum(1e-5, mel)) - REF_LEVEL_DB
    return np.clip((log_mel - MIN_LEVEL_DB) / -MIN_LEVEL_DB, 0.0, 1.0)


def mel_chunks_for_frames(mel: np.ndarray, frames: int, fps: float) -> np.ndarray:
    """每个视频帧取一段 16 帧 mel（原版口径）；音频不够就补静音（归一化后正好 0）。

    原版在 mel 不够时**重复最后一帧**，配音比画面短时嘴会停在最后一个口型上；
    这里补 0 = 静音 mel，说完就闭嘴。
    """
    out = np.zeros((frames, 1, N_MELS, MEL_STEP), dtype=np.float32)
    multiplier = 80.0 / max(fps, 1e-6)
    total = mel.shape[1]
    for index in range(frames):
        start = int(index * multiplier)
        if start >= total:
            continue
        width = min(MEL_STEP, total - start)
        out[index, 0, :, :width] = mel[:, start : start + width]
    return out
