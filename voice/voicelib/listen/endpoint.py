"""말 끝 판단. 프레임마다 말소리 확률을 받아 한 번의 발화가 어디서 시작하고 끝나는지 정한다."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from voicelib.settings import TurnConfig
from voicelib.listen import FRAME_SECONDS

SPEECH_THRESHOLD = 0.5
# 이만큼 말소리가 이어져야 말이 시작됐다고 본다. 짧은 잡음에 반응하지 않기 위함
MIN_SPEECH_SECONDS = 0.2
# 말이 시작되기 직전의 소리도 이만큼 함께 넘긴다. 첫 음절이 잘리지 않게 한다
PRE_ROLL_SECONDS = 0.3
# 멈춘 뒤 이만큼 지나서 억양 모델에게 묻는다
ASK_AFTER_SECONDS = 0.2
# 이보다 긴 멈춤만 "말하는 도중의 멈춤" 으로 센다
MIN_PAUSE_SECONDS = 0.3
MAX_UTTERANCE_SECONDS = 30
# 끝난 뒤의 침묵은 이만큼만 남긴다
TAIL_SECONDS = 0.3


@dataclass(frozen=True)
class Utterance:
    audio: np.ndarray
    pauses: int  # 말하는 도중에 멈춘 횟수
    waited: float  # 마지막 말소리 뒤 끝났다고 판단하기까지 기다린 시간(초)
    ended_by: str  # "smart_turn" | "limit" | "silence" | "max_length"


class Endpointer:
    def __init__(
        self,
        turn: TurnConfig,
        complete_probability: Callable[[np.ndarray], float] | None = None,
    ) -> None:
        self._turn = turn
        # 억양 모델을 쓰지 않으면 침묵 길이(wait)만으로 판단한다
        self._complete_probability = complete_probability if turn.smart_turn else None
        self._complete_model = complete_probability
        self._recent: deque[np.ndarray] = deque(maxlen=round(PRE_ROLL_SECONDS / FRAME_SECONDS))
        # 말이 시작될 때마다 1 씩 늘어난다. 바깥에서 "새로 말하기 시작했는지" 알아보는 데 쓴다
        self.epoch = 0
        self.reset()

    def configure_turn(self, turn: TurnConfig) -> None:
        """듣는 스레드에서만 호출한다. 모드 전환 시 미완성 조각을 버리고 epoch는 보존한다."""
        self._turn = turn
        self._complete_probability = self._complete_model if turn.smart_turn else None
        self.reset()

    @property
    def speaking(self) -> bool:
        return self._speaking

    def reset(self) -> None:
        """듣던 것을 버리고 처음 상태로 돌아간다."""
        self._speaking = False
        self._candidate: list[np.ndarray] = []
        self._frames: list[np.ndarray] = []
        self._silence = 0.0
        self._pauses = 0
        self._complete: bool | None = None
        self._recent.clear()

    def flush(self) -> Utterance | None:
        """유한 입력의 끝에서 남은 발화를 돌려준다. 실시간 듣기에서는 호출하지 않는다."""
        if not self._speaking:
            self.reset()
            return None
        return self._finish("window_end")

    def snapshot(self) -> np.ndarray | None:
        """실시간 받아쓰기용 현재 발화 사본. 말끝 판단과 녹음 수집은 계속한다."""
        return np.concatenate(self._frames) if self._speaking and self._frames else None

    @property
    def sample_count(self):
        return sum(len(frame) for frame in self._frames) if self._speaking else 0

    def feed(self, frame: np.ndarray, probability: float) -> Utterance | None:
        """프레임 하나를 넣는다. 발화가 끝났다고 판단하면 그 발화를 돌려준다."""
        is_speech = probability >= SPEECH_THRESHOLD
        if not self._speaking:
            self._wait_for_start(frame, is_speech)
            return None

        self._frames.append(frame)
        if is_speech:
            if self._silence >= MIN_PAUSE_SECONDS:
                self._pauses += 1
            self._silence = 0.0
            self._complete = None
            if len(self._frames) * FRAME_SECONDS >= MAX_UTTERANCE_SECONDS:
                return self._finish("max_length")
            return None

        self._silence += FRAME_SECONDS
        if self._complete_probability is None:
            return self._finish("silence") if self._silence >= self._turn.wait else None

        if self._complete is None and self._silence >= ASK_AFTER_SECONDS:
            self._complete = self._complete_probability(np.concatenate(self._frames)) > 0.5
        if self._complete and self._silence >= self._turn.wait:
            return self._finish("smart_turn")
        if self._silence >= self._turn.limit:
            return self._finish("limit")
        return None

    def _wait_for_start(self, frame: np.ndarray, is_speech: bool) -> None:
        if not is_speech:
            self._recent.extend(self._candidate)
            self._candidate = []
            self._recent.append(frame)
            return
        self._candidate.append(frame)
        if len(self._candidate) * FRAME_SECONDS >= MIN_SPEECH_SECONDS:
            self._frames = [*self._recent, *self._candidate]
            self._candidate = []
            self._recent.clear()
            self._speaking = True
            self._silence = 0.0
            self._pauses = 0
            self._complete = None
            self.epoch += 1

    def _finish(self, ended_by: str) -> Utterance:
        drop = max(0, round((self._silence - TAIL_SECONDS) / FRAME_SECONDS))
        frames = self._frames[: len(self._frames) - drop] if drop else self._frames
        utterance = Utterance(np.concatenate(frames), self._pauses, round(self._silence, 2), ended_by)
        self.reset()
        return utterance
