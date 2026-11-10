"""소리 크기(dBFS) 계산 공통 부품. 메아리 측정(listen/echo_probe.py)과 말하는 도중 끼어들기(listen/level_barge.py,
speak/reference.py)가 같은 계산을 쓰게 하려고 한곳에 둔다(2026-10-18)."""

from __future__ import annotations

import numpy as np

from voicelib.listen import FRAME_SAMPLES, SAMPLE_RATE

FLOOR_DB = -100.0  # 무음 프레임의 크기(dBFS)
ACTIVE_DB = -60.0  # 재생 소리가 이보다 크면 "재생 중"으로 본다


def frame_db(audio: np.ndarray) -> np.ndarray:
    """프레임(512 샘플)마다 RMS 크기(dBFS). 남는 끝 조각은 버린다."""
    audio = np.asarray(audio, dtype="float32")
    count = len(audio) // FRAME_SAMPLES
    if count == 0:
        return np.zeros(0, dtype="float64")
    frames = audio[: count * FRAME_SAMPLES].reshape(count, FRAME_SAMPLES).astype("float64")
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return np.maximum(20 * np.log10(np.maximum(rms, 1e-12)), FLOOR_DB)


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    """단일 채널 소리를 16kHz 로 바꾼다(선형 보간, 크기 비교·특징 추출용)."""
    audio = np.asarray(audio, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate == SAMPLE_RATE or len(audio) == 0:
        return audio.copy()
    length = int(round(len(audio) * SAMPLE_RATE / rate))
    positions = np.arange(length) * (rate / SAMPLE_RATE)
    return np.interp(positions, np.arange(len(audio)), audio).astype("float32")


def window_max(db: np.ndarray, frames: int) -> np.ndarray:
    """프레임마다 그 프레임을 포함해 앞으로 frames 개(자기 자신 포함) 안의 가장 큰 크기. 모자란 앞쪽은 무음으로 채운다."""
    db = np.asarray(db, dtype="float64")
    if frames <= 1 or len(db) == 0:
        return db.copy()
    padded = np.concatenate([np.full(frames - 1, FLOOR_DB), db])
    return np.lib.stride_tricks.sliding_window_view(padded, frames).max(axis=1)
