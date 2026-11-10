"""음성 프로세스 시험(앱처럼 띄움: 실제 small 모델, 마이크 대신 파일, 소리 없이)."""
import time

from run_voice import VoiceProc

LONG = "오늘은 날씨가 맑고 기온은 십팔도입니다. 오후에는 구름이 조금 끼겠고 저녁에는 바람이 불겠습니다. 내일도 맑은 날씨가 이어지겠습니다."


def heard(events):
    return [e["heard"] for e in events if "heard" in e]


def codes(events):
    return [e.get("code") for e in events if "info" in e]


def run(files, setup, settings=None, after=6.0):
    """준비 → setup(v) → 입력 파일이 다 흐를 때까지 → after 초 더 기다린 뒤 모든 사건."""
    v = VoiceProc(files, settings=settings)
    try:
        v.ready()
        setup(v)
        v.wait_for(lambda e: e.get("input_done"), timeout=180)
        time.sleep(after)
        return v.all
    finally:
        v.close()


def test_protocol_round_trip():
    """names·mic·say·stop·mode·quit 를 보내고 사건을 받는다. 마이크 대신 빈 입력(조용한 소리)."""
    v = VoiceProc([], settings={"mic_always_on": False})
    try:
        assert v.ready()["state"] == "off"
        v.send(names=["하늘"])
        v.send(mic=True)
        assert v.wait_for(lambda e: e.get("code") == "mic_on")
        assert v.wait_for(lambda e: e.get("state") == "called")
        # 회의 명령은 실제 마이크로 녹음하므로 여기서 보내지 않는다(test_meeting 이 가짜 장치로 본다)
        v.send(say="안녕하세요. 시험 문장입니다.\n---\n읽지 않는 줄")
        speaking = v.wait_for(lambda e: e.get("state") == "speaking", timeout=60)
        assert "읽지 않는" not in speaking["detail"]
        v.send(stop=True)
        v.wait_for(lambda e: e.get("state") == "called", timeout=60)
        v.send(mic=False)
        assert v.wait_for(lambda e: e.get("state") == "off")
        v.send(mic=True)
        assert v.wait_for(lambda e: e.get("state") == "called")
        # 대화모드: 모드 사건(화면 이름)·대화 모델 상태, 일반모드로 돌아가면 상태는 null 이고 마이크는 꺼진다(늘 켜 두기 아님)
        v.send(mode="dialogue")
        mode = v.wait_for(lambda e: "mode" in e)
        assert mode["mode"] == "dialogue" and mode["label"], mode
        assert v.wait_for(lambda e: isinstance(e.get("chat_info"), dict))["chat_info"]["model"]
        v.send(mode="language", lang="xx")   # 모르는 언어는 영어
        assert v.wait_for(lambda e: e.get("mode") == "language")["lang"] == "en"
        v.send(mode="general")
        assert v.wait_for(lambda e: e.get("mode") == "general")
        assert v.wait_for(lambda e: "chat_info" in e and e["chat_info"] is None)
        assert v.wait_for(lambda e: e.get("state") == "off")
    finally:
        assert v.close() == 0, v.log[-20:]


def test_dialogue_mode_listens_without_name_and_ends_by_voice(wavs):
    """대화모드: 이름을 부르지 않은 말도 입력창 원고(partial)로 모이고 앱의 대화(heard)로는 가지 않는다. "대화 끝내자"면 일반모드로."""
    files = wavs("dialogue", "SIL:2", "대화 끝내자")
    events = run(files, lambda v: (v.send(names=["하늘"]), v.send(mode="dialogue")), settings={"dialogue_correction": False})
    modes = [e["mode"] for e in events if "mode" in e]
    assert modes == ["dialogue", "general"], events
    assert not heard(events) and not [e for e in events if e.get("user")], events
    assert "mode_ended" in codes(events)


def dictated(events):
    return [e["dictated"] for e in events if "dictated" in e]


def test_mic_on_means_dictation(wavs):
    """일반모드에서 마이크가 켜져 있으면 이름을 부르지 않아도 받아쓴 말이 모두 dictated 로 간다(보내기·교정은 앱). 받아쓰는 중엔 partial."""
    files = wavs("dictate", "오늘 날씨 어때", "하늘아, 내일 일정 알려줘")
    events = run(files, lambda v: v.send(names=["하늘"]))
    said = dictated(events)
    assert len(said) == 2 and "날씨" in said[0] and "일정" in said[1], events
    assert not heard(events) and any(e.get("partial") for e in events)


def test_mic_turns_off_after_idle_minutes():
    """받아쓰기: 도구 설정 '말이 없으면 마이크 끄기'(분) 동안 말이 없으면 마이크를 끈다(시험은 0.05분 = 3초)."""
    v = VoiceProc([], settings={"dictate_idle": 0.05})
    try:
        v.ready()
        assert v.wait_for(lambda e: "dictate_idle" in e, timeout=30)
        assert v.wait_for(lambda e: e.get("state") == "off", timeout=10)
    finally:
        v.close()


def test_stop_word_while_speaking(wavs):
    """말하는 도중 "그만": 말을 멈추고, 멈춤 말은 요청으로 보내지 않는다."""
    files = wavs("barge", "SIL:3", "그만")
    events = run(files, lambda v: (v.send(names=["하늘"]), v.send(mic=True), v.send(say=LONG)))
    assert any(e.get("state") == "speaking" for e in events)
    assert {"barge_stopped", "level_stopped_voice", "level_stopped_loud"} & set(codes(events)), events
    assert not heard(events), events
