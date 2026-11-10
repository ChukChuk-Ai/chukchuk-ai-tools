"""도구 '음성'의 빠른 실행: 마이크 켜기·끄기, GPU 받아쓰기 준비. 음성 일은 앱이 띄워 둔 voice.py 가 하고, 여기서는 명령 한 줄만 남긴다
(data.request_voice → 앱이 음성 프로세스에 그대로 넘긴다).
- GPU 준비(gpu_setup): NVIDIA GPU 가 있는데 CUDA 12 라이브러리가 없으면 nvidia-cublas-cu12·nvidia-cudnn-cu12(약 2GB)를
  이 도구 환경(.venv)에 뒤 작업으로 설치하고, 끝나면 알린 뒤 음성을 다시 띄운다(받아쓰기 모델 '자동'이 large-v3-turbo 를 GPU 로 쓴다).
  requirements 에 넣지 않은 것은 GPU 가 없는 PC 에 2GB 를 깔지 않으려는 것이다.

- 대화모드·외국어 회화 켜기·끄기: "대화모드 시작", "영어 회화 시작", "대화모드 끝"·"회화 끝" → {"mode": …}.
- 회의: "회의 시작"·"회의 끝"·"회의 정리해줘"·"회의 정리 취소" → {"meeting": …}. "최근 회의록 보여줘"는 여기서 요약 파일을 읽어 카드로 보인다.

AI Jarvis 커밋 427b471 의 tools/음성/run.py 에서 마이크 켜기·끄기, 커밋 cc00c35 의 같은 파일에서 모드 켜기·끄기를 옮겼다(회의는 다음 걸음).
인자 JSON: {"trigger", "text", "action": "mic_on|mic_off|gpu_setup|dialogue|language|general|meeting_start|meeting_stop|
meeting_process|meeting_cancel|meeting_show", "lang": "en|영어|English…"}.
알아듣지 못한 말은 종료 코드 10(앱이 캐릭터에게 넘긴다).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("CHUKCHUK_ROOT") or str(HERE.parents[1]))   # sdk 찾기(앱 폴더는 앱이 알려 준다)
from sdk import data, system, text  # noqa: E402

T = text.load(HERE)
GPU_PACKAGES = ["nvidia-cublas-cu12>=12.4,<13", "nvidia-cudnn-cu12>=9.1,<10"]   # ctranslate2 4.x(faster-whisper)가 쓰는 CUDA 12·cuDNN 9
GPU_LOG = data.tool_dir("voice") / "gpu_install.log"
GPU_LOCK = data.tool_dir("voice") / "gpu_install.lock"
GPU_WORDS = {"음성gpu준비해줘", "음성gpu준비", "gpu받아쓰기준비해줘", "gpu받아쓰기준비", "gpu로받아쓰기", "음성gpu설치해줘",
             "preparevoicegpu", "setupvoicegpu", "voicegpusetup"}
ON_WORDS = {"마이크켜줘", "마이크켜", "마이크켜기", "마이크켜줄래", "마이크좀켜줘", "turnonthemic", "turnonmic", "turnonthemicrophone",
            "turnonmicrophone", "micon", "switchonthemic"}
OFF_WORDS = {"마이크꺼줘", "마이크꺼", "마이크끄기", "마이크꺼줄래", "마이크좀꺼줘", "turnoffthemic", "turnoffmic", "turnoffthemicrophone",
             "turnoffmicrophone", "micoff", "switchoffthemic"}
DIALOGUE_WORDS = {"대화모드시작", "대화모드시작해줘", "대화모드켜줘", "대화모드로바꿔줘", "대화모드로전환", "대화모드로전환해줘", "대화모드",
                  "startdialoguemode", "dialoguemode", "dialoguemodeon", "switchtodialoguemode"}
GENERAL_WORDS = {"대화모드끝", "대화모드종료", "대화모드꺼줘", "회화끝", "회화종료", "회화그만", "일반모드", "일반모드로바꿔줘",
                 "일반모드로전환", "일반모드로전환해줘", "enddialoguemode", "endconversation", "endpractice", "normalmode",
                 "switchtonormalmode", "backtonormalmode"}
MEETING_WORDS = {
    "meeting_start": {"회의시작", "회의시작해줘", "회의녹음시작", "회의녹음시작해줘", "회의녹음해줘", "녹음시작", "녹취시작",
                      "startmeeting", "startmeetingrecording", "recordthemeeting"},
    "meeting_stop": {"회의끝", "회의종료", "회의녹음끝", "회의녹음종료", "녹음끝", "녹음종료", "녹취끝", "녹취종료",
                     "endmeeting", "stopmeeting", "stopmeetingrecording"},
    "meeting_process": {"회의정리해줘", "회의정리", "회의내용정리해줘", "회의내용정리", "회의록만들어줘", "회의요약해줘",
                        "summarizethemeeting", "summarizemeeting", "makemeetingminutes"},
    "meeting_cancel": {"회의정리취소", "회의정리그만", "cancelmeetingsummary"},
    "meeting_show": {"최근회의록보여줘", "회의록보여줘", "회의요약보여줘", "지난회의록보여줘", "showmeetingminutes",
                     "showthelatestmeeting", "showmeetingsummary"},
}
LANGUAGE_NAMES = {"영어": "en", "일본어": "ja", "일어": "ja", "중국어": "zh", "스페인어": "es", "프랑스어": "fr", "불어": "fr", "독일어": "de",
                  "english": "en", "japanese": "ja", "chinese": "zh", "spanish": "es", "french": "fr", "german": "de"}


def norm(said: str) -> str:
    return re.sub(r"[\s.!?~,]+", "", str(said or "")).casefold()


def action_of(args: dict) -> str:
    """표준 값(action)을 먼저, 없으면 말에서. 모르면 빈 글."""
    action = str(args.get("action") or "").strip().lower()
    if action in ("mic_on", "mic_off", "gpu_setup", "gpu_install_job", "dialogue", "language", "general", *MEETING_WORDS):
        return action
    if str(args.get("lang") or "").strip():   # 문장 틀 '{lang} 회화 시작'
        return "language"
    said = norm(args.get("text", ""))
    if said in DIALOGUE_WORDS:
        return "dialogue"
    if said in GENERAL_WORDS:
        return "general"
    for name, words in MEETING_WORDS.items():
        if said in words:
            return name
    if said in GPU_WORDS:
        return "gpu_setup"
    if said in ON_WORDS:
        return "mic_on"
    if said in OFF_WORDS:
        return "mic_off"
    return ""




def gpu_state() -> tuple[int, bool]:
    """(NVIDIA GPU 수, CUDA 라이브러리를 불러올 수 있는가). 이 도구 환경의 파이썬에서 돈다."""
    sys.path.insert(0, str(HERE))
    from voicelib.listen.stt import cuda_device_count, cuda_libraries_ready
    gpus = cuda_device_count()
    return gpus, bool(gpus) and cuda_libraries_ready()


def gpu_setup() -> int:
    gpus, ready = gpu_state()
    if not gpus:
        print(f"# {T('gpu_title')}\n\n> {T('gpu_none')}")
        return 0
    if ready:
        print(f"# {T('gpu_title')}\n\n> {T('gpu_ready')}")
        return 0
    if GPU_LOCK.exists():
        print(f"# {T('gpu_title')}\n\n> {T('gpu_running')}")
        return 0
    GPU_LOCK.write_text("", encoding="utf-8")
    system.spawn_background([sys.executable, str(HERE / "run.py"), json.dumps({"action": "gpu_install_job"})], GPU_LOG, cwd=str(HERE))
    print(f"# {T('gpu_title')}\n\n> {T('gpu_started')}\n\n{T('gpu_log', path=GPU_LOG)}")
    return 0


def gpu_install_job() -> int:
    """뒤 작업: pip 로 설치하고 결과를 앱에 알린다(카드). 되면 음성을 다시 띄워 GPU 로 받아쓴다."""
    try:
        done = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *GPU_PACKAGES],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
                              creationflags=system.NO_WINDOW)
        print(done.stdout[-4000:], done.stderr[-4000:], sep="\n", flush=True)
        _, ready = gpu_state() if done.returncode == 0 else (0, False)
        if ready:
            data.notify(f"# {T('gpu_title')}\n\n> {T('gpu_done')}", sound=True)
            data.request_voice({"restart": True})
        else:
            tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [f"exit {done.returncode}"]
            data.notify(f"# {T('gpu_title')}\n\n> {T('gpu_install_failed', reason=tail[0][:200])}\n\n{T('gpu_log', path=GPU_LOG)}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        data.notify(f"# {T('gpu_title')}\n\n> {T('gpu_install_failed', reason=type(exc).__name__)}")
    finally:
        GPU_LOCK.unlink(missing_ok=True)
    return 0


def language_code(value: str) -> str:
    """'영어'·'English'·'en' → 'en'. 모르면 빈 글."""
    word = norm(value)
    if word in LANGUAGE_NAMES.values():
        return word
    return LANGUAGE_NAMES.get(word) or LANGUAGE_NAMES.get(word.removesuffix("회화")) or ""


def meeting(what: str) -> int:
    """회의 녹음·정리 명령은 음성 프로세스에 넘기고(결과는 음성이 앱에 알린다), 최근 회의록 보기는 여기서 요약 파일을 읽는다."""
    if what == "show":
        sys.path.insert(0, str(HERE))
        from meeting import latest_folder, summary_card
        root = data.mydata("meeting")
        folders = sorted((p for p in root.glob("[0-9]" * 8 + "-*") if (p / "summary.md").is_file()), key=lambda p: p.name)
        if not folders:
            print(f"# {T('meeting_summary_heading')}\n\n> {T('card_meeting_none')}")
            return 0
        card = summary_card(folders[-1])
        latest = latest_folder(root)
        later = f"\n\n> {T('meeting_unprocessed', name=latest.name)}" if latest is not None and latest != folders[-1] else ""
        print(f"# {card['title']}\n\n{card['body']}{later}")
        return 0
    data.request_voice({"meeting": what})
    print(f"# {T(f'card_meeting_{what}')}\n\n> {T(f'card_meeting_{what}_text')}")
    return 0


def switch_mode(action: str, args: dict) -> int:
    """대화모드·외국어 회화 켜기·끄기(음성 프로세스가 모드를 바꾼다. 모드 중 말은 작업 분야 '음성 대화' 모델이 답한다)."""
    if action == "dialogue":
        data.request_voice({"mode": "dialogue"})
        print(f"# {T('card_dialogue')}\n\n> {T('card_dialogue_text')}")
        return 0
    if action == "general":
        data.request_voice({"mode": "general"})
        print(f"# {T('card_general')}\n\n> {T('card_general_text')}")
        return 0
    lang = language_code(str(args.get("lang") or "en"))
    if not lang:
        if args.get("trigger") == "assistant":
            print(f"# {T('mode_language', language='?')}\n\n> {T('unknown_language')}")
            return 0
        return 10   # 모르는 언어 이름: 캐릭터에게 넘긴다
    name = T(f"lang_{lang}")
    data.request_voice({"mode": "language", "lang": lang})
    print(f"# {T('card_language', language=name)}\n\n> {T('card_language_text', language=name)}")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = json.loads(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else {}
    except ValueError:
        args = {}
    action = action_of(args if isinstance(args, dict) else {})
    if not action:
        return 10
    if action == "gpu_setup":
        return gpu_setup()
    if action == "gpu_install_job":
        return gpu_install_job()
    if action in ("dialogue", "language", "general"):
        return switch_mode(action, args)
    if action.startswith("meeting_"):
        return meeting(action.removeprefix("meeting_"))
    data.request_voice({"mic": action == "mic_on"})
    if action == "mic_on":
        print(f"# {T('card_mic_on')}\n\n> {T('card_mic_on_text')}")
    else:
        print(f"# {T('card_mic_off')}\n\n> {T('card_mic_off_text')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
