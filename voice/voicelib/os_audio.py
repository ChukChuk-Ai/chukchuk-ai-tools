"""OS 별 소리 코드 한 곳: 기본 마이크·스피커의 식별자와 이름, OS 내장 음성(글 → 소리).

지금은 Windows 판이다(pycaw·comtypes 로 기본 장치, SAPI 로 내장 음성). macOS 판은 이 파일만 새로 짠다.
다른 OS 이거나 패키지가 없으면 기능을 끈 채로 돌아간다: 장치 식별자는 (None, None)(장치가 바뀐 것을 알아채지 못할 뿐
마이크·스피커는 OS 기본 장치로 그대로 쓴다), 내장 음성은 BuiltinVoiceError 를 내 다음 엔진(없으면 말하기 실패 알림)으로 넘긴다.
소리를 녹음·재생하지 않는다(녹음·재생은 sounddevice 가 OS 마다 같은 코드로 한다).
"""
from __future__ import annotations

import queue
import sys
import threading

import numpy as np

IS_WINDOWS = sys.platform == "win32"
BUILTIN_SAMPLE_RATE = 24000
BUILTIN_TIMEOUT_SECONDS = 15
# 화면 언어 → Windows 음성의 Language 값(16진 LCID). 목소리 이름을 비웠을 때 이 언어의 첫 목소리를 고른다
_LANGUAGE_LCIDS = {"ko": ("412",), "en": ("409", "809", "c09", "1009", "4009"), "ja": ("411",), "zh": ("804", "404")}

DeviceIds = tuple[str | None, str | None]                                  # (기본 마이크, 기본 스피커). 없으면 None
DeviceInfos = tuple[tuple[str, str] | None, tuple[str, str] | None]        # (식별자, 사람이 읽는 이름) 쌍


class BuiltinVoiceError(Exception):
    pass


# ---- 기본 장치 ----

def _audio_utilities():
    """(comtypes, AudioUtilities) 또는 None(Windows 가 아니거나 패키지가 없음)."""
    if not IS_WINDOWS:
        return None
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities
    except ImportError:
        return None
    return comtypes, AudioUtilities


def _id_of(device) -> str | None:
    for attr in ("id", "GetId"):
        found = getattr(device, attr, None)
        if found is None:
            continue
        try:
            return found() if callable(found) else str(found)
        except Exception:
            return None
    return None


def _name_of(device, utilities) -> str:
    try:
        if not hasattr(device, "FriendlyName"):
            device = utilities.CreateDevice(device)
        return str(device.FriendlyName or "")
    except Exception:
        return ""


def default_device_infos() -> DeviceInfos:
    """기본 마이크·스피커의 (식별자, 이름). 장치가 없거나 읽을 수 없으면 그 자리는 None. 어느 스레드에서 불러도 된다."""
    found = _audio_utilities()
    if found is None:
        return (None, None)
    comtypes, utilities = found
    comtypes.CoInitialize()
    try:
        infos = []
        for getter in (utilities.GetMicrophone, utilities.GetSpeakers):
            try:
                device = getter()
                ident = _id_of(device)
                infos.append((ident, _name_of(device, utilities) or ident) if ident else None)
            except Exception:  # 기본 장치가 없으면 COM 오류가 난다
                infos.append(None)
        return (infos[0], infos[1])
    finally:
        comtypes.CoUninitialize()


def default_device_ids() -> DeviceIds:
    """기본 마이크·스피커의 식별자만(바뀌었는지 가볍게 볼 때)."""
    mic, speaker = default_device_infos()
    return (mic[0] if mic else None, speaker[0] if speaker else None)


# ---- 내장 음성 ----

class BuiltinVoice:
    """OS 내장 음성(Windows SAPI). 인터넷 없이 동작하고 글이 밖으로 나가지 않는다.

    COM 객체는 만든 스레드에서만 쓸 수 있으므로 전용 스레드 하나가 합성을 맡는다. 소리는 스피커가 아니라 메모리로 받는다.
    name 이 비었으면 language 의 첫 목소리, 그것도 없으면 OS 기본 목소리를 쓴다.
    """

    def __init__(self, name: str = "", language: str = "") -> None:
        self._name = name.strip()
        self._language = language
        self._requests: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self.voice_name = ""   # 실제로 고른 목소리(처음 말할 때 정해진다)

    @staticmethod
    def available() -> bool:
        if not IS_WINDOWS:
            return False
        try:
            import comtypes.client  # noqa: F401
        except ImportError:
            return False
        return True

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if not self.available():
            raise BuiltinVoiceError("builtin voice unavailable on this system")
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, daemon=True, name="builtin_voice")
                self._worker.start()
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put((text, reply))
        try:
            result = reply.get(timeout=BUILTIN_TIMEOUT_SECONDS)
        except queue.Empty:
            raise BuiltinVoiceError("builtin voice timed out") from None
        if isinstance(result, BuiltinVoiceError):
            raise result
        return result

    def _run(self) -> None:
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        voice = None
        while True:
            text, reply = self._requests.get()
            try:
                if voice is None:   # 준비에 실패하면 이번 요청만 실패로 알리고 다음 요청 때 다시 준비한다
                    voice = comtypes.client.CreateObject("SAPI.SpVoice", dynamic=True)
                    token = self._choose(voice)
                    if token is not None:
                        voice.Voice = token
                        self.voice_name = str(token.GetDescription())
                stream = comtypes.client.CreateObject("SAPI.SpMemoryStream", dynamic=True)
                stream.Format.Type = 26  # SAFT24kHz16BitMono
                voice.AudioOutputStream = stream
                voice.Speak(text, 16)    # SVSFIsNotXML: 글 속의 <...> 를 음성 제어 명령으로 해석하지 않는다
                pcm = np.frombuffer(bytes(stream.GetData()), dtype="<i2")
            except Exception as e:
                voice = None
                reply.put(BuiltinVoiceError(f"{type(e).__name__}: {e}"))
                continue
            if len(pcm) == 0:
                reply.put(BuiltinVoiceError("builtin voice returned no audio"))
            else:
                reply.put((pcm.astype("float32") / 32768.0, BUILTIN_SAMPLE_RATE))

    def _choose(self, voice):
        """이름이 맞는 목소리 → 화면 언어의 첫 목소리 → None(OS 기본)."""
        tokens = voice.GetVoices()
        items = [tokens.Item(i) for i in range(tokens.Count)]
        if self._name:
            wanted = self._name.lower()
            for token in items:
                if wanted in (str(token.GetAttribute("Name")).lower(), str(token.GetDescription()).lower()):
                    return token
        lcids = _LANGUAGE_LCIDS.get(self._language, ())
        for token in items:
            try:
                codes = str(token.GetAttribute("Language")).lower().split(";")
            except Exception:
                continue
            if any(code.lstrip("0") in lcids for code in codes):
                return token
        return None
