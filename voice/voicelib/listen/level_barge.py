"""캐릭터가 말하는 도중 사용자의 목소리가 들리면 말을 멈추고 듣기로 넘기기 — 소리 크기 규칙.

AI_Partner src/ai_partner/listen/level_barge.py 의 LevelFire·LevelBarge·classify_stop 을 복사(2026-11-08, 사용자 요청
"말소리가 들리면 음성 안내를 바로 끊고 받아쓰기"). 잘못 멈춤 통계 기록(BargeLog·PendingStops)은 옮기지 않았다.

규칙: 마이크 프레임(0.032초)마다 아래 셋이 모두 참인 상태가 hold_seconds(0.26초) 이어지면 사용자가 말하기 시작한 것으로 본다.
  ① 캐릭터가 최근 window_seconds(1.3초) 안에 소리를 냈다(그 안의 가장 큰 크기 > ACTIVE_DB)
  ② 말소리 검출이 말소리로 본다(확률 0.5 이상)
  ③ 마이크 크기가 "최근 재생 소리의 최댓값 + echo_gain_db(스피커→마이크 감쇠) + margin_db(15dB)"보다 크다
멈추면 말 시작 조금 앞(pre_roll)부터의 마이크 프레임을 돌려주어 평소 듣기로 넘긴다(내 말의 첫머리가 잘리지 않게).
멈춘 뒤 받아 적은 글이 비었거나 캐릭터가 방금 한 말이면 잘못 멈춤으로 보고 버린다(classify_stop).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

import numpy as np

from voicelib.audio_level import ACTIVE_DB, frame_db
from voicelib.listen import FRAME_SAMPLES, FRAME_SECONDS
from voicelib.listen.barge_in import normalize

SPEECH_PROBABILITY = 0.5
PRE_ROLL_SECONDS = 0.2  # 규칙이 처음 참이 된 프레임 조금 앞부터 평소 듣기로 넘긴다
MIN_TEXT = 2  # 받아 적은 글이 이보다 짧으면(정리한 글자 수) 빈 것으로 본다
ECHO_BLOCK_SHARE = 0.6  # 받아 적은 글의 이 비율 이상이 캐릭터 말과 이어서 같으면 캐릭터의 목소리로 본다
ECHO_BLOCK_MIN = 4  # 그리고 같은 부분이 이 글자 수 이상이어야 한다


@dataclass(frozen=True)
class LevelFire:
    """규칙이 맞아 멈춰야 할 때. frames 는 평소 듣기로 넘길 마이크 프레임(말 시작 조금 앞부터 지금까지)."""

    frames: list[np.ndarray]
    run_seconds: float  # 규칙이 이어진 시간
    peak_over_db: float  # 이어지는 동안 (마이크 − 예상 메아리)의 최댓값
    mic_db: float  # 이어지는 동안 마이크 크기 평균
    ref_db: float  # 멈출 때의 최근 재생 소리 최댓값


class LevelBarge:
    def __init__(
        self,
        margin_db: float = 15.0,
        hold_seconds: float = 0.26,
        gain_db: float = -22.0,
        pre_roll_seconds: float = PRE_ROLL_SECONDS,
    ) -> None:
        self.margin_db = margin_db
        self.gain_db = gain_db
        self.hold_frames = max(1, round(hold_seconds / FRAME_SECONDS))
        self.pre_roll_frames = max(0, round(pre_roll_seconds / FRAME_SECONDS))
        self._ring: deque[np.ndarray] = deque(maxlen=self.hold_frames + self.pre_roll_frames)
        self._run = 0
        self._peak = -1e9
        self._mic_sum = 0.0
        self.armed_frames = 0  # 규칙을 볼 수 있었던(캐릭터가 소리를 내던) 프레임 수. 잘못 멈춤을 1분당으로 세는 분모

    @property
    def armed_seconds(self) -> float:
        return self.armed_frames * FRAME_SECONDS

    def reset(self) -> None:
        """말하기가 끝났다(또는 멈췄다). 모으던 것을 버린다."""
        self._ring.clear()
        self._run, self._peak, self._mic_sum = 0, -1e9, 0.0

    def feed(self, frame: np.ndarray, probability: float, ref_db: float) -> LevelFire | None:
        """프레임 하나를 넣는다. 멈춰야 하면 LevelFire, 아니면 None. ref_db: 최근 window_seconds 안 재생 소리의 최대 크기."""
        if len(frame) != FRAME_SAMPLES:
            return None
        self._ring.append(frame)
        armed = ref_db > ACTIVE_DB
        if armed:
            self.armed_frames += 1
        mic_db = float(frame_db(frame)[0])
        over = mic_db - (ref_db + self.gain_db)
        if armed and probability >= SPEECH_PROBABILITY and over >= self.margin_db:
            self._run += 1
            self._peak = max(self._peak, over)
            self._mic_sum += mic_db
        else:
            self._run, self._peak, self._mic_sum = 0, -1e9, 0.0
            return None
        if self._run < self.hold_frames:
            return None
        fire = LevelFire(
            frames=list(self._ring), run_seconds=self._run * FRAME_SECONDS, peak_over_db=self._peak,
            mic_db=self._mic_sum / self._run, ref_db=ref_db,
        )
        self.reset()
        return fire


def classify_stop(heard: str, spoken: str) -> str:
    """멈춘 뒤 받아 적은 글이 진짜 내 말인지: 'real' | 'empty'(비었거나 너무 짧음) | 'echo'(캐릭터가 방금 한 말).

    캐릭터 말인지는 받아 적은 글이 캐릭터 말에 통째로 들어 있거나, 글의 60% 이상(4글자 이상)이 캐릭터 말과 끊김 없이 같을 때로 본다.
    진짜 내 말을 캐릭터 말로 잘못 버리지 않도록 일부 글자만 우연히 겹치는 정도로는 캐릭터 말로 보지 않는다.
    """
    said = normalize(heard)
    if len(said) < MIN_TEXT:
        return "empty"
    echo = normalize(spoken)
    if echo:
        if said in echo:
            return "echo"
        block = SequenceMatcher(None, said, echo, autojunk=False).find_longest_match(0, len(said), 0, len(echo)).size
        if block >= ECHO_BLOCK_MIN and block >= ECHO_BLOCK_SHARE * len(said):
            return "echo"
    return "real"
