"""음성 인식(STT). faster-whisper 로 이 PC 안에서 처리한다.

모델은 처음 쓸 때 Hugging Face 에서 받아 사용자의 기본 캐시(~/.cache/huggingface/hub)에 둔다. 이미 받은 것이 있으면
서버에 묻지 않고 바로 읽는다(local_files_only 를 먼저 시도: 부르는 쪽이 정한다).
"""

from __future__ import annotations

import os
import re
import site
import threading
from pathlib import Path

import numpy as np

from voicelib.settings import STTConfig


def add_gpu_libraries() -> None:
    """pip 로 설치한 NVIDIA 실행 라이브러리(nvidia-cublas-cu12 등의 DLL) 위치를 알려 준다. Windows 는 이 폴더를 저절로 찾지 못한다."""
    for packages in site.getsitepackages():
        for bin_dir in Path(packages).glob("nvidia/*/bin"):
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(bin_dir))
            if str(bin_dir) not in os.environ.get("PATH", ""):
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def cuda_device_count() -> int:
    """쓸 수 있는 NVIDIA GPU 수(드라이버 기준). ctranslate2 가 없거나 읽지 못하면 0."""
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def cuda_libraries_ready() -> bool:
    """GPU 받아쓰기에 필요한 CUDA 12 라이브러리(cuBLAS·cuDNN 9)를 불러올 수 있는지. Windows 만 미리 본다(그 밖은 True:
    올리다 실패하면 부르는 쪽이 CPU 로 바꾼다)."""
    if os.name != "nt":
        return True
    import ctypes
    add_gpu_libraries()
    for name in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(name)
        except OSError:
            return False
    return True


def build_prompt(words) -> str:
    """음성 인식 모델에 미리 알려 줄 말: 캐릭터 이름·프로그램 이름·사용자가 덧붙인 낱말의 목록.

    Whisper 계열은 앞선 글을 단서로 비슷한 말을 더 잘 받아 적는다. 다만 잡음이나 혼잣말에서 이 글을
    되풀이하는 일이 있어(환각) 명령 문장이 아니라 낱말 목록으로 만든다(원본 2026-10-14 측정).
    """
    return ", ".join(dict.fromkeys(str(w).strip() for w in words if str(w).strip()))


# 받아 적은 글이 프롬프트의 이 비율 이상을 그대로 되풀이하면 지어낸 것으로 본다
ECHO_COVERAGE = 0.4
# 언어별로 거는 인식 힌트. 중국어는 힌트가 없으면 번체로 적는 일이 있어 간체 문장을 준다(외국어 회화용으로 남겨 둔다)
LANGUAGE_PROMPTS = {"zh": "以下是普通话的句子。"}


def clean_text(text: str, prompt: str = "") -> str:
    """받아 적은 글에서 깨진 글자를 걷어 내고, 미리 알려 준 말을 길게 그대로 되풀이한 것이면 버린다."""
    text = text.replace("�", "").strip()
    heard, known = _normalize(text), _normalize(prompt)
    if heard and known and heard in known and len(heard) >= ECHO_COVERAGE * len(known):
        return ""  # 소리가 없거나 잡음인데 프롬프트를 되뇐 것
    return text


# 인식기가 스스로 확신하지 못한 구간(잡음·TV·먼 소리에서 지어낸 글)을 버리는 기준. 값이 없는 구간(시험용 가짜 포함)은 버리지 않는다
LOW_LOGPROB = -1.0       # 평균 로그확률이 이보다 낮고 다른 의심 신호가 함께 있을 때
VERY_LOW_LOGPROB = -1.5  # 이보다 낮으면 단독으로도 버린다
NO_SPEECH_HINT = 0.3
MAX_COMPRESSION = 2.4    # 같은 말이 반복되는 환각의 압축비


def is_doubtful_segment(segment) -> bool:
    """인식기가 말이 아닐 수 있다고 본 구간인지(환각 걸러내기)."""
    logprob = getattr(segment, "avg_logprob", None)
    if not isinstance(logprob, (int, float)):
        return False
    if logprob < VERY_LOW_LOGPROB:
        return True
    no_speech = getattr(segment, "no_speech_prob", 0.0)
    compression = getattr(segment, "compression_ratio", 0.0)
    no_speech = no_speech if isinstance(no_speech, (int, float)) else 0.0
    compression = compression if isinstance(compression, (int, float)) else 0.0
    return logprob < LOW_LOGPROB and (no_speech > NO_SPEECH_HINT or compression > MAX_COMPRESSION)


# 한국어로 고정한 인식에서 다른 나라 글자(가나, 키릴, 태국, 아랍 문자)가 나오면 짧고 흐린 소리에서 지어낸 글이다
# (원본 2026-10-26 "그래 고마워"가 "에,こんばんは"로 적힌 일). 한국어가 아니면 검사하지 않는다.
_FOREIGN_SCRIPT = re.compile(r"[぀-ヿЀ-ӿ฀-๿؀-ۿ]")


def has_foreign_script(text: str, language: str) -> bool:
    return language == "ko" and _FOREIGN_SCRIPT.search(text) is not None


def _normalize(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def decode_recording(path: Path) -> np.ndarray:
    """녹음 파일을 PyAV 로 16kHz mono float32 로 바꾼다(출력 장치는 열지 않는다). 회의는 약 1분 조각 파일이라 통째로 읽어도 된다.
    AI Jarvis 커밋 2ed201a 의 tools/음성/voicelib/listen/stt.py 에서 옮김."""
    import av

    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    chunks = []
    with av.open(str(path), mode="r") as container:
        for frame in container.decode(audio=0):
            chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
        chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    return np.concatenate(chunks).astype("float32", copy=False) if chunks else np.zeros(0, dtype="float32")


class Transcriber:
    def __init__(self, config: STTConfig, model: str, device: str, compute_type: str, prompt: str = "", *,
                 local_files_only: bool = False) -> None:
        if device == "cuda":
            add_gpu_libraries()
        from faster_whisper import WhisperModel

        self._config = config
        self.model_name, self.device = model, device
        self._inference_lock = threading.Lock()
        self._final_waiting = threading.Event()
        self._final_state_lock = threading.Lock()
        self._final_count = 0
        self._default_prompt = prompt or None
        self._prompt = self._default_prompt
        self._base_language = config.language or None
        self._language = self._base_language
        self._model = WhisperModel(model, device=device, compute_type=compute_type, local_files_only=local_files_only)

    @property
    def language(self) -> str | None:
        return self._language

    def set_prompt(self, prompt: str) -> None:
        """미리 알려 줄 낱말을 바꾼다(캐릭터 이름이 늦게 오거나 바뀔 때). 기본 언어일 때만 쓴다."""
        self._default_prompt = prompt or None
        if self._language == self._base_language:
            self._prompt = self._default_prompt

    def set_language(self, language: str | None) -> None:
        """음성 인식 언어를 바꾼다(None 이면 기본으로). 기본 언어가 아니면 낱말 목록은 쓰지 않는다(외국어 회화용)."""
        language = language or self._base_language
        prompt = self._default_prompt if language == self._base_language else LANGUAGE_PROMPTS.get(language or "")
        self._language, self._prompt = language, prompt

    def warmup(self) -> None:
        """무음을 VAD 로 버리지 않고 실제 인코더·디코더를 한 번 실행한다(GPU 라이브러리가 없으면 여기서 오류가 난다)."""
        segments, _ = self._model.transcribe(
            np.zeros(16000, dtype="float32"), language=self._language,
            vad_filter=False, beam_size=1, best_of=1, temperature=0.0,
            max_new_tokens=1, condition_on_previous_text=False,
        )
        for _ in segments:   # faster-whisper 는 지연 실행하므로 반복자를 소비해야 실제로 계산한다
            pass

    def transcribe(self, audio: np.ndarray) -> str:
        """16kHz 단일 채널 소리를 글로 받아 적는다(최종). 미리보기보다 앞선다."""
        with self._final_state_lock:
            self._final_count += 1
            self._final_waiting.set()
        try:
            with self._inference_lock:
                return self._transcribe(audio)
        finally:
            with self._final_state_lock:
                self._final_count -= 1
                if not self._final_count:
                    self._final_waiting.clear()

    def transcribe_file(self, path: Path) -> list[dict]:
        """회의 녹음 조각 파일 → [{"start", "end", "text"}](파일 안의 초). 화면 언어로, 낱말 목록 없이 받아쓴다."""
        audio = decode_recording(path)
        with self._inference_lock:
            segments, _ = self._model.transcribe(audio, language=self._base_language, beam_size=self._config.beam_size,
                                                 vad_filter=True, condition_on_previous_text=False)
            return [{"start": float(segment.start), "end": float(segment.end), "text": clean_text(segment.text)}
                    for segment in segments]

    def transcribe_preview(self, audio: np.ndarray) -> str:
        """미리보기는 단일 후보로 읽고, 최종 인식이 기다리거나 도는 중이면 기다리지 않고 빈 글."""
        if self._final_waiting.is_set() or not self._inference_lock.acquire(blocking=False):
            return ""
        try:
            if self._final_waiting.is_set():
                return ""
            language, prompt = self._language, self._prompt
            segments, _ = self._model.transcribe(
                audio, language=language, initial_prompt=prompt, beam_size=1, best_of=1,
                temperature=0.0, vad_filter=True, condition_on_previous_text=False,
            )
            return clean_text(" ".join(segment.text.strip() for segment in segments
                                       if not is_doubtful_segment(segment)
                                       and not has_foreign_script(segment.text, language)), prompt or "")
        finally:
            self._inference_lock.release()

    def _transcribe(self, audio: np.ndarray) -> str:
        language, prompt = self._language, self._prompt  # 바뀌는 중이어도 한 쌍으로 쓴다
        segments, _ = self._model.transcribe(
            audio, language=language, beam_size=self._config.beam_size, vad_filter=True,
            condition_on_previous_text=False,   # 앞 문장에 이끌려 없는 말을 지어내는 것을 줄인다
            initial_prompt=prompt,
        )
        parts = [segment.text.strip() for segment in segments
                 if not is_doubtful_segment(segment) and not has_foreign_script(segment.text, language)]
        return clean_text(" ".join(parts), prompt or "")
