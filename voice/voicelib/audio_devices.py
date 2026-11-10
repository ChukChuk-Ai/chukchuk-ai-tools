"""오디오 장치 감시. 기본 마이크·스피커가 바뀌었는지 알아내고, PortAudio 의 장치 목록을 새로 읽는다.

원격 데스크톱 연결이 열리고 닫히거나 장치를 꽂고 뺄 때 OS 의 기본 장치가 바뀐다. PortAudio(sounddevice)는
장치 목록을 처음 불러올 때만 읽고 그 뒤로는 갱신하지 않으므로, 그대로 두면 이전 장치를 붙들고 있거나(소리가 없음)
장치가 없다는 오류가 난다. 그래서
1) 기본 장치의 식별자를 OS 에 직접 물어(os_audio) 바뀌었는지 가볍게 확인하고(소리 스트림을 건드리지 않음),
2) 바뀌었을 때만 스트림을 닫고 PortAudio 를 다시 초기화한 뒤 새 장치로 다시 연다.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from voicelib.os_audio import DeviceIds, DeviceInfos, default_device_ids, default_device_infos  # noqa: F401

_portaudio_condition = threading.Condition()
_portaudio_users = 0
_portaudio_refreshing = False


@contextmanager
def portaudio_use():
    """스트림 생성부터 close 반환까지 스트림과 장치 조회를 재초기화에서 보호한다(입력·출력 동시 사용, 중첩 사용 허용)."""
    global _portaudio_users
    with _portaudio_condition:
        _portaudio_condition.wait_for(lambda: not _portaudio_refreshing)
        _portaudio_users += 1
    try:
        yield
    finally:
        with _portaudio_condition:
            _portaudio_users -= 1
            _portaudio_condition.notify_all()


class DeviceReport:
    """기본 마이크·스피커의 바뀜·끊김·다시 찾음을 알린다. step() 을 주기적으로 부르면 바뀐 것만 알린다.

    out(devices={...}) 로 앱에 지금 장치 이름을, info(글) 로 사람이 읽을 알림을 보낸다. T 는 도구의 언어별 문자열.
    """

    def __init__(self, get: Callable[[], DeviceInfos], out: Callable[..., None], info: Callable[[str], None],
                 T: Callable[..., str], on_change: Callable[[], None] | None = None) -> None:
        self._get, self._out, self._info, self._T, self._on_change = get, out, info, T, on_change
        self._last: DeviceInfos | None = None

    def step(self) -> None:
        try:
            now = self._get()
        except Exception:
            return  # 읽지 못하면 바뀌었다고 단정하지 않는다
        if now == self._last:
            return
        first, before = self._last is None, self._last or (None, None)
        self._last = now
        self._out(devices={"mic": now[0][1] if now[0] else None, "speaker": now[1][1] if now[1] else None})
        for kind, old, new in zip(("mic", "speaker"), before, now):
            label = self._T(f"device_{kind}")
            if new == old and not (first and new is None):
                continue
            if new is None:
                self._info(self._T("device_missing", device=label))
            elif first:
                self._info(self._T("device_now", device=label, name=new[1]))
            elif old is None:
                self._info(self._T("device_found", device=label, name=new[1]))
            else:
                self._info(self._T("device_changed", device=label, old=old[1], name=new[1]))
        if self._on_change and not first:
            self._on_change()


def refresh_portaudio(*, timeout: float = 5.0) -> None:
    """입력·출력의 close 까지 기다린 뒤 장치 목록을 갱신한다. 사용 중이면 파괴하지 않고 보류한다."""
    global _portaudio_refreshing
    import sounddevice as sd

    with _portaudio_condition:
        if not _portaudio_condition.wait_for(lambda: not _portaudio_refreshing and _portaudio_users == 0, timeout=timeout):
            raise sd.PortAudioError("audio devices busy; refresh postponed")
        _portaudio_refreshing = True
    try:
        try:
            sd._terminate()
        except Exception:
            pass
        sd._initialize()
    finally:
        with _portaudio_condition:
            _portaudio_refreshing = False
            _portaudio_condition.notify_all()


class DeviceWatcher:
    """기본 마이크·스피커가 바뀌었는지 일정 간격으로 확인한다(듣는 스레드가 프레임을 받을 때마다 부른다)."""

    def __init__(self, get_ids: Callable[[], DeviceIds] = default_device_ids, interval: float = 2.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._get_ids, self._interval, self._clock = get_ids, interval, clock
        try:
            self._ids = get_ids()
        except Exception:
            self._ids = (None, None)
        self._checked = clock()

    @property
    def ids(self) -> DeviceIds:
        return self._ids

    def changed(self) -> bool:
        """간격이 지났고 기본 장치가 바뀌었으면 True (기준도 새 장치로 바꾼다)."""
        now = self._clock()
        if now - self._checked < self._interval:
            return False
        self._checked = now
        try:
            ids = self._get_ids()
        except Exception:  # 확인에 실패하면 바뀌었다고 단정하지 않는다(스트림 끊김 등 다른 신호가 따로 알린다)
            return False
        if ids == self._ids:
            return False
        self._ids = ids
        return True
