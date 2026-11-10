"""회의 녹음·정리 시험. 모델은 가짜(meeting.ask_model 바꿔 끼우기), 녹음 장치는 가짜(말소리 wav 를 흘려 보냄).
fake_device.py 는 AI Jarvis 커밋 2ed201a 의 tools/음성/tests/fake_device.py 에서 옮김."""
import json
import re
import time
import wave

import numpy as np

from fake_device import FakeBackend


def fake_model(calls):
    def ask(system, body):
        calls.append(system)
        if "JSON 배열" in system:   # 보정: 구간 ID 를 그대로 돌려준다
            return json.dumps([{"id": r["id"], "text": r["text"]} for r in json.loads(body)], ensure_ascii=False)
        return "## 결정사항\n- 12월 15일 골프 모임 [00:00:04]"
    return ask


def test_meeting_resumes_from_finished_steps(tmp_path, monkeypatch):
    """받아쓰기는 끝났고 모델이 실패하면 원음·받아쓰기 결과가 남고, 다시 정리하면 받아쓰기를 되풀이하지 않고 이어 한다."""
    import meeting
    folder = tmp_path / "20261110-0900"
    folder.mkdir()
    with wave.open(str(folder / "audio_0001.wav"), "wb") as w:
        w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        w.writeframes(np.zeros(16000 * 5, dtype=np.int16).tobytes())
    (folder / "session.json").write_text(json.dumps({"status": "recorded", "files": [{"name": "audio_0001.wav", "offset": 0.0}]}),
                                         encoding="utf-8")
    heard, events, calls = [], [], []
    transcribe = lambda path: heard.append(path) or [{"start": 0.5, "end": 2.0, "text": "안건은 골프 모임입니다"}]  # noqa: E731

    def fail(system, body):
        raise meeting.llm.LLMError("한도 초과")
    monkeypatch.setattr(meeting, "ask_model", fail)
    m = meeting.Meeting(tmp_path, transcribe=transcribe, notify=events.append)
    assert m.process()[0]
    m._worker.join(10)
    assert events[-1]["meeting"] == "error" and "한도 초과" in events[-1]["detail"]
    assert (folder / "stt_0001.json").is_file() and m.snapshot()["status"] == "processing_failed"
    again = meeting.Meeting(tmp_path, transcribe=transcribe, notify=events.append)   # 다시 켠 뒤
    again.recover()
    assert events[-1]["meeting"] == "unfinished"
    monkeypatch.setattr(meeting, "ask_model", fake_model(calls))
    assert again.process()[0]
    again._worker.join(10)
    assert len(heard) == 1, "받아쓰기를 되풀이했다"
    assert events[-1]["meeting"] == "processed" and "골프" in events[-1]["card"]["body"]
    assert "골프" in (folder / "transcript_corrected.md").read_text(encoding="utf-8")
    assert not again.process()[0] and events[-1]["meeting"] == "processed"   # 이미 정리한 회의: 카드만 다시


def test_meeting_record_transcribe_summarize(wavs, tmp_path, monkeypatch):
    """음성 프로세스 안에서: 녹음 중에는 state meeting(말하지 않음) → 끝 → 정리(실제 small 받아쓰기, 가짜 모델) → 요약 카드."""
    import meeting
    import voice
    files = wavs("meeting", "오늘 회의 안건은 골프 모임 일정입니다.", "날짜는 십이월 십오일로 정했습니다.")
    monkeypatch.setenv("VOICE_SETTINGS", json.dumps({"stt_model": "small", "language": "ko", "mic_always_on": False}))
    calls = []
    monkeypatch.setattr(meeting, "ask_model", fake_model(calls))
    events = []
    v = voice.Voice(lambda **e: events.append(e), no_sound=True)
    v.meeting_root = tmp_path / "meeting"
    v.meeting_backend = FakeBackend(files)
    v.load()
    try:
        v.handle({"meeting": "start"})
        assert v.meeting.active and re.fullmatch(r"\d{8}-\d{4}", v.meeting.folder.name)   # 사용자 자료 이름 규칙
        assert any(e.get("state") == "meeting" for e in events)
        v.handle({"say": "녹음 중에는 읽지 않는다"})
        time.sleep(9)
        v.handle({"meeting": "stop"})
        assert not v.played, "회의 녹음 중에 말했다"
        v.handle({"meeting": "process"})
        end = time.monotonic() + 300
        while v.meeting.busy and time.monotonic() < end:
            time.sleep(0.5)
        folder = v.meeting.folder
        assert "골프" in (folder / "transcript_original.txt").read_text(encoding="utf-8")
        assert (folder / "summary.md").is_file() and len(calls) >= 2
        assert [e["meeting"] for e in events if "meeting" in e] == ["recording", "stopped", "processing", "processed"]
        card = next(e["card"] for e in events if e.get("meeting") == "processed")
        assert folder.name in card["title"] and "12월 15일" in card["body"]
        v.handle({"meeting": "stop"})
        assert events[-1]["meeting"] == "idle"
    finally:
        v.stopping.set()
        v.meeting.close()
