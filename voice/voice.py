"""음성 프로세스(도구 '음성'). 앱이 켜 있는 동안 따로 돈다(tool.json 의 "process"). 앱과는 표준 입출력 JSON 한 줄씩 주고받는다.

앱 → 음성: {"mic": true|false}  {"say": 글, "voice": "chat|command"}  {"stop": true}
           {"silent": true|false}(음성 안내 계속 끔/다시 켬: 끈 동안은 말할 글이 와도 소리 내지 않는다)
           {"names": ["캐릭터 대화명", ...]}  {"mode": "general|dialogue|language", "lang": "en|ja|zh|es|fr|de"}
           {"ask": 글}(모드 중 대화창에 친 말)  {"meeting": "start|stop|process|cancel"}  {"quit": true}
음성 → 앱: {"state": "loading|off|called|hearing|speaking|meeting|error", "detail": 글}  {"partial": 글}
           {"dictated": 글}(받아쓰기: 일반모드에서 마이크가 켜져 있으면 받아쓴 말을 모두. 보내기·교정은 앱이 본다)
           {"dictate_idle": 분}(그동안 말이 없어 마이크를 껐다)  {"heard": 글}(모드가 끝난 뒤 늦게 온 모드 말)
           {"info": 글, "code": 짧은 영문 코드}  {"devices": {"mic": 이름|null, "speaker": 이름|null}}
           {"dropped": 글, "why": "context"}  {"input_done": true}(시험용)
           대화모드·외국어 회화: {"mode": 이름, "lang": 코드, "label": 화면 이름}  {"user": 사용자가 말한 글}
           {"answer": 대화창에 보일 답, "card": {"title", "body"}(화이트보드로 보낼 자세한 내용, 없으면 빠짐)}
           {"chat_info": {"model", "state"} | null}(대화 모델 상태. 일반모드면 null)
           회의: {"meeting": "recording|stopped|processing|processed|cancelled|error|unfinished|idle", "folder", "detail",
           "card": {"title", "body"}(processed 의 요약 카드)}. 녹음 중에는 대화 듣기·말하기를 쉬고 state 는 "meeting".

대화모드·외국어 회화: 모드가 켜지면 호출어 없이 듣고, 캐릭터(앱의 대화 엔진) 대신 작업 분야 '음성 대화' 모델(modechat.py)이 답한다.
대화모드의 받아쓴 말은 '음성 교정' 모델(correct.py)로 고치고 옆 사람·TV 소리를 거른 뒤, 말이 끊기면 한 번에 보낸다.
외국어 회화는 받아쓰기·목소리를 그 언어로 바꾸고, 답의 새 표현을 노트(data/mydata/language/)에 남긴다.
회의(meeting.py): 녹음 중에는 마이크를 회의 녹음이 쓰고, 정리는 받아쓰기 모델로 뒤에서 한다(작업 분야 '회의 정리').

받아쓰기(일반모드): 마이크가 켜져 있으면 호출어 없이 받아쓴 말을 모두 앱에 보낸다(앱이 입력창이나 앞에 있는 다른 창에 모은다).
도구 설정 '말이 없으면 마이크 끄기'(분) 동안 말이 없으면 마이크를 끈다. 캐릭터 이름은 받아쓰기에 미리 알려 줄 낱말로만 쓴다.

부품은 AI Jarvis 의 음성 도구(원본 AI_Partner 부품 사본)에서 옮긴 voicelib. 이 파일은 그 voice.py 에서 듣기·받아쓰기(커밋 cc00c35 의
일반모드 받아쓰기)와 말하기(끼어들기 멈춤), 대화모드·외국어 회화를 옮긴 것이다. 화면 글은 texts.toml([ko]·[en]), 설정은 defaults.toml + 도구 설정.
앱은 이 도구의 .venv 파이썬으로, 작업 폴더 = 앱 폴더, CHUKCHUK_ROOT 를 주고, 표준 오류를 data/logs/voice.log 로 돌려 띄운다.
시험용: --input 파일.wav(마이크 대신 파일을 흘려 넣음), --no-sound(스피커 대신 기록만), --fast, 환경 변수 VOICE_SETTINGS(JSON, 도구 설정 덮기).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import replace
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("CHUKCHUK_ROOT") or str(HERE.parents[1]))   # sdk 찾기(앱 폴더는 앱이 알려 준다)
sys.path.insert(0, str(HERE))
from sdk import config, data, text  # noqa: E402

import numpy as np  # noqa: E402

from voicelib.audio_devices import DeviceReport, DeviceWatcher, default_device_infos, portaudio_use  # noqa: E402
from voicelib.audio_level import frame_db  # noqa: E402
from voicelib.listen import FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402
from voicelib.listen.barge_in import BargeIn, is_pause_phrase, normalize  # noqa: E402
from voicelib.listen.level_barge import LevelBarge, classify_stop  # noqa: E402
from voicelib.mic import (HEALTHY_SECONDS, NO_FRAMES_SECONDS, RECONNECT_BACKOFF_BASE, RECONNECT_BACKOFF_MAX,  # noqa: E402
                          MicrophoneFrames, MicWatch, drop_reason, safe_refresh_portaudio)
from voicelib.resident import SessionRouter  # noqa: E402
from voicelib.settings import load_settings, resolve_stt  # noqa: E402
from correct import preview_text  # noqa: E402

T = text.load(HERE)
DEFAULTS = HERE / "defaults.toml"
STALE_PARTIAL_SECONDS = 10.0   # 입력창에 받아쓴 글이 이만큼 바뀌지 않으면(말끝을 못 찾았다) 지우고 dropped 로 알린다
MODEL_SIZES = {"large-v3-turbo": "1.6GB", "medium": "1.5GB", "small": "470MB", "base": "140MB", "tiny": "75MB"}
LANGUAGES = ("en", "ja", "zh", "es", "fr", "de")   # 외국어 회화 언어(받아쓰기 언어 코드)
CONFIRM_SECONDS = 0.6   # 대화모드·회화: 원고를 보인 뒤 새 말이 없으면 이만큼 기다렸다 보낸다(초)
MODE_ENDS = ("대화모드 끝", "대화 모드 끝", "대화모드 종료", "회화 끝", "회화 종료", "일반모드", "일반 모드", "일반모드로 전환",
             "일반모드로 바꿔줘", "end conversation", "finish conversation", "go back to korean", "back to normal mode",
             "normal mode", "end dialogue mode")   # 모드 중 이 말(또는 도구 설정의 대화 끝 말)이면 일반모드로


def log(message: str) -> None:
    """진단 기록(표준 오류 → 앱이 data/logs/voice.log 로 돌린다)."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", file=sys.stderr, flush=True)


class Out:
    """표준 출력에 JSON 한 줄씩(여러 스레드에서 부른다)."""

    def __init__(self, stream=None):
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()

    def __call__(self, **event):
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            try:
                self._stream.write(line + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass


def user_settings() -> dict:
    """도구 설정(환경설정 > 도구 설정 > 음성). 시험은 VOICE_SETTINGS(JSON)로 덮는다."""
    values = config.tool_settings(HERE)
    try:
        values.update(json.loads(os.environ.get("VOICE_SETTINGS") or "{}"))
    except ValueError:
        pass
    return values


def app_words() -> list[str]:
    """받아쓰기에 미리 알려 줄 프로그램 이름·별칭(config/apps.toml, sdk.apps)."""
    try:
        from sdk import apps
        items = apps.load_config().get("apps", [])
    except Exception:
        return []
    words = []
    for app in items if isinstance(items, list) else []:
        for word in (app.get("name", ""), *app.get("aliases", [])):
            if isinstance(word, str) and word.strip() and word not in words:
                words.append(word.strip())
    return words




def speech_text(markdown: str, limit: int = 600) -> str:
    """답(마크다운)을 소리로 읽을 글로: `---` 줄 아래는 읽지 않고, 표·코드·그림·링크 주소·기호를 빼고,
    ▶ 선택지는 '선택지: …'로 묶는다(취소는 읽지 않는다). 평범한 글은 그대로다."""
    lines, choices = [], []
    fenced = False
    for raw in str(markdown).splitlines():
        line = raw.strip()
        if line.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line):
            break
        if not line or line.startswith("|"):
            continue
        if line.startswith("▶"):
            choice = line.lstrip("▶ ").strip()
            if normalize(choice) not in {normalize(T("cancel")), "취소", "cancel"}:
                choices.append(choice)
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"^#+\s*|^[-*]\s+|^\d+\.\s+|^>\s*", "", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        if line.strip():
            lines.append(line.strip())
    said = " ".join(lines)
    if choices:
        said += " " + T("choices", items=", ".join(choices))
    return said[:limit]




class FileFrames:
    """시험용 마이크 대신: wav 파일을 16kHz 프레임으로 흘려 넣고, 끝나면 조용한 방 소리를 이어 준다."""

    def __init__(self, paths: list[Path], realtime: bool = True, on_done=None):
        self._on_done = on_done
        chunks = []
        for path in paths:
            with wave.open(str(path), "rb") as w:
                rate, raw = w.getframerate(), w.readframes(w.getnframes())
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
            if rate != SAMPLE_RATE:
                idx = np.arange(0, len(audio), rate / SAMPLE_RATE).astype(int)
                audio = audio[idx[idx < len(audio)]]
            chunks.append(np.concatenate([audio, np.zeros(SAMPLE_RATE * 3, dtype=np.float32)]))
        self._noise = np.random.default_rng(0).normal(0, 0.002, FRAME_SAMPLES).astype(np.float32)
        self._audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        self._pos, self._realtime = 0, realtime
        self.failed = threading.Event()
        self.reason = ""
        self.done = threading.Event()

    def get(self, timeout):
        if self._realtime:
            time.sleep(FRAME_SAMPLES / SAMPLE_RATE)
        frame = self._audio[self._pos:self._pos + FRAME_SAMPLES]
        self._pos += FRAME_SAMPLES
        if len(frame) < FRAME_SAMPLES:
            if not self.done.is_set() and self._on_done is not None:
                self._on_done()
            self.done.set()
            frame = np.concatenate([frame, self._noise[:FRAME_SAMPLES - len(frame)]])
        return frame


class Voice:
    def __init__(self, out: Out, *, input_files: list[Path] | None = None, no_sound: bool = False, realtime: bool = True):
        self.out = out
        self.input_files, self.no_sound, self.realtime = input_files, no_sound, realtime
        settings = user_settings()
        self.correction = settings.get("dialogue_correction", True) is not False   # 대화모드 말 교정(도구 설정)
        try:   # 받아쓰기: 이만큼(초) 말이 없으면 마이크를 끈다. 0 이면 끄지 않는다(도구 설정은 분)
            self.dictate_idle = max(0.0, float(settings.get("dictate_idle", 10) or 0)) * 60
        except (TypeError, ValueError):
            self.dictate_idle = 600.0
        self._dictate_at = time.monotonic()   # 마지막으로 말소리가 있었던 때
        self.language = str(settings.pop("language", "") or config.language())
        self.cfg = load_settings(DEFAULTS, settings, self.language)   # 받아쓰기 언어·기본 목소리 = 화면 언어
        self.models = data.tool_dir(HERE.name) / "models"   # data/tools/voice/models (Smart Turn)
        self.mic_on = False
        self.stopping = threading.Event()
        self._mic_changed = threading.Event()
        self._utterances: queue.Queue = queue.Queue()
        self._speech: queue.Queue = queue.Queue()
        self._speaking = threading.Event()
        self.silent = False   # 음성 안내 계속 끔(앱의 말 멈추기 버튼 셋째 상태)
        self._state = None
        self._last_partial = ""
        self._partial_text, self._partial_at = "", 0.0   # 입력창에 보이는 받아쓴 글과 마지막으로 바뀐 때
        self._stt_running = False
        self._names: tuple[str, ...] = ()
        self._fallback_told = False
        self.level = None        # 소리 크기 끼어들기(load 에서 만든다)
        self.barge = None
        self._handoff = False    # 끼어들어 멈춘 말하기가 끝나는 동안 들어오는 소리를 평소 듣기로 보낸다
        self._barged = False     # 다음에 끝나는 발화가 끼어들기로 시작됐다(받아쓴 뒤 캐릭터 자신의 소리인지 본다)
        self._speak_ring = deque(maxlen=16)   # 말하는 동안 최근 마이크 프레임(큰 소리로 멈추면 이것부터 받아쓴다)
        self._loud_run = 0
        self.played: list[tuple[np.ndarray, int]] = []   # --no-sound 일 때 낸 소리(시험용)
        # 대화모드·외국어 회화
        self.mode, self.lang = "general", ""
        self.chat = None                       # ModeChat(모드 중에만)
        self._mode_lock = threading.RLock()
        self._chat_q: queue.Queue = queue.Queue()   # (대화 상대, 사용자 말, 대화창에 '나' 줄로 보일지)
        self._tts: dict[str, object] = {}      # 언어별 말하기 엔진("" = 화면 언어)
        self._draft_lock = threading.Lock()
        self._draft_wake = threading.Event()
        self._draft_segments: list[str] = []   # 이번 원고에 사용자가 한 말(받아쓴 그대로, 시간순)
        self._draft_text = ""                  # 마지막 교정본(없으면 빈 글)
        self._draft_rev = self._draft_done = 0  # 말이 더해진 차례 / 교정을 마친 차례
        self._draft_done_segments = 0          # 교정본에 들어간 말 수(그 뒤의 말은 미리보기로 붙여 보인다)
        self._dialogue_log: list[dict] = []    # 교정 참고용 최근 대화(사용자 원고·대화 모델이 말한 답)
        self._force_draft = False              # 받아쓴 글이 오래 그대로라 말하는 중이라고 나와도 교정·판단한다
        self._confirm_at, self._confirm_activity = 0.0, 0
        self._activity = 0                     # 말소리가 시작된 횟수(교정 뒤 새 말이 시작됐는지 본다)
        self._quiet_at = 0.0                   # 마지막 말소리가 끝난 때
        # 회의
        self.meeting = None
        self.meeting_root: Path | None = None   # 시험: 회의 폴더 자리(없으면 data/mydata/meeting)
        self.meeting_backend = None             # 시험: 마이크 대신 쓸 가짜 녹음 장치
        self._released = threading.Event()      # 대화 듣기가 마이크를 놓았다(회의 녹음이 열기 전에 기다린다)

    # ---- 준비 ----

    def _transcriber(self, model: str, device: str, compute_type: str, prompt: str):
        """받은 모델이 있으면 바로, 없으면 받으면서 올린다. 실패하면 예외."""
        from voicelib.listen.stt import Transcriber
        where = T("device_gpu") if device == "cuda" else T("device_cpu")
        self.state("loading", T("loading_stt", model=model, device=where))
        try:
            stt = Transcriber(self.cfg.stt, model, device, compute_type, prompt, local_files_only=True)
        except Exception:
            self.state("loading", T("downloading_stt", model=model, size=MODEL_SIZES.get(model, "?")))
            stt = Transcriber(self.cfg.stt, model, device, compute_type, prompt, local_files_only=False)
        stt.warmup()
        return stt

    def _load_stt(self, prompt: str):
        from voicelib.listen.stt import cuda_device_count, cuda_libraries_ready
        stt_cfg = self.cfg.stt
        gpus = cuda_device_count()
        if gpus and not cuda_libraries_ready():   # GPU 는 있지만 CUDA 라이브러리가 없다: 켤 때마다 알리지 않고 CPU 로(tool.json 사용법)
            log("GPU 가 있지만 cuBLAS·cuDNN(CUDA 12)을 찾지 못해 CPU 로 받아쓴다")
            gpus = 0
        model, device, compute_type = resolve_stt(stt_cfg, gpus)
        try:
            self.stt = self._transcriber(model, device, compute_type, prompt)
        except Exception as e:
            if device != "cuda":
                raise
            log(f"GPU 받아쓰기 실패: {type(e).__name__}: {e}")
            model = stt_cfg.cpu_model if stt_cfg.model == "auto" else model
            self.info(T("gpu_failed", model=model), "gpu_failed")
            device, compute_type = "cpu", stt_cfg.cpu_compute_type
            self.stt = self._transcriber(model, device, compute_type, prompt)
        log(f"받아쓰기 모델: {model} ({device}, {compute_type}), 언어 {self.stt.language}")
        # 미리보기 전용 작은 모델(따로 두면 최종 받아쓰기가 도는 동안에도 미리보기가 멈추지 않는다)
        self.preview_stt = self.stt
        preview = stt_cfg.preview_model.strip()
        if preview and preview != model:
            try:
                self.preview_stt = self._transcriber(preview, device, compute_type, prompt)
            except Exception as e:
                log(f"미리보기 모델 실패: {type(e).__name__}: {e}")
                self.info(T("preview_failed", model=preview), "preview_failed")

    def _load_smart_turn(self):
        from voicelib.listen.smart_turn import SmartTurn, is_installed
        folder = self.models / "smart-turn"
        if not is_installed(folder):
            self.state("loading", T("downloading_smart_turn"))
        try:
            return SmartTurn(folder)
        except Exception as e:
            log(f"Smart Turn 실패: {type(e).__name__}: {e}")
            self.info(T("smart_turn_failed"), "smart_turn_failed")
            return None

    def prompt(self) -> str:
        from voicelib.listen.stt import build_prompt
        if not self.cfg.stt.use_prompt:
            return ""
        return build_prompt([*self._names, *self.cfg.resident.wake_words, *app_words(),
                             *self.cfg.stt.prompt_extra.split(",")])

    def load(self):
        from voicelib.listen.dictation import StreamingDictation
        from voicelib.listen.endpoint import Endpointer
        from voicelib.listen.vad import StreamingVAD
        from voicelib.speak.speaker import Speaker
        from voicelib.speak.tts import create_tts

        cfg = self.cfg
        self.state("loading", T("loading"))
        for name in cfg.warnings:
            self.info(T("bad_setting", name=name), "bad_setting")
        self._load_stt(self.prompt())
        smart = self._load_smart_turn() if cfg.turn.smart_turn else None
        self.vad = StreamingVAD()
        self.endpointer = Endpointer(cfg.turn, smart.complete_probability if smart else None)
        kwargs = {"play": self._record_play} if self.no_sound else {}
        self._tts[""] = create_tts(cfg.tts, self.language)
        self.speaker = Speaker(self._tts[""], lead_in=cfg.tts.lead_in,
                               lead_in_noise=cfg.tts.lead_in_noise, lead_in_tone=cfg.tts.lead_in_tone, **kwargs)
        self.router = SessionRouter(cfg.resident, known_targets=tuple(app_words()))
        self.router.mute(True)
        self.barge_vad = StreamingVAD()
        b = cfg.barge_in
        if b.enabled and b.mode in ("phrases", "both"):
            self.barge = BargeIn(b.phrases, self.stt.transcribe, self.speaker.recent_echo_text, self._barge_stop,
                                 on_error=lambda e: log(f"끼어들기 받아쓰기 실패: {e}"), pause_phrases=b.pause_phrases)
        if b.enabled and b.mode in ("level", "both"):   # 목소리가 들리면 무슨 말이든 멈추고 이어 받아쓰기
            self.level = LevelBarge(b.level_margin_db, b.level_hold_seconds, b.echo_gain_db)
        self.dictation = StreamingDictation(self.preview_stt.transcribe_preview, self._on_partial,
                                            minimum_seconds=cfg.turn.partial_min_seconds,
                                            interval_seconds=cfg.turn.partial_interval_seconds)
        from meeting import Meeting
        self.meeting = Meeting(self.meeting_root or data.mydata("meeting"), transcribe=self.stt.transcribe_file,
                               notify=lambda event: self.out(**event), audio_backend=self.meeting_backend,
                               before_capture=self._release_mic, after_capture=self._mic_changed.set)
        self.meeting.recover()   # 지난 실행에서 끝나지 않은 회의만 알린다(이어 하지 않는다)
        threads = [self._listen, self._recognize, self._speak, self._draft_loop, self._chat_loop]
        if self.input_files is None:
            threads.append(self._device_loop)
        for target in threads:
            threading.Thread(target=target, daemon=True, name=target.__name__).start()
        self.state("off", T("state_off"))
        if cfg.resident.mic_always_on:   # 도구 설정 '앱을 켤 때 마이크도 켜기': 켜진 채 받아쓴다
            self.mic_on = True
            self._dictate_at = time.monotonic()
            self.router.mute(False)
            self._mic_changed.set()
            self._idle_state()

    # ---- 알림 ----

    def state(self, state: str, detail: str = ""):
        if (state, detail) != self._state:
            self._state = (state, detail)
            self.out(state=state, detail=detail)

    def info(self, message: str, code: str = ""):
        self.out(info=message, code=code) if code else self.out(info=message)

    def partial(self, shown: str):
        shown = shown or ""
        if shown != self._partial_text:
            self._partial_text, self._partial_at = shown, time.monotonic()
        self.out(partial=shown)

    def _recording(self) -> bool:
        return self.meeting is not None and self.meeting.active

    def _idle_state(self):
        if self._recording():
            self.state("meeting", T("state_meeting"))
        elif not self.mic_on:
            self.state("off", T("state_off"))
        elif self.mode != "general":
            self.state("called", self.mode_label())
        else:
            self.state("called", T("state_dictating"))

    # ---- 앱 명령 ----

    def handle(self, cmd: dict):
        if "mic" in cmd:
            self.set_mic(bool(cmd["mic"]))
        if "say" in cmd:
            self._speech.put((str(cmd["say"]), str(cmd.get("voice") or "chat"), ""))
        if cmd.get("stop"):
            self.stop_speaking()
        if "silent" in cmd:
            self.silent = bool(cmd["silent"])
            if self.silent:
                self.stop_speaking()
        if "names" in cmd:
            names = cmd["names"] if isinstance(cmd["names"], list) else [cmd["names"]]
            self._names = tuple(str(n) for n in names if str(n).strip())   # 받아쓰기에 미리 알려 줄 낱말·대화 상대 이름
            prompt = self.prompt()
            for stt in {id(self.stt): self.stt, id(self.preview_stt): self.preview_stt}.values():
                stt.set_prompt(prompt)
            self._idle_state()
        if "mode" in cmd:
            self.set_mode(str(cmd["mode"] or "general"), str(cmd.get("lang") or ""))
        if "meeting" in cmd:
            self.meeting_command(str(cmd["meeting"]))
        if cmd.get("ask"):   # 모드 중 대화창에 친 글(앱이 이미 '나' 줄로 보였다)
            chat = self.chat
            if chat is not None:
                self._dialogue_log = (self._dialogue_log + [{"user": str(cmd["ask"])}])[-8:]
                self._chat_q.put((chat, str(cmd["ask"]), False))
            else:
                self.info(T("mode_not_on"), "mode_not_on")

    # ---- 대화모드·외국어 회화 ----

    def mode_label(self) -> str:
        if self.mode == "dialogue":
            return T("mode_dialogue")
        if self.mode == "language":
            return T("mode_language", language=T(f"lang_{self.lang}"))
        return T("mode_general")

    def _labels(self) -> dict:
        keys = ("me", "new", "fixed", "user", "past", "this_turn", "now", "notes", "notes_none")
        return {**{k: T(f"label_{k}") for k in keys}, "language": T(f"lang_{self.lang}") if self.lang else ""}

    def set_mode(self, mode: str, lang: str = ""):
        """일반 ↔ 대화모드 ↔ 외국어 회화. 모드가 켜지면 마이크를 켜고 호출어 없이 듣는다. 끝나면 호출 대기로 돌아간다."""
        if mode not in ("general", "dialogue", "language"):
            return
        lang = (lang if lang in LANGUAGES else "en") if mode == "language" else ""
        with self._mode_lock:
            if (mode, lang) == (self.mode, self.lang):
                self.out(mode=mode, lang=lang, label=self.mode_label())   # 이미 그 모드다(앱 표시만 맞춘다)
                return
            was_general = self.mode == "general"
            if mode != "general" and self._recording():
                self.meeting_command("stop")   # 대화모드·회화로 바꾸면 회의 녹음은 멈춘다(파일은 남는다)
            self.mode, self.lang = mode, lang
            self._draft_clear()
            self.chat = None
            if mode != "general":
                from modechat import ModeChat
                name = self._names[0] if self._names else T("default_name")
                self.chat = ModeChat(mode, lang, name=name, ui_language=self.language, labels=self._labels(),
                                     notes_dir=data.mydata("language"))
                if mode == "language":   # 회화 상대가 먼저 인사한다
                    self._chat_q.put((self.chat, T("language_start"), False))
            for stt in {id(self.stt): self.stt, id(self.preview_stt): self.preview_stt}.values():
                stt.set_language(lang or None)
            self._last_partial = ""
            self.partial("")
            self.out(mode=mode, lang=lang, label=self.mode_label())
            self._chat_status()
            if mode != "general":
                self.set_mic(True)
                self.router.skip_wake()
                self._idle_state()
            elif not was_general:
                self.router.end_call()
                self._call_ended(T("mode_ended"), "mode_ended")

    # ---- 회의 ----

    def _release_mic(self):
        """회의 녹음이 마이크를 열기 전에 대화 듣기가 마이크를 놓을 때까지 기다린다."""
        self._released.clear()
        self._mic_changed.set()
        self._released.wait(3)

    def meeting_command(self, action: str):
        m = self.meeting
        if action == "start":
            if m.active:
                self.out(meeting="recording", folder=str(m.folder), detail=T("meeting_already"))
                return
            if self.mode != "general":
                self.set_mode("general")
            self.stop_speaking()
            self._last_partial = ""
            self.partial("")
            ok, message = m.start()
            self.out(**({"meeting": "recording", "folder": str(m.folder)} if ok else {"meeting": "error"}), detail=message)
            self._mic_changed.set()
            self._idle_state()
        elif action == "stop":
            was, message = m.stop()
            self.out(meeting="stopped" if was else "idle", detail=message, **({"folder": str(m.folder)} if was else {}))
            self._mic_changed.set()
            self._idle_state()
        elif action == "process":
            ok, message = m.process()
            done = not ok and m.snapshot().get("status") == "complete"   # 이미 정리한 회의(요약 카드를 다시 올렸다)
            self.out(meeting="processing" if ok else ("idle" if done else "error"), detail=message)
        elif action == "cancel":
            self.info(m.cancel(), "meeting_cancel")
        else:
            self.info(T("meeting_unknown", action=action), "meeting_unknown")

    def _chat_status(self, state: str = ""):
        """대화 모델 상태(앱이 작업자 칸에 보인다). 일반모드면 null."""
        chat = self.chat
        if chat is None:
            self.out(chat_info=None)
            return
        try:
            model = chat.model
        except Exception:
            model = "?"
        self.out(chat_info={"model": model, "state": state or T("chat_waiting"), "turns": len(chat.history) // 2})

    def _tts_for(self, lang: str):
        """언어별 말하기 엔진(edge 목소리는 defaults.toml 의 [tts.default_voices], 내장 음성은 그 언어의 첫 목소리)."""
        if lang not in self._tts:
            from voicelib.speak.tts import create_tts
            tts = self.cfg.tts
            voice = str((tts.default_voices or {}).get(lang) or tts.edge_voice)
            self._tts[lang] = create_tts(replace(tts, edge_voice=voice, windows_voice=""), lang)
        return self._tts[lang]

    def set_mic(self, on: bool):
        if on == self.mic_on:
            return
        self.mic_on = on
        self.router.mute(not on)
        if on:
            self.router.skip_wake()
            self._dictate_at = time.monotonic()
            self.info(T("mic_on"), "mic_on")
        else:
            self._last_partial = ""
            self.partial("")
        self._mic_changed.set()
        self._idle_state()

    def stop_speaking(self):
        while True:
            try:
                self._speech.get_nowait()
            except queue.Empty:
                break
        self.speaker.stop()

    # ---- 말하기 ----

    def _record_play(self, audio, rate, stopped):
        """--no-sound: 스피커 대신 기록만 하고 재생 시간만큼 기다린다(끼어들기 시험이 실제처럼 돌게)."""
        self.played.append((np.array(audio, copy=True), rate))
        end = time.monotonic() + len(audio) / rate / (1 if self.realtime else 20)
        while time.monotonic() < end and not stopped.is_set():
            time.sleep(0.02)

    def _speak(self):
        while not self.stopping.is_set():
            said, _voice, lang = self._speech.get()   # lang: 외국어 회화의 답이면 그 언어 목소리로
            said = speech_text(said)
            if not said or self._recording() or self.silent:   # 회의 녹음 중(녹음에 섞이지 않게)·음성 안내를 끈 동안은 말하지 않는다
                continue
            try:
                self.speaker.tts = self._tts_for(lang)
                self._speaking.set()
                self.state("speaking", said[:40])
                self.speaker.say(said)
                used = getattr(self.speaker.tts, "last_used", "")
                engines = [e.name for e in getattr(self.speaker.tts, "_engines", [])]
                if used and engines and used != engines[0] and not self._fallback_told:
                    self._fallback_told = True
                    self.info(T("tts_fallback", engine=T(f"engine_{used}")), "tts_fallback")
            except Exception as e:
                log(f"말하기 실패: {type(e).__name__}: {e}")
                self.info(T("speak_failed", error=f"{type(e).__name__}: {e}"[:160]), "speak_failed")
            finally:
                if self._speech.empty():
                    self._speaking.clear()
                    self.router.touch()
                    self._idle_state()

    def _barge_stop(self, phrase, heard):
        """멈춤 말을 들었다(끼어들기 조각 받아쓰기). 일상 말에도 나오는 짧은 말은 말하는 중일 때만."""
        if is_pause_phrase(phrase, self.cfg.barge_in.pause_phrases) and not self.speaker.active:
            return
        if not self.router.is_stop(heard) and normalize(heard) != normalize(phrase):
            return
        self.info(T("barge_stopped", heard=heard.strip()), "barge_stopped")
        self.stop_speaking()

    # ---- 장치 감시 ----

    def _device_loop(self):
        """기본 마이크·스피커가 바뀌거나 끊기면 앱에 알린다(3초마다, 이 프로세스 안에서만)."""
        report = DeviceReport(default_device_infos, self.out, lambda m: self.info(m, "device"), T, self._device_changed)
        while not self.stopping.is_set():
            report.step()
            self.stopping.wait(3.0)

    def _device_changed(self):
        """마이크가 꺼져 있을 때만 장치 목록을 새로 읽는다(켜져 있으면 듣기 루프가 다시 연다)."""
        if not self.mic_on:
            safe_refresh_portaudio()

    # ---- 듣기 ----

    def _listen(self):
        """마이크를 열고 듣는다. 장치가 바뀌거나 끊기면 닫고 다시 연다. 꺼져 있으면 기다린다."""
        failures = 0
        while not self.stopping.is_set():
            if not self.mic_on or self._recording():   # 회의 녹음 중에는 마이크를 녹음에 넘긴다
                self._released.set()
                self._mic_changed.wait(0.5)
                self._mic_changed.clear()
                continue
            reason, opened_at = "", None
            try:
                if self.input_files is not None:
                    frames = FileFrames(self.input_files, self.realtime, on_done=lambda: self.out(input_done=True))
                    opened_at = time.monotonic()
                    reason = self._pump(frames, None)
                    self.input_files = []
                else:
                    import sounddevice as sd

                    frames = MicrophoneFrames()
                    with portaudio_use(), sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                                         blocksize=FRAME_SAMPLES, callback=frames.on_audio) as stream:
                        opened_at = time.monotonic()
                        reason = self._pump(frames, stream)
            except Exception as e:
                log(f"마이크 오류: {type(e).__name__}: {e}")
                reason = T("mic_open_failed", error=f"{type(e).__name__}: {e}"[:120])
            if self.stopping.is_set():
                break
            if not reason:
                continue   # 마이크를 껐다
            failures = 0 if opened_at is not None and time.monotonic() - opened_at >= HEALTHY_SECONDS else failures + 1
            self.state("error", reason[:160])
            self.stopping.wait(min(RECONNECT_BACKOFF_MAX, RECONNECT_BACKOFF_BASE * 2 ** max(0, failures - 1)))
            safe_refresh_portaudio()

    def _pump(self, frames, stream) -> str:
        """말하는 동안은 끼어들기만 보고, 그 밖에는 말 끝을 찾아 받아쓰기로 넘긴다. 끝낸 이유(화면 글) 또는 빈 글(마이크 끔)."""
        watcher = DeviceWatcher() if stream is not None else None
        watch = MicWatch(lambda code, peak: self.info(T(code, peak=f"{peak:.4f}"), code))
        resume = self.cfg.turn.resume_after
        listen_after, was_busy, was_hearing = time.monotonic() + resume, False, False
        last_frame = last_check = time.monotonic()
        self.endpointer.reset()
        self.vad.reset()
        self._idle_state()
        while not self.stopping.is_set():
            if not self.mic_on or self._recording():
                return ""
            if frames.failed.is_set():
                return T(frames.reason or "mic_overflow")
            if stream is not None:
                if watcher is not None and watcher.changed():
                    return T("device_switched")
                if not stream.active:
                    return T("mic_disconnected")
            try:
                frame = frames.get(timeout=0.2)
            except queue.Empty:
                if time.monotonic() - last_frame > NO_FRAMES_SECONDS:
                    return T("mic_no_frames")
                continue
            last_frame = time.monotonic()
            if last_frame - last_check >= 1.0:   # 1초마다: 호출이 무응답으로 끝났는지(사용자 침묵만), 받아쓴 글이 오래 그대로인지
                last_check = last_frame
                if (self.mode == "general" and self.dictate_idle and not self._speaking.is_set()
                        and last_frame - self._dictate_at >= self.dictate_idle):   # 받아쓰기: 오래 말이 없으면 마이크를 끈다
                    self.out(dictate_idle=round(self.dictate_idle / 60))
                    self.set_mic(False)
                    return ""
                if self._partial_text and last_frame - self._partial_at >= STALE_PARTIAL_SECONDS:
                    self._stale_partial()
                    self.endpointer.reset()
                    self.vad.reset()
                    self.dictation.reset()
                    was_hearing = False
            if self._handoff and not (self._speaking.is_set() or self.speaker.active):
                self._handoff = False   # 끼어들어 멈춘 말하기가 다 끝났다
            if (self._speaking.is_set() or self.speaker.active) and not self._handoff:
                self._dictate_at = time.monotonic()
                if self.cfg.barge_in.enabled and not self.speaker.stopping and len(frame) == FRAME_SAMPLES:
                    probability = self.barge_vad.probability(frame)
                    if self.barge is not None:
                        self.barge.feed(frame, probability)
                    ref_db = self.speaker.reference.recent_max_db(self.cfg.barge_in.level_window_seconds)
                    self._speak_ring.append(frame)
                    taken, why = None, ""   # 멈추면 받아쓰기로 넘길 프레임
                    if self.level is not None:
                        fire = self.level.feed(frame, probability, ref_db)
                        if fire is not None:
                            taken, why = fire.frames, "voice"
                    if taken is None and self._loud(frame):
                        taken, why = list(self._speak_ring), "loud"
                    if taken is not None:
                        self._level_stop(taken, why)   # 멈추고, 말 시작 조금 앞부터의 소리를 평소 듣기로 넘겼다
                        was_busy, was_hearing = False, self.endpointer.speaking
                        listen_after = 0.0
                        continue
                was_busy = True
                continue
            if was_busy:
                was_busy = False
                self._speak_ring.clear()
                self._loud_run = 0
                if self.barge is not None:
                    self.barge.reset()
                if self.level is not None:
                    self.level.reset()
                self.barge_vad.reset()
                listen_after = time.monotonic() + resume   # 스피커 소리의 꼬리가 들어오지 않도록 잠시 뒤부터 듣는다
                self.endpointer.reset()
                self.vad.reset()
            if time.monotonic() < listen_after or len(frame) != FRAME_SAMPLES:
                continue
            watch.frame(frame)
            utterance = self.endpointer.feed(frame, self.vad.probability(frame))
            if self.endpointer.speaking:
                self._dictate_at = time.monotonic()
                samples = self.endpointer.sample_count
                if self.dictation.wants(self.endpointer.epoch, samples):
                    audio = self.endpointer.snapshot()
                    if audio is not None:
                        self.dictation.submit(self.endpointer.epoch, audio)
                if not was_hearing:
                    self._activity += 1
                    self.state("hearing", T("state_hearing"))
            if was_hearing and not self.endpointer.speaking:
                self._quiet_at = time.monotonic()
            was_hearing = self.endpointer.speaking
            if utterance is not None:
                self.dictation.reset()
                barged, self._barged = self._barged, False
                self._utterances.put((time.monotonic(), utterance, barged))
        return ""

    def _loud(self, frame) -> bool:
        """큰 소리로 멈추기: 마이크 크기가 loud_db 이상으로 loud_hold_seconds 이어졌는지."""
        b = self.cfg.barge_in
        if b.loud_db >= 0:
            return False
        self._loud_run = self._loud_run + 1 if float(frame_db(frame)[0]) >= b.loud_db else 0
        if self._loud_run * FRAME_SAMPLES / SAMPLE_RATE >= b.loud_hold_seconds:
            self._loud_run = 0
            return True
        return False

    def _level_stop(self, frames, why: str):
        """말을 멈추고, 끼어든 말의 첫머리부터 평소 듣기(말끝 찾기·받아쓰기)로 넘긴다."""
        self.info(T(f"level_stopped_{why}"), f"level_stopped_{why}")
        self.stop_speaking()
        if self.barge is not None:
            self.barge.reset()
        if self.level is not None:
            self.level.reset()
        self._speak_ring.clear()
        self._loud_run = 0
        self._handoff, self._barged = True, True
        self.endpointer.reset()
        self.vad.reset()
        for frame in frames:
            self.endpointer.feed(frame, self.vad.probability(frame))
        self.state("hearing", T("state_hearing"))

    def _on_partial(self, epoch, said, audio, activity):
        said = (said or "").strip()
        if len(said) > 25 * len(audio) / SAMPLE_RATE + 20:
            return   # 받아쓰기 모델이 같은 말을 되풀이해 지어낸 미리보기(말한 시간에 비해 너무 긴 글)는 보이지 않는다
        if said and said != self._last_partial:
            self._last_partial = said
            with self._draft_lock:
                shown = preview_text(self._draft_shown(), said) if self._draft_segments else said
            self.partial(shown)

    def _stale_partial(self):
        """입력창 흐린 글이 STALE_PARTIAL_SECONDS 동안 그대로다(잡음으로 말하는 중에 머물렀을 수 있다): 지우고 알린다.
        대화모드·회화는 버리지 않고 원고로 삼아 교정·판단한 뒤 기다리지 않고 보낸다."""
        self._last_partial = ""
        if self.mode != "general" and (self.mode == "language" or self.correction):
            with self._draft_lock:
                if not self._draft_segments and self._partial_text.strip():
                    self._draft_segments.append(self._partial_text.strip())   # 미리보기만 남았다: 이것을 원고로
                    self._draft_rev += 1
            self._force_draft = True
            self._partial_at = time.monotonic()
            self._draft_wake.set()
            return
        pending = self._partial_text.strip()
        if pending and self.mode == "general":   # 받아쓰기: 말끝을 못 찾은 글도 버리지 않고 보낸다
            self.out(dictated=pending)
        self.partial("")

    def _call_ended(self, why: str, code: str):
        self.info(why, code)
        self.partial("")
        if not self.cfg.resident.mic_always_on:
            self.set_mic(False)   # 마이크를 늘 켜 두지 않으면 대화가 끝나면 다시 끈다
        self._idle_state()

    def _recognize(self):
        """받아쓰기 → 호출어 판단 → 앱으로. 호출 대기 중 짧은·오래된·밀린 소리는 받아 적지 않는다."""
        while not self.stopping.is_set():
            submitted, utterance, barged = self._utterances.get()
            self._stt_running = True
            try:
                self._recognize_one(submitted, utterance, barged)
            except Exception as e:
                log(f"받아쓰기 처리 오류: {type(e).__name__}: {e}")
            finally:
                self._stt_running = False

    def _recognize_one(self, submitted, utterance, barged=False):
        r = self.cfg.resident
        general = self.mode == "general"
        reason = drop_reason(utterance, submitted, self._utterances.qsize(), awaiting_wake=False,
                             max_pending=r.max_pending, stale_seconds=r.stale_seconds, min_speech=r.min_speech)
        if reason or not self.mic_on:
            self._partial_restore()   # 버린 말의 미리보기가 입력창에 남지 않게(모드 중이면 지금 원고로 되돌린다)
            return
        try:
            said = self.stt.transcribe(utterance.audio).strip()
        except Exception as e:
            log(f"받아쓰기 실패: {type(e).__name__}: {e}")
            self.info(T("stt_failed", error=type(e).__name__), "stt_failed")
            return
        self._last_partial = ""
        loud = float(frame_db(utterance.audio).max()) if len(utterance.audio) >= FRAME_SAMPLES else -100.0
        log(f"받아씀 {len(utterance.audio) / SAMPLE_RATE:.1f}초 최대 {loud:.0f}dBFS: {said}")
        if barged and classify_stop(said, self.speaker.recent_echo_text()) != "real":
            self.info(T("barge_echo", text=said[:40]), "barge_echo")   # 스피커 소리가 너무 크면 생긴다
            self.partial("")
            return
        if barged and self.router.is_stop(said):
            self.partial("")   # "그만"·"멈춰"로 끼어들었다: 멈춘 것으로 끝(요청으로 보내지 않는다)
            return
        if not said:
            self._partial_restore()
            return
        if not general:
            self._mode_heard(said)
            return
        self._dictate_at = time.monotonic()
        self.partial("")
        self.out(dictated=said)   # 받아쓰기: 보내기("지금 전송해줘")·교정("지금 교정해줘")은 앱이 본다
        self._idle_state()

    # ---- 대화모드·회화: 원고 모으기·교정 ----

    def _partial_restore(self):
        """입력창 흐린 글을 지금 원고(없으면 빈 글)로 되돌린다."""
        with self._draft_lock:
            shown = self._draft_shown()
        self._last_partial = ""
        self.partial(shown)

    def _mode_heard(self, said: str):
        """모드 중 받아쓴 말: 끝말이면 일반모드로, 아니면 원고에 더한다(말이 끊기면 한 번에 대화 모델에게)."""
        ends = {normalize(p) for p in (*self.cfg.resident.end_phrases, *MODE_ENDS)}
        heard = normalize(said)
        if heard in ends or any(len(p) >= 6 and p in heard for p in ends):   # 긴 끝말은 문장 속에 있어도("Okay, go back to Korean")
            self._draft_send()   # 모으던 앞말은 버리지 않고 먼저 보낸다
            self.set_mode("general")
            return
        if self.mode == "language" or self.correction:
            self._draft_add(said)
            return
        self.partial("")
        self._mode_say(said)

    def _draft_shown(self) -> str:
        """지금 입력창에 보일 원고: 교정본 + 아직 교정을 거치지 않은 말('X 말고 Y'만 먼저 적용). _draft_lock 안에서 부른다."""
        text = self._draft_text
        for segment in self._draft_segments[self._draft_done_segments:]:
            text = preview_text(text, segment)
        return text

    def _draft_add(self, said: str):
        with self._draft_lock:
            self._draft_segments.append(said)
            self._draft_rev += 1
            shown = self._draft_shown()
        self.partial(shown)
        self._draft_wake.set()

    def _draft_reset(self) -> str:
        """원고를 비우고, 비우기 전에 보이던 글을 돌려준다. _draft_lock 안에서 부른다."""
        shown = self._draft_shown().strip()
        self._draft_segments, self._draft_text = [], ""
        self._draft_rev = self._draft_done = self._draft_done_segments = 0
        return shown

    def _draft_clear(self):
        with self._draft_lock:
            self._draft_reset()
        self._force_draft = False

    def _draft_send(self):
        with self._draft_lock:
            said = self._draft_reset()
        if said:
            self._dialogue_log = (self._dialogue_log + [{"user": said}])[-8:]
            self.partial("")
            self._mode_say(said)

    def _draft_loop(self):
        """말이 멈추면 교정하고, 교정본을 보인 뒤 CONFIRM_SECONDS 동안 새 말이 없으면 대화 모델에게 보낸다.
        새 말이 오면 지난 교정본(previous)과 함께 다시 교정한다. 외국어 회화는 교정 없이 이어 붙이기만 한다."""
        from correct import CorrectionError, correct
        while not self.stopping.is_set():
            self._draft_wake.wait(0.15)
            self._draft_wake.clear()
            if self.mode == "general":
                continue
            speaking = self.endpointer.speaking
            with self._draft_lock:
                if not self._draft_segments:
                    self._force_draft = False
                    continue
                rev, done = self._draft_rev, self._draft_done
                segments, previous = list(self._draft_segments), self._draft_text
            forced = self._force_draft
            if (speaking or self._stt_running or not self._utterances.empty()) and not forced:
                continue   # 말하는 중·받아쓰는 중에는 기다린다(끝나면 받아쓴 말이 원고에 더해진다)
            if done != rev:
                addressed, joined = True, " ".join(segments)
                text = joined
                if self.mode == "dialogue" and self.correction:
                    try:
                        result = correct(list(self._dialogue_log), segments, previous)
                        text, addressed = result.text, result.addressed
                    except CorrectionError as e:
                        log(f"대화 교정 실패: {e}")
                        self.info(T("correction_failed", error=str(e)[:160]), "correction_failed")
                        text = joined
                with self._draft_lock:
                    if self._draft_rev != rev and not forced:
                        continue   # 교정하는 사이 새 말이 더해졌다: 다시 교정한다
                    if not addressed:   # 옆 사람과의 말·TV 소리 등: 보내지 않고 버린다
                        self._draft_reset()
                    else:
                        self._draft_text, self._draft_done, self._draft_done_segments = text, rev, len(segments)
                        self._confirm_at, self._confirm_activity = time.monotonic() + CONFIRM_SECONDS, self._activity
                if not addressed:
                    self._force_draft = False
                    self.out(dropped=text, why="context")
                    self.partial("")
                    continue
                self.partial(text)
                if not forced:
                    continue
            if forced:   # 오래 그대로였던 원고: 교정·판단을 마쳤으니 기다리지 않고 보낸다
                self._force_draft = False
                self._draft_send()
                continue
            if self._activity != self._confirm_activity:   # 교정본을 보인 뒤 새 말소리가 있었다(잡음이었으면 다시 기다린다)
                with self._draft_lock:
                    self._confirm_at, self._confirm_activity = time.monotonic() + CONFIRM_SECONDS, self._activity
                continue
            if time.monotonic() >= max(self._confirm_at, self._quiet_at + CONFIRM_SECONDS):
                self._draft_send()

    # ---- 대화모드·회화: 대화 모델이 답한다 ----

    def _mode_say(self, said: str):
        """모드 중 사용자가 한 말: 대화 모델에게(캐릭터에게 가지 않는다). 모드가 아니면 평소처럼 앱으로."""
        chat = self.chat
        if chat is not None:
            self._chat_q.put((chat, said, True))
        else:
            self.out(heard=said)

    def _chat_loop(self):
        """차례대로 대화 모델에 묻고, 답을 앱에 보이고(answer), `---` 위를 소리로 말하고, 회화 표현을 노트에 남긴다."""
        from modechat import END_MARK
        while not self.stopping.is_set():
            chat, said, show = self._chat_q.get()
            if chat is not self.chat:
                continue   # 모드가 바뀌었다
            if show:
                self.out(user=said)
            self._chat_status(T("chat_thinking"))
            try:
                answer = chat.reply(said)
            except Exception as e:
                log(f"대화 모델 실패: {type(e).__name__}: {e}")
                self.out(answer=T("chat_failed", model=chat.model, error=f"{e}"[:240]))
                self._chat_status(T("chat_failed_state"))
                continue
            if chat is not self.chat:
                continue
            ending = END_MARK in answer
            answer = answer.replace(END_MARK, "").rstrip()
            try:   # 나누기가 실패해도 이 스레드가 죽으면 다음 말부터 답이 없다
                shown, spoken, card = self._board_split(answer) if chat.mode == "language" else self._detail_split(answer)
            except Exception as e:
                log(f"답 나누기 실패: {type(e).__name__}: {e}")
                shown, spoken, card = answer, answer, None
            self.out(**({"answer": shown, "card": card} if card else {"answer": shown}))
            self._chat_status()
            if spoken.strip():
                self._speech.put((spoken, "chat", chat.lang))
                if chat.mode == "dialogue":
                    self._dialogue_log = (self._dialogue_log + [{"assistant": spoken}])[-8:]
            try:
                chat.save_notes(answer)
            except OSError as e:
                log(f"표현 노트 실패: {e}")
            if ending and chat is self.chat:   # 대화 모델이 끝내려는 뜻을 알아들었다
                self.set_mode("general")

    def _card_title(self, fallback: str) -> str:
        asked = next((x["user"] for x in reversed(self._dialogue_log) if "user" in x), "")
        title = re.sub(r"\s+", " ", asked)[:24].strip()
        return f"{datetime.now():%H:%M} {self.mode_label()} · {title or fallback}"

    def _detail_split(self, answer: str) -> tuple[str, str, dict | None]:
        """대화모드: (대화창 글, 소리로 읽을 글, 화이트보드 카드). `---` 아래(길거나 정리가 필요한 내용)는 카드로 보내고
        대화창에는 위만 둔다. 모델이 `---` 없이 표·목록을 답에 넣으면 그 첫 줄부터 아래를 카드로 보낸다."""
        lines = answer.strip().splitlines()
        cut = next((i for i, x in enumerate(lines) if re.fullmatch(r"\s*-{3,}\s*", x)), None)
        skip = 1
        if cut is None:
            cut = next((i for i, x in enumerate(lines) if re.match(r"\s*(\||[-*•]\s|\d+[.)]\s)", x)), None)
            skip = 0
            if not cut:   # 표·목록이 없거나 맨 앞부터라 말로 읽을 요점이 없다
                return answer, answer, None
        head = "\n".join(lines[:cut]).strip()
        body = "\n".join(lines[cut + skip:]).strip()
        if not body:
            return head, head, None
        return head + "\n\n" + T("see_board"), head, {"title": self._card_title(T("mode_dialogue")), "body": body}

    def _board_split(self, answer: str) -> tuple[str, str, dict | None]:
        """외국어 회화: (대화창 글, 소리로 읽을 글, 화이트보드 카드). 대화창에는 외국어 문장과 번역 줄만 두고,
        나머지 설명·표현은 카드로 보낸다. 모델이 `---` 위를 빼먹으면 캐릭터 이름 줄을 대신 쓴다."""
        lines = answer.strip().splitlines()
        cut = next((i for i, x in enumerate(lines) if re.fullmatch(r"\s*-{3,}\s*", x)), None)
        if cut is None:
            return answer, answer, None
        head = "\n".join(lines[:cut]).strip()
        name, translation = self.chat.name if self.chat else "", T("label_translation")
        said = translated = ""
        rest = []
        for line in lines[cut + 1:]:
            m = re.match(r"\s*\*\*(.+?)\*\*\s*:?\s*(.*)", line)
            if m and m.group(1).strip() == name and not said:
                said = m.group(2).strip()
            elif m and m.group(1).strip() == translation and not translated:
                translated = m.group(2).strip()
            else:
                rest.append(line)
        head = head or said
        body = "\n".join(rest).strip()
        shown = head + (f"\n\n**{translation}** : {translated}" if translated else "")
        card = {"title": f"{datetime.now():%H:%M} {self.mode_label()} · {T('label_expressions')}", "body": body} if body else None
        return shown, head, card


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", nargs="*", type=Path, help="시험용: 마이크 대신 흘려 넣을 wav 파일")
    parser.add_argument("--no-sound", action="store_true", help="시험용: 스피커로 소리를 내지 않는다")
    parser.add_argument("--fast", action="store_true", help="시험용: 파일을 실제 시간보다 빨리 흘려 넣는다")
    args = parser.parse_args()
    out = Out()
    voice = Voice(out, input_files=args.input, no_sound=args.no_sound, realtime=not args.fast)
    ready = True
    try:
        voice.load()
    except Exception as e:
        log(f"음성 준비 실패: {type(e).__name__}: {e}")
        failure = T("load_failed", error=f"{type(e).__name__}: {e}"[:240])
        out(state="error", detail=failure)
        ready = False   # 끝나지 않고 기다린다(앱이 다시 띄우기를 되풀이하지 않게). 명령마다 같은 상태로 답한다
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        if not isinstance(cmd, dict):
            continue
        if cmd.get("quit"):
            break
        if not ready:
            out(state="error", detail=failure)
            continue
        try:
            voice.handle(cmd)
        except Exception as e:
            log(f"명령 처리 실패: {type(e).__name__}: {e}")
            out(info=T("command_failed", error=f"{type(e).__name__}: {e}"[:160]), code="command_failed")
    voice.stopping.set()
    if ready:
        voice.stop_speaking()
        if voice.meeting is not None and voice.meeting.active:
            voice.meeting.stop()   # 앱이 꺼지면 녹음을 마치고 저장한다(정리는 다음에 요청)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    sys.exit(main())
