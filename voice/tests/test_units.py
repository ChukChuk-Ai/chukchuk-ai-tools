"""부품 시험(프로세스 없이): 호출어 판단, 말로 읽을 글, 설정 겹치기, 말하기 엔진 대체(edge 실패 → 내장 음성), 빠른 실행 말."""
import threading

import numpy as np
import pytest

from voicelib.resident import SessionRouter
from voicelib.settings import ResidentConfig, STTConfig, TTSConfig, load_settings, resolve_stt
from voicelib.speak.tts import FallbackTTS, TTSError, create_tts


def router(names=(), now=None, **kw):
    clock = now or [0.0]
    r = SessionRouter(ResidentConfig(**kw), clock=lambda: clock[0])
    r.set_name_calls(names)
    r.mute(False)   # 마이크 켬(호출 대기)
    return r


def test_character_name_wakes_at_start_and_end_only():
    r = router(["하늘"])
    assert r.awaiting_wake()
    assert r.route("오늘 날씨 어때").kind == "ignore"
    got = r.route("하늘아, 내일 일정 알려줘")
    assert (got.kind, got.text) == ("ask", "내일 일정 알려줘")
    r.end_call()
    got = r.route("내일 회의 일정 알려줘 하늘아")
    assert (got.kind, got.text) == ("ask", "내일 회의 일정 알려줘")
    r.end_call()
    assert r.route("우리 하늘이 학교 갔어").kind == "ignore"   # 문장 중간의 이름은 부른 것이 아니다


def test_no_names_means_nothing_wakes_and_names_can_change():
    r = router()
    assert not r.has_wake_words
    assert r.route("하늘아 내일 일정 알려줘").kind == "ignore"
    r.set_name_calls(["하늘"])
    assert r.route("하늘아 내일 일정 알려줘").kind == "ask"
    r.end_call()
    r.set_name_calls(["Sky"])   # 대화명을 바꾸면 이전 이름은 빠진다
    assert r.route("하늘아 내일 일정 알려줘").kind == "ignore"
    assert r.route("Sky, what's on tomorrow?").text == "what's on tomorrow?"


def test_name_end_can_be_turned_off_and_extra_wake_words_work():
    r = router(["하늘"], character_name_end=False, wake_words=("컴퓨터",))
    assert r.route("내일 일정 알려줘 하늘아").kind == "ignore"
    assert r.route("컴퓨터 불 꺼줘").text == "불 꺼줘"
    assert r.wake_words == ("하늘", "컴퓨터")


def test_call_lasts_silence_timeout_then_waits_and_end_phrase():
    now = [0.0]
    r = router(["하늘"], now=now, silence_timeout=60.0)
    assert r.skip_wake()                      # 마이크 버튼: 호출어 없이
    assert r.route("내일 일정 알려줘").kind == "ask"
    now[0] = 30.0
    assert r.route("모레는?").kind == "ask"
    now[0] = 100.0
    assert r.expire() and r.awaiting_wake()   # 60초 동안 말이 없었다
    assert r.route("모레는?").kind == "ignore"
    r.skip_wake()
    assert r.route("대화 끝.").kind == "end" and r.awaiting_wake()
    r.skip_wake()
    assert r.route("That's all").kind == "end"
    assert r.is_stop("그만") and r.is_stop("Stop it.") and not r.is_stop("그만 둘까 생각 중이야")




def test_speech_text_reads_only_above_rule_and_skips_symbols():
    from voice import speech_text
    said = speech_text("# 날씨\n오늘은 **맑음**입니다. [자세히](https://example.com)\n| 시각 | 기온 |\n|---|---|\n"
                       "▶ 내일 날씨\n▶ 취소\n---\n이 줄은 읽지 않는다")
    assert "맑음입니다" in said and "자세히" in said and "https" not in said and "|" not in said
    assert "내일 날씨" in said and "취소" not in said and "읽지 않는다" not in said
    assert speech_text("그냥 글입니다.") == "그냥 글입니다."


def test_settings_overlay_and_model_choice(tmp_path):
    from voice import DEFAULTS
    cfg = load_settings(DEFAULTS, {"stt_model": "huge", "wake_words": "컴퓨터, 비서", "tts_engines": "windows",
                                   "silence_timeout": 120, "barge_in": False, "edge_voice": ""}, "en")
    assert "stt_model" in cfg.warnings and cfg.stt.model == "auto"   # 틀린 값은 그 칸만 기본값
    assert cfg.resident.wake_words == ("컴퓨터", "비서") and cfg.resident.silence_timeout == 120
    assert cfg.tts.engines == ("windows",) and cfg.tts.edge_voice == "en-US-AriaNeural" and cfg.stt.language == "en"
    assert not cfg.barge_in.enabled and "stop" in cfg.barge_in.phrases
    assert load_settings(DEFAULTS, {}, "ko").tts.edge_voice == "ko-KR-SunHiNeural"
    auto = STTConfig()
    assert resolve_stt(auto, 1) == ("large-v3-turbo", "cuda", "int8")
    assert resolve_stt(auto, 0) == ("small", "cpu", "int8")
    assert resolve_stt(STTConfig(model="medium"), 0) == ("medium", "cpu", "int8")


class _Failing:
    name = "edge"

    def synthesize(self, said):
        raise TTSError("edge-tts: offline")

    def synthesize_stream(self, said, stopped):
        raise TTSError("edge-tts: offline")
        yield


class _Builtin:
    name = "windows"

    def synthesize(self, said):
        return np.full(2400, 0.1, dtype=np.float32), 24000


def test_tts_falls_back_from_failing_edge_to_builtin_voice():
    tts = FallbackTTS([_Failing(), _Builtin()])
    audio, rate = tts.synthesize("안녕하세요")
    assert rate == 24000 and len(audio) == 2400 and tts.last_used == "windows"
    chunks = list(FallbackTTS([_Failing(), _Builtin()]).synthesize_stream("안녕하세요", threading.Event()))
    assert len(chunks) == 1 and chunks[0][1] == 24000


def test_speaker_uses_builtin_voice_when_edge_fails():
    """실제 엔진: edge 에 없는 목소리 이름을 줘 실패시키면 Windows 내장 음성으로 읽는다(그 음성이 없는 PC 는 건너뜀)."""
    from voicelib.os_audio import BuiltinVoice
    from voicelib.speak.speaker import Speaker
    if not BuiltinVoice.available():
        pytest.skip("OS 내장 음성을 쓸 수 없는 환경")
    played = []
    tts = create_tts(TTSConfig(engines=("edge", "windows"), edge_voice="xx-XX-NoSuchVoiceNeural"), "ko")
    speaker = Speaker(tts, play=lambda audio, rate, stopped: played.append((len(audio), rate)))
    assert speaker.say("안녕하세요. 시험입니다.") is not None
    assert played and tts.last_used == "windows"


def test_run_py_understands_mic_phrases():
    import run
    assert run.action_of({"text": "마이크 켜줘"}) == "mic_on"
    assert run.action_of({"text": "Turn off the mic."}) == "mic_off"
    assert run.action_of({"action": "mic_on"}) == "mic_on"
    assert run.action_of({"text": "마이크 사줘"}) == ""


def test_small_model_transcribes_generated_speech(wavs):
    """실제 받아쓰기(small, CPU): edge 음성으로 만든 한국어 문장. 모델을 받을 수 없으면 건너뛴다."""
    import wave
    from voicelib.listen.stt import Transcriber
    path = wavs("stt", "안녕하세요, 내일 날씨를 알려주세요.")[0]
    try:
        stt = Transcriber(STTConfig(language="ko"), "small", "cpu", "int8", local_files_only=True)
    except Exception:
        try:
            stt = Transcriber(STTConfig(language="ko"), "small", "cpu", "int8")
        except Exception as e:
            pytest.skip(f"받아쓰기 모델을 받지 못했습니다: {type(e).__name__}")
    with wave.open(str(path), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    said = stt.transcribe(audio)
    assert "안녕" in said and "날씨" in said, said


def test_run_py_understands_mode_phrases():
    import run
    assert run.action_of({"text": "대화모드 시작"}) == "dialogue"
    assert run.action_of({"text": "회화 끝"}) == "general"
    assert run.action_of({"text": "영어 회화 시작", "lang": "영어"}) == "language"
    assert [run.language_code(x) for x in ("영어", "English", "ja", "불어", "독일어회화", "라틴어")] == ["en", "en", "ja", "fr", "de", ""]


def test_language_notes_take_new_expressions_and_fixes():
    from modechat import note_lines
    answer = ("Nice to meet you.\n---\n**하늘** : Nice to meet you.\n**한글번역** : 만나서 반가워요.\n\n"
              "**나** : I am go school ( → I go to school : 동사 하나만 )\n\n**새 표현**\n- nice ↔ glad : 반가운\n  - 예문 줄\n")
    assert note_lines(answer, "나", "새 표현", "고침") == ["- [고침] I am go school → I go to school", "- nice ↔ glad : 반가운"]


def test_correction_parse_and_preview():
    import correct
    got = correct.parse_correction('```json\n{"text": "강아지를 산책시켜줘", "uncertain": [], "complete": true, "to": "person"}\n```')
    assert got.text == "강아지를 산책시켜줘" and not got.addressed
    with pytest.raises(correct.CorrectionError):
        correct.parse_correction("교정했습니다")
    assert correct.preview_text("가아지를 산책시켜줘", "아니 가아지 말고 강아지") == "강아지를 산책시켜줘"


def test_mode_answers_split_for_chat_board_and_speech():
    """대화모드: `---` 아래는 화이트보드 카드. 회화: 대화창엔 외국어 문장과 번역 줄, 나머지는 카드, 소리는 외국어 문장만."""
    from types import SimpleNamespace
    import voice
    v = voice.Voice.__new__(voice.Voice)
    v.mode, v.lang, v._dialogue_log = "dialogue", "", [{"user": "책 추천해줘"}]
    v.chat = SimpleNamespace(name="하늘")
    shown, spoken, card = v._detail_split("두 권을 골랐어요.\n---\n| 책 | 이유 |\n|---|---|\n| 가 | 나 |")
    assert spoken == "두 권을 골랐어요." and shown.startswith(spoken) and "| 책" in card["body"] and "책 추천" in card["title"]
    assert v._detail_split("그냥 짧은 답이에요.") == ("그냥 짧은 답이에요.", "그냥 짧은 답이에요.", None)
    v.mode, v.lang = "language", "en"
    tr = voice.T("label_translation")
    shown, spoken, card = v._board_split(f"Hello! How are you?\n---\n**하늘** : Hello! How are you?\n**{tr}** : 안녕!\n\n**{voice.T('label_new')}**\n- hi ↔ hello : 인사")
    assert spoken == "Hello! How are you?" and f"**{tr}** : 안녕!" in shown and "hi ↔ hello" in card["body"] and "하늘" not in card["body"]
