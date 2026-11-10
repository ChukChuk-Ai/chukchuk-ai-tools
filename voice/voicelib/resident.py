"""호출·대화·종료의 로컬 상태 처리. 마이크·LLM·개인 파일에 접근하지 않는다.

원본 AI_Partner resident.py 에서 호출어 판단(SessionRouter 와 도우미)만 옮겼다. 원본의 대화창·프로그램 종료 명령과
대화 모드 요청 판단은 이 앱에 없는 기능이라 뺐다. 호출어는 두 가지다.
- 캐릭터 이름(앱이 {"names": [...]} 로 알려 준다): 문장 맨 앞, 그리고 character_name_end 면 문장 맨 끝에서 받는다.
- 추가 호출어(도구 설정): 맨 앞·끝, 뒤에 알려진 명령이 오면 중간, 소리가 비슷한 변형도 받는다(원본의 호출어 규칙).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher

from voicelib.settings import ResidentConfig


def normalize_phrase(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


_VOCATIVE = r"(?:님|야|아)?"   # 한국어 부르는 말 끝("○○야", "○○님")
_SEPARATORS = " ,，.!?。？！~:"
_BOUNDARY = r"[\s,，.!?。？！~:]"


def _spaced(word: str) -> str:
    """띄어 적혀도 맞게("척척 박사")."""
    return r"\s*".join(re.escape(ch) for ch in word if not ch.isspace())


def wake_request(text: str, words: tuple[str, ...]) -> str | None:
    """문장 맨 앞의 호출어 뒤 요청. 맨 앞에 호출어가 없으면 None. 대화 속 이름 언급은 호출로 보지 않는다."""
    for word in sorted(words, key=len, reverse=True):
        pattern = rf"^\s*{_spaced(word)}{_VOCATIVE}(?=$|{_BOUNDARY})[\s,，.!?。？！~:]*"
        match = re.match(pattern, text, re.IGNORECASE)
        if match:
            return text[match.end():].strip()
    return None


def _jamo(text: str) -> list[tuple[str, object]]:
    """한글 음절을 초성·중성·종성으로 풀어 소리가 비슷한지 비교할 수 있게 한다."""
    out: list[tuple[str, object]] = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            code -= 0xAC00
            out += [("i", code // 588), ("m", (code % 588) // 28)]
            if code % 28:
                out.append(("f", code % 28))
        else:
            out.append(("o", ch))
    return out


def sound_similarity(a: str, b: str) -> float:
    """두 글이 소리로 얼마나 비슷한지(0~1)."""
    return SequenceMatcher(None, _jamo(a), _jamo(b)).ratio()


# 호출어 변형(받아쓰기가 조금 틀리게 적은 호출어)을 인정하는 기준(원본 2026-10-15·16 실사용 값)
FUZZY_WAKE_SIMILARITY = 0.7
LOOSE_SINGLE_SIMILARITY = 0.75
_FUZZY_TAIL_CHARS = 2
POSITION_END, POSITION_MIDDLE = "end", "middle"
_REQUEST_EDGE = " ,，:~"
_MIN_END_REQUEST_CHARS = 2


def _split_after(text: str, count: int) -> tuple[str, str]:
    """원래 글을 normalize_phrase 로 센 count 글자 지점에서 (앞말, 나머지)로 나눈다."""
    seen = 0
    for index, ch in enumerate(text):
        if ch.casefold().isalnum():
            seen += 1
            if seen == count:
                return text[: index + 1].strip(), text[index + 1:].lstrip(_SEPARATORS)
    return text.strip(), ""


def _is_known_command(rest: str, targets: tuple[str, ...], stops: frozenset[str]) -> bool:
    """공백·기호를 뺀 글 rest 가 알려진 명령(프로그램 이름, 말 멈추기)으로 시작하는지."""
    return bool(rest) and (any(rest.startswith(known) for known in targets) or rest in stops)


def positional_wake_request(text: str, wake_words: tuple[str, ...], targets: tuple[str, ...], end_only: tuple[str, ...] = (),
                            stops: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """(위치, 요청) 또는 None. 문장 끝의 호출어면 그 앞을, 중간이면 바로 뒤에 알려진 명령이 올 때만 그 뒤를 요청으로 본다.
    end_only 는 문장 끝에서만 인정하는 말(캐릭터 이름·호출어의 일부)."""
    for word in sorted((*wake_words, *end_only), key=len, reverse=True):
        spaced = _spaced(word)
        end = re.search(rf"(?<={_BOUNDARY}){spaced}{_VOCATIVE}{_BOUNDARY}*$", text, re.IGNORECASE)
        if end:
            request = text[: end.start()].strip(_REQUEST_EDGE)
            if len(normalize_phrase(request)) >= _MIN_END_REQUEST_CHARS:
                return POSITION_END, request
        if word in end_only:
            continue
        for middle in re.finditer(rf"(?<={_BOUNDARY}){spaced}{_VOCATIVE}(?={_BOUNDARY}){_BOUNDARY}*", text, re.IGNORECASE):
            rest = text[middle.end():].strip()
            if _is_known_command(normalize_phrase(rest), targets, stops):
                return POSITION_MIDDLE, rest
    return None


def fuzzy_wake_request(text: str, wake_words: tuple[str, ...], targets: tuple[str, ...],
                       stops: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """(걷어 낸 앞말, 요청) 또는 None. 앞말이 호출어와 소리가 비슷하고 요청이 알려진 명령으로 시작할 때만 인정한다."""
    normalized = normalize_phrase(text)
    for word in wake_words:
        target = normalize_phrase(word)
        scored = [(sound_similarity(normalized[:cut], target), cut)
                  for cut in (len(target) - 1, len(target), len(target) + 1) if 2 <= cut < len(normalized)]
        cuts = [cut for score, cut in sorted(scored, reverse=True) if score >= FUZZY_WAKE_SIMILARITY]
        if len(target) > _FUZZY_TAIL_CHARS and normalized.startswith(target[-_FUZZY_TAIL_CHARS:]):
            cuts.append(_FUZZY_TAIL_CHARS)  # 호출어의 앞부분을 놓치고 뒤 두 글자만 들린 경우
        for cut in cuts:
            if _is_known_command(normalized[cut:], targets, stops):
                heard, request = _split_after(text, cut)
                if request:
                    return heard, request
    return None


_FIRST_TOKEN = re.compile(r"^\s*([^\s,，.!?。？！~:]+)(?:\s+([^\s,，.!?。？！~:]+))?")


def loose_wake_request(text: str, wake_words: tuple[str, ...]) -> tuple[str, str] | None:
    """(걷어 낸 앞말, 요청) 또는 None. 문장 맨 앞 한두 낱말이 호출어와 소리가 비슷하면 뒤에 무엇이 오든 호출로 인정한다.
    낱말이 호출어보다 길면 이름을 말하는 일반 말로 보고 넘긴다."""
    match = _FIRST_TOKEN.match(text)
    if match is None:
        return None
    first, second = match.group(1), match.group(2)
    for word in wake_words:
        target = normalize_phrase(word)
        candidates = [(first, match.end(1))]
        if second:
            candidates.append((first + second, match.end()))  # 띄어 받아 적힌 경우
        for token, end in candidates:
            heard = normalize_phrase(token)
            if heard.endswith(("님", "야", "아")) and len(heard) > len(target):
                heard = heard[:-1]
            floor = FUZZY_WAKE_SIMILARITY if token != first else LOOSE_SINGLE_SIMILARITY
            if 2 <= len(heard) <= len(target) and sound_similarity(heard, target) >= floor:
                return text[:end].strip(), text[end:].lstrip(_SEPARATORS)
    return None


@dataclass(frozen=True)
class RoutedInput:
    kind: str  # "ignore" | "wake" | "ask" | "end". ask 만 앱으로 보낸다
    text: str = ""


class SessionRouter:
    """호출 대기 ↔ 호출된 대화. 마이크를 켜거나 부르면 silence_timeout 동안 호출어 없이 듣는다."""

    def __init__(self, config: ResidentConfig, clock: Callable[[], float] = time.monotonic,
                 known_targets: tuple[str, ...] = ()) -> None:
        self._config = config
        # 호출어 변형·중간 호출어를 인정할 때 뒤에 와야 하는 알려진 명령 낱말(프로그램 이름 등, 공백·기호 뺀 형태)
        self.known_targets = tuple(sorted({t for t in map(normalize_phrase, known_targets) if len(t) >= 2}, key=len, reverse=True))
        self.last_fuzzy = ""      # 가장 최근 route 가 변형으로 인정한 앞말(알림용)
        self.last_position = ""   # 가장 최근 route 가 문장 끝·중간의 호출어로 인정했으면 그 위치
        self._clock = clock
        self._lock = threading.RLock()
        self._name_calls: tuple[str, ...] = ()
        self._active = False
        self._muted = False
        self._last_activity = clock()
        self._ends = {normalize_phrase(p) for p in config.end_phrases}
        self._stops = frozenset(normalize_phrase(p) for p in config.stop_commands)

    @property
    def wake_words(self) -> tuple[str, ...]:
        """캐릭터 이름과 추가 호출어(문장 맨 앞에서 받는다)."""
        return (*self._name_calls, *self._config.wake_words)

    @property
    def has_wake_words(self) -> bool:
        return bool(self.wake_words)

    def set_name_calls(self, names) -> None:
        """캐릭터 이름(대화명)으로 부를 수 있게 한다(바꾸면 이전 이름은 빠진다). 한 글자 이름·추가 호출어와 같은 이름은 받지 않는다."""
        if not self._config.character_name_wake:
            names = ()
        configured = {normalize_phrase(word) for word in self._config.wake_words}
        seen: list[str] = []
        for name in names:
            key = normalize_phrase(str(name))
            if len(key) >= 2 and key not in configured and key not in {normalize_phrase(n) for n in seen}:
                seen.append(str(name).strip())
        with self._lock:
            self._name_calls = tuple(seen)

    def is_stop(self, text: str) -> bool:
        """말 전체가 '그만'·'stop' 같은 말 멈추기 명령인지."""
        return normalize_phrase(text) in self._stops

    def is_end(self, text: str) -> bool:
        return normalize_phrase(text) in self._ends

    def mute(self, muted: bool) -> None:
        """마이크를 끄거나 켠다. 다시 켰을 때 지난 호출로 이어지지 않도록 호출 상태를 푼다."""
        with self._lock:
            self._muted = muted
            self._active = False

    def remaining_seconds(self) -> float | None:
        """호출된 대화가 호출 대기로 돌아가기까지 남은 시간(초). 호출된 대화 중이 아니면 None."""
        with self._lock:
            if self._muted or not self._active_now():
                return None
            return max(self._config.silence_timeout - (self._clock() - self._last_activity), 0.0)

    def end_call(self) -> bool:
        """호출된 대화를 바로 끝내고 호출 대기로 돌린다. 호출된 대화 중이었으면 True."""
        with self._lock:
            was_active = self._active_now()
            self._active = False
            return was_active

    def skip_wake(self) -> bool:
        """호출어를 들은 것으로 처리한다(마이크 버튼). 마이크가 꺼져 있으면 하지 않고 False."""
        with self._lock:
            if self._muted:
                return False
            self._active = True
            self.touch()
            return True

    def touch(self) -> None:
        """실제 말소리가 시작되거나 응답을 재생하는 동안 부른다(호출 유지 시간을 처음부터)."""
        with self._lock:
            self._last_activity = self._clock()

    def _active_now(self) -> bool:
        return self._active and self._clock() - self._last_activity < self._config.silence_timeout

    def awaiting_wake(self) -> bool:
        """호출을 기다리는 중인지(호출 대기 또는 마이크 꺼짐)."""
        with self._lock:
            return self._muted or not self._active_now()

    def _positional(self, text: str) -> tuple[str, str] | None:
        """문장 끝·중간의 추가 호출어, 그다음 문장 끝의 캐릭터 이름(중간은 가족 말이 많아 받지 않는다)."""
        found = positional_wake_request(text, self._config.wake_words, self.known_targets,
                                        end_only=self._config.wake_partials, stops=self._stops)
        if found is None and self._config.character_name_end and self._name_calls:
            found = positional_wake_request(text, (), self.known_targets, end_only=self._name_calls, stops=self._stops)
        return found

    def _fuzzy(self, text: str) -> tuple[str, str] | None:
        """추가 호출어의 변형(캐릭터 이름은 변형을 받지 않는다: 일상 말에 비슷한 낱말이 많다)."""
        words = self._config.wake_words
        if not words:
            return None
        if self.known_targets and (found := fuzzy_wake_request(text, words, self.known_targets, self._stops)) is not None:
            return found
        return loose_wake_request(text, words)

    def expire(self, busy: bool = False) -> bool:
        """사용자 침묵만 센다. 응답 재생 중(busy)이면 시간을 늘린다. 호출이 방금 끝났으면 True."""
        with self._lock:
            if busy:
                self.touch()
            if self._active and not busy and self._clock() - self._last_activity >= self._config.silence_timeout:
                self._active = False
                return True
            return False

    def route(self, text: str) -> RoutedInput:
        with self._lock:
            if not text.strip() or self._muted:
                return RoutedInput("ignore")
            self.expire()
            self.last_fuzzy = self.last_position = ""
            waiting = not self._active
            request = wake_request(text, (*self.wake_words, *self._config.wake_partials) if waiting else self.wake_words)
            if waiting and request is not None and wake_request(text, self.wake_words) is None:
                stripped = text.strip()
                self.last_fuzzy = stripped[: len(stripped) - len(request)].strip(" ,，.!?~:")
            if request is None and waiting:
                if (positional := self._positional(text)) is not None:
                    self.last_position, request = positional
                elif (fuzzy := self._fuzzy(text)) is not None:
                    self.last_fuzzy, request = fuzzy
            if waiting and request is None:
                return RoutedInput("ignore")
            text = text.strip() if request is None else request
            if self.is_end(text):
                self._active = False
                return RoutedInput("end")
            self._active = True
            self.touch()
            return RoutedInput("ask", text) if text else RoutedInput("wake")
