"""대화모드·외국어 회화의 대화 상대: 캐릭터(앱의 대화 엔진) 대신 작업 분야 '음성 대화'(voice_chat)의 모델이 답한다.

- 지침은 modes/<모드>.<화면 언어>.md({name} = 캐릭터 대화명, {language} = 회화 언어 이름).
- 대화 기록은 모드가 켜져 있는 동안만 이 프로세스에 둔다(모드를 끝내면 버린다. 대화창 기록은 앱이 남긴다).
  모델은 sdk.llm 으로 한 번씩 부르고, 최근 대화를 글로 붙여 보낸다.
- 외국어 회화: 답의 '새 표현' 줄과 '나' 줄의 고친 문장을 data/mydata/language/YYYYMMDD-HHMM_<언어>.md(회화 한 번에 파일 하나)에 덧붙인다.

AI Jarvis 커밋 1fb9731 의 tools/음성/mode_chat.py 에서 옮겼다(모델 부르기는 sdk.llm, 이름표는 화면 언어별 글).
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

from sdk import llm

HERE = Path(__file__).resolve().parent
DOMAIN = "voice_chat"   # 작업 분야 '음성 대화'(환경설정 > 작업 분야별 모델)
HISTORY_TURNS = 20      # 모델에 함께 보내는 최근 주고받기 수
NOTE_LINES = 60         # 지침에 붙이는 지난 표현 노트 줄 수
END_MARK = "[end mode]"   # 모델이 모드를 끝내려는 뜻을 알아들었을 때 답 맨 끝에 쓰는 줄(지침)


def guide_text(mode: str, ui_language: str, name: str, language_name: str) -> str:
    folder = HERE / "modes"
    path = folder / f"{mode}.{ui_language}.md"
    if not path.is_file():
        path = folder / f"{mode}.ko.md"
    return path.read_text(encoding="utf-8").replace("{name}", name).replace("{language}", language_name)


def note_lines(markdown: str, me: str, new: str, fixed: str) -> list[str]:
    """답에서 표현 노트로 남길 줄: '새 표현' 아래의 '- …' 줄, '나' 줄의 '→ 고친 문장'. me·new 는 이름표, fixed 는 노트 머리말."""
    notes, in_new = [], False
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith(f"**{new}**"):
            in_new = True
            continue
        if line.startswith("**"):
            in_new = False
        if in_new and raw.startswith("- "):   # 들여쓴 예문 줄은 빼고 표현 줄만
            notes.append(line)
        said = re.match(rf"\*\*{re.escape(me)}\*\*\s*:\s*(.+?)\s*\(\s*→\s*(.+?)\s*\)\s*$", line)
        if said:   # "( → 고친 문장 : 이유 )"
            better = re.split(r"\s+[—:]\s+", said.group(2))[0].strip()
            if better and better != said.group(1).strip():
                notes.append(f"- [{fixed}] {said.group(1).strip()} → {better}")
    return notes


class ModeChat:
    def __init__(self, mode: str, lang: str, *, name: str, ui_language: str, labels: dict, notes_dir: Path):
        """mode: dialogue|language. lang: 회화 언어 코드(en 등). labels: 화면 언어의 이름표(texts.toml)."""
        self.mode, self.lang, self.name, self.labels = mode, lang, name, labels
        self.notes_dir = notes_dir
        self.history: list[tuple[str, str]] = []   # (말한 이, 글): "user" | "assistant"
        self._lock = threading.Lock()
        self._notes_file = notes_dir / f"{datetime.now():%Y%m%d-%H%M}_{lang}.md"
        self.guide = guide_text(mode, ui_language, name, labels.get("language", lang))

    @property
    def model(self) -> str:
        return llm.model_for(DOMAIN)

    def _system(self) -> str:
        now = datetime.now()
        parts = [self.guide, f"\n\n# {self.labels['now']}\n{now:%Y-%m-%d %A %H:%M}"]
        if self.mode == "language":
            notes = sorted(self.notes_dir.glob(f"*_{self.lang}.md")) if self.notes_dir.is_dir() else []
            lines = []
            for path in notes[-3:]:
                lines += path.read_text(encoding="utf-8", errors="replace").splitlines()
            parts.append(f"\n\n# {self.labels['notes']}\n" + ("\n".join(lines[-NOTE_LINES:]) if lines else self.labels["notes_none"]))
        return "".join(parts)

    def _prompt(self, user_text: str) -> str:
        """최근 대화를 글로 붙인 이번 요청."""
        who = {"user": self.labels["user"], "assistant": self.name}
        past = [f"{who[role]}: {said}" for role, said in self.history[-HISTORY_TURNS * 2:]]
        head = f"# {self.labels['past']}\n" + "\n\n".join(past) + "\n\n" if past else ""
        return f"{head}# {self.labels['this_turn']}\n{user_text}"

    def reply(self, user_text: str) -> str:
        """사용자 말 하나에 대한 답(마크다운). 기록에 둘 다 남긴다. 실패하면 llm.LLMError."""
        with self._lock:
            answer = llm.ask(DOMAIN, self._prompt(user_text), system=self._system(), search=True, timeout=120)
            self.history += [("user", user_text), ("assistant", answer)]
            return answer

    def save_notes(self, answer: str) -> int:
        """외국어 회화: 답의 새 표현·고친 문장을 이번 회화의 노트 파일에 덧붙인다. 덧붙인 줄 수."""
        if self.mode != "language":
            return 0
        lines = note_lines(answer, self.labels["me"], self.labels["new"], self.labels["fixed"])
        if not lines:
            return 0
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        head = "" if self._notes_file.exists() else f"# {datetime.now():%Y-%m-%d} {self.labels.get('language', self.lang)}\n\n"
        with self._notes_file.open("a", encoding="utf-8") as f:
            f.write(head + "\n".join(lines) + "\n")
        return len(lines)
