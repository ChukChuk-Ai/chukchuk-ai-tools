"""시험 도우미: voice.py 를 앱처럼 띄우고(작업 폴더 = 앱 폴더, CHUKCHUK_ROOT) 시험 모드(--input 파일, --no-sound)로
명령을 보내며 사건을 모은다. 도구 설정은 VOICE_SETTINGS(JSON)로 준다."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1]
ROOT = TOOL.parents[1]
# 시험 기본: CPU 의 small(가장 작게 받는 모델), 한국어, 대화가 끝나도 마이크를 끄지 않음(여러 문장을 이어 시험)
BASE_SETTINGS = {"stt_model": "small", "language": "ko", "mic_always_on": True}


class VoiceProc:
    def __init__(self, inputs=(), *, settings=None, fast=False):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", CHUKCHUK_ROOT=str(ROOT),
                   VOICE_SETTINGS=json.dumps({**BASE_SETTINGS, **(settings or {})}, ensure_ascii=False))
        cmd = [sys.executable, str(TOOL / "voice.py"), "--no-sound", "--input", *map(str, inputs)] + (["--fast"] if fast else [])
        self.p = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", env=env)
        self.events = queue.Queue()
        self.all = []
        self.log = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_log, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.all.append(event)
            self.events.put(event)

    def _read_log(self):
        for line in self.p.stderr:
            self.log.append(line.rstrip())

    def send(self, **cmd):
        self.p.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def wait_for(self, check, timeout=120):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                event = self.events.get(timeout=0.5)
            except queue.Empty:
                if self.p.poll() is not None:
                    raise RuntimeError("음성 프로세스가 끝났습니다: " + "\n".join(self.log[-20:]))
                continue
            if check(event):
                return event
        raise TimeoutError(f"기다린 사건이 오지 않았습니다. 지금까지: {self.all[-15:]}")

    def ready(self, timeout=600):
        """준비(state off)까지 기다린다. 모델을 받지 못해 준비에 실패하면 시험을 건너뛴다."""
        event = self.wait_for(lambda e: e.get("state") in ("off", "waiting", "called", "error"), timeout=timeout)
        if event.get("state") == "error":
            self.close()
            pytest.skip(f"음성 준비 실패(모델을 받지 못했을 수 있음): {event.get('detail')}")
        return event

    def close(self):
        try:
            self.send(quit=True)
            return self.p.wait(timeout=20)
        except Exception:
            self.p.kill()
            return None
