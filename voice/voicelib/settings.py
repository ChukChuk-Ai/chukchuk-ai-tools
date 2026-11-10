"""음성 설정 묶음. 원본 AI_Partner config.py 의 STTConfig·TTSConfig·TurnConfig·BargeInConfig·ResidentConfig 를
듣기·말하기에 쓰는 칸만 남겨 옮겼다.

값은 두 겹이다.
1) 도구 폴더의 defaults.toml: 고급 값(원본에서 시험을 마친 값). 사용자는 보통 고치지 않는다.
2) 도구 설정(tool.json settings, 환경설정 > 도구 설정 > 음성): 사용자가 고르는 몇 칸. defaults.toml 위에 얹는다.
화면 언어(ko·en)는 받아쓰기 언어와 기본 목소리를 정한다.
"""
from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

STT_MODELS = ("auto", "large-v3-turbo", "medium", "small")
TTS_ENGINE_NAMES = ("edge", "windows")


@dataclass(frozen=True)
class STTConfig:
    model: str = "auto"                 # auto | large-v3-turbo | medium | small (도구 설정)
    gpu_model: str = "large-v3-turbo"   # auto 이고 NVIDIA GPU 가 있을 때
    cpu_model: str = "small"            # auto 이고 GPU 가 없을 때(또는 GPU 를 쓰지 못할 때)
    gpu_compute_type: str = "int8"
    cpu_compute_type: str = "int8"
    beam_size: int = 1
    language: str = "ko"                # 화면 언어로 채운다
    use_prompt: bool = True             # 캐릭터 이름·프로그램 이름을 받아쓰기에 미리 알려 준다(initial_prompt)
    prompt_extra: str = ""              # 더 알려 줄 낱말(쉼표)
    preview_model: str = "small"        # 말하는 동안 입력창 미리보기만 이 작은 모델로(본 모델이 더 클 때만)

    def __post_init__(self) -> None:
        if self.model not in STT_MODELS:
            raise ValueError(f"stt.model 은 {', '.join(STT_MODELS)} 중 하나여야 합니다: {self.model!r}")


@dataclass(frozen=True)
class TTSConfig:
    engines: tuple[str, ...] = ("edge", "windows")   # 앞에서부터 시도하고, 실패하면 다음으로
    audio_stream: bool = True    # edge 음성의 전체 수신을 기다리지 않고 준비된 부분부터 재생
    edge_voice: str = ""         # 비우면 default_voices 의 화면 언어 목소리
    windows_voice: str = ""      # 비우면 화면 언어의 첫 내장 목소리
    default_voices: dict = field(default_factory=lambda: {"ko": "ko-KR-SunHiNeural", "en": "en-US-AriaNeural"})
    lead_in: float = 0.6         # 출력 시작 보호 구간(초, 신호 포함). 첫 음절이 잘리지 않게. 0 이면 끔
    lead_in_tone: float = 0.05   # 시작 신호(800Hz 0.08초)의 크기(0~0.3). 0 이면 신호 없음
    lead_in_noise: float = 0.0   # 보호 구간에 까는 잡음의 크기(0~0.05). 0 이면 무음

    def __post_init__(self) -> None:
        if not self.engines or any(e not in TTS_ENGINE_NAMES for e in self.engines):
            raise ValueError(f"tts.engines 는 {', '.join(TTS_ENGINE_NAMES)} 중에서 고릅니다: {self.engines!r}")


@dataclass(frozen=True)
class TurnConfig:
    """말이 끝났다고 판단하는 규칙."""

    wait: float = 0.6             # 끝난 것 같을 때 이만큼 조용하면 끝으로 본다(초)
    limit: float = 1.5            # 덜 끝난 것 같아도 이만큼 조용하면 끝으로 본다(초)
    smart_turn: bool = True       # 억양으로 끝났는지 판단하는 모델(Smart Turn)을 쓸지. 끄면 wait 만 쓴다
    resume_after: float = 0.3     # 말하기가 끝난 뒤 이만큼 지나서 다시 듣는다(초). 스피커 소리의 꼬리를 피한다
    partial_min_seconds: float = 0.3       # 첫 미리보기 받아쓰기를 시작할 말소리 길이
    partial_interval_seconds: float = 0.2  # 미리보기 받아쓰기 최소 갱신 간격

    def __post_init__(self) -> None:
        for name in ("partial_min_seconds", "partial_interval_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .1 <= value <= 2:
                raise ValueError(f"turn.{name} 은 0.1~2초여야 합니다")


@dataclass(frozen=True)
class BargeInConfig:
    """말하는 도중 끼어들기."""

    enabled: bool = True
    # 받아 적은 짧은 말에 이 말이 있으면(방금 한 말에 없을 때) 말을 멈춘다
    phrases: tuple[str, ...] = ("그만", "멈춰", "중지", "스톱", "잠깐", "잠깐만", "stop", "wait")
    # 일상 문장에도 나오는 말: 말 전체가 이것일 때만 멈춘다
    pause_phrases: tuple[str, ...] = ("잠시만", "잠시만요", "잠깐", "잠깐만", "알았어", "오케이", "OK", "okay", "wait")
    mode: str = "both"            # phrases = 멈춤 말만, level = 또렷한 목소리면 무슨 말이든, both = 둘 다
    level_margin_db: float = 15.0      # 예상 메아리(최근 재생 크기 + echo_gain_db)보다 이만큼 커야 한다
    level_hold_seconds: float = 0.26   # 위 조건이 이만큼 이어져야 멈춘다
    level_window_seconds: float = 1.3  # 최근 이만큼의 재생 소리 중 가장 큰 것을 기준으로(스피커→마이크 지연)
    echo_gain_db: float = -22.0        # 스피커 소리가 마이크에 들어올 때 줄어드는 정도(dB)
    loud_db: float = -4.0              # 말하는 동안 마이크가 이 크기(dBFS) 이상으로 이어지면 무슨 소리든 멈춘다. 0 이면 끔
    loud_hold_seconds: float = 0.2

    def __post_init__(self) -> None:
        if not -40 <= self.loud_db <= 0 or not 0 < self.loud_hold_seconds <= 2:
            raise ValueError("barge_in.loud_db 는 -40~0(0 이면 끔), loud_hold_seconds 는 0~2초여야 합니다")
        if self.mode not in ("phrases", "level", "both"):
            raise ValueError(f"barge_in.mode 는 phrases, level, both 중 하나여야 합니다: {self.mode!r}")
        if not 0 < self.level_hold_seconds <= 2 or not 0 < self.level_window_seconds <= 5:
            raise ValueError("barge_in.level_hold_seconds 는 0~2초, level_window_seconds 는 0~5초여야 합니다")
        if not 0 < self.level_margin_db <= 60 or not -80 <= self.echo_gain_db <= 20:
            raise ValueError("barge_in.level_margin_db 는 0~60, echo_gain_db 는 -80~20 이어야 합니다")


@dataclass(frozen=True)
class ResidentConfig:
    """호출·대화·종료."""

    wake_words: tuple[str, ...] = ()     # 캐릭터 이름 말고 더 받을 호출어(도구 설정 '추가 호출어')
    wake_partials: tuple[str, ...] = ()  # 호출어의 일부만 들려도 호출로 보는 말(문장 맨 앞·끝)
    end_phrases: tuple[str, ...] = ("대화 끝", "대기해", "대기해 줘", "대기 모드", "that's all", "stop listening")
    # 말 전체가 이것이면 말 멈추기(대화는 끝내지 않는다)
    stop_commands: tuple[str, ...] = ("그만", "그만해", "멈춰", "멈춰줘", "말 멈춰", "스톱", "중지", "stop", "stop it", "stop talking")
    silence_timeout: float = 300.0       # 부른 뒤 이 시간(초) 동안은 호출어 없이 이어서 말한다
    max_pending: int = 3       # 호출 대기 중 밀린 소리가 이보다 많으면 받아 적지 않고 버린다. 0 이면 끔
    stale_seconds: float = 8.0  # 호출 대기 중 이보다 오래 기다린 소리는 버린다. 0 이면 끔
    min_speech: float = 0.6    # 호출 대기 중 이보다 짧은 말소리("응", "어")는 받아 적지 않는다. 0 이면 끔
    character_name_wake: bool = True   # 캐릭터 이름으로 부른다(문장 맨 앞)
    character_name_end: bool = True    # 캐릭터 이름을 문장 끝에서도 인정한다("…알려줘 ○○야")
    mic_always_on: bool = False        # 켜면 켜진 채 시작하고 대화가 끝나도 켜 둔다(도구 설정)

    def __post_init__(self) -> None:
        if not math.isfinite(self.silence_timeout) or self.silence_timeout <= 0:
            raise ValueError("resident.silence_timeout 은 양수여야 합니다")
        if isinstance(self.max_pending, bool) or not isinstance(self.max_pending, int) or not 0 <= self.max_pending <= 100:
            raise ValueError("resident.max_pending 은 0~100 정수여야 합니다")
        for name in ("stale_seconds", "min_speech"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"resident.{name} 은 0 이상이어야 합니다")
        if any(not word.strip() for word in (*self.wake_words, *self.wake_partials, *self.end_phrases)):
            raise ValueError("호출어와 끝말은 빈 글일 수 없습니다")


@dataclass(frozen=True)
class VoiceSettings:
    stt: STTConfig
    tts: TTSConfig
    turn: TurnConfig
    barge_in: BargeInConfig
    resident: ResidentConfig
    warnings: tuple[str, ...] = ()   # 읽지 못해 기본값으로 바꾼 칸(앱에 알린다)


def _make(cls, values: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: tuple(v) if isinstance(v, list) else v for k, v in (values or {}).items() if k in names})


def split_words(text) -> tuple[str, ...]:
    """'가, 나，다' → ('가', '나', '다'). 목록이면 그대로 정리한다."""
    items = text if isinstance(text, (list, tuple)) else re.split(r"[,，;]", str(text or ""))
    return tuple(dict.fromkeys(w.strip() for w in items if str(w).strip()))


def _overlay(base: VoiceSettings, user: dict, language: str) -> VoiceSettings:
    """도구 설정 값을 얹는다. 틀린 값은 그 칸만 기본값으로 두고 warnings 에 적는다."""
    warnings = []
    stt, tts, barge, resident = base.stt, base.tts, base.barge_in, base.resident

    def apply(section, name, **changes):
        try:
            return replace(section, **changes)
        except (TypeError, ValueError):
            warnings.append(name)
            return section

    if user.get("stt_model") not in (None, ""):
        stt = apply(stt, "stt_model", model=str(user["stt_model"]).strip())
    if user.get("tts_engines") not in (None, ""):
        tts = apply(tts, "tts_engines", engines=split_words(user["tts_engines"]))
    for key in ("edge_voice", "windows_voice"):
        if user.get(key) is not None:
            tts = apply(tts, key, **{key: str(user[key]).strip()})
    if isinstance(user.get("barge_in"), bool):
        barge = apply(barge, "barge_in", enabled=user["barge_in"])
    if isinstance(user.get("mic_always_on"), bool):
        resident = apply(resident, "mic_always_on", mic_always_on=user["mic_always_on"])
    if user.get("silence_timeout") not in (None, ""):
        try:
            seconds = float(user["silence_timeout"])
        except (TypeError, ValueError):
            seconds = 0.0
        resident = apply(resident, "silence_timeout", silence_timeout=seconds) if seconds >= 10 else resident
        if seconds < 10:
            warnings.append("silence_timeout")
    if user.get("wake_words") not in (None, ""):
        resident = apply(resident, "wake_words",
                         wake_words=tuple(dict.fromkeys((*resident.wake_words, *split_words(user["wake_words"])))))
    stt = replace(stt, language=language or stt.language)
    if not tts.edge_voice:
        voices = tts.default_voices or {}
        tts = replace(tts, edge_voice=str(voices.get(language) or voices.get("en") or "en-US-AriaNeural"))
    return VoiceSettings(stt, tts, base.turn, barge, resident, tuple(warnings))


def load_settings(defaults: Path, user: dict | None = None, language: str = "ko") -> VoiceSettings:
    """defaults.toml(없으면 코드의 기본값) 위에 도구 설정(user)을 얹는다. defaults.toml 이 틀리면 ValueError."""
    data = tomllib.loads(Path(defaults).read_text(encoding="utf-8-sig")) if Path(defaults).is_file() else {}
    base = VoiceSettings(_make(STTConfig, data.get("stt")), _make(TTSConfig, data.get("tts")),
                         _make(TurnConfig, data.get("turn")), _make(BargeInConfig, data.get("barge_in")),
                         _make(ResidentConfig, data.get("resident")))
    return _overlay(base, dict(user or {}), language)


def resolve_stt(stt: STTConfig, cuda_devices: int) -> tuple[str, str, str]:
    """(모델, 장치, 계산 형식). auto 는 GPU 가 있으면 gpu_model, 없으면 cpu_model. 고른 모델은 GPU 가 있으면 GPU 로."""
    model = stt.model
    if model == "auto":
        model = stt.gpu_model if cuda_devices > 0 else stt.cpu_model
    if cuda_devices > 0:
        return model, "cuda", stt.gpu_compute_type
    return model, "cpu", stt.cpu_compute_type
