# ChukChuk AI shared tools (척척아이 공유 도구)

[척척아이(ChukChuk AI)](https://github.com/chukchuk-ai) 앱에서 받아 쓰는 공유 도구 목록입니다. 맨 위의 `catalog.json`이 목록이고, 폴더 하나가 도구 하나입니다.

## 받아 쓰기

1. 척척아이에서 "도구 목록 보여줘"라고 합니다(환경설정 > 도구 설정 > 도구설치의 '공유 목록 주소'가 이 저장소).
2. "○○ 도구 설치해줘"라고 하면, 받아서 실행하지 않고 살펴본 카드(하는 일·접속하는 곳·읽고 쓰는 파일·주의할 점)가 나옵니다.
3. 카드의 [○○ 설치 확정] 버튼을 누르면 설치됩니다. 필요한 패키지는 처음 실행할 때 설치됩니다.

설치한 도구는 내 PC 에서 모든 권한으로 돕니다. 카드를 보고 판단해 주세요.

## 올리기(풀 리퀘스트)

1. 척척아이에서 "○○ 도구 내보내기": 개인 흔적·비밀값 검사를 통과하면 zip 과 목록 항목 예시(내용 지문 포함)가 나옵니다.
2. 이 저장소를 포크해 zip 을 푼 도구 폴더를 맨 위에 두고, `catalog.json`의 `tools`에 항목을 더해 풀 리퀘스트를 보냅니다.
   - 자기 저장소에 둔 도구라면 폴더 대신 `url`(커밋을 고정한 zip 주소)과 그 안의 `path`를 적습니다.
3. 지킬 것:
   - `id`는 `작성자/도구`(영문 소문자·숫자·`-`·`_`), `license`는 꼭 적습니다.
   - 개인정보(이름, 계정, 경로, 토큰)를 넣지 않습니다.
   - 약관에 걸릴 수 있는 자동화(예약 매크로 등)는 받지 않습니다.
   - 관리자가 코드를 읽고 합칩니다. 도구를 고치면 내용 지문(`sha256`)도 새로 바꿉니다.

---

# ChukChuk AI shared tools

The catalog of shared tools for the ChukChuk AI app: `catalog.json` at the top is the list, and each folder is one tool.

- **Get a tool:** in ChukChuk AI say "show the tool list" (the catalog address in Settings > Tool settings > 도구설치 points here), then "install the ○○ tool". It downloads the tool without running it and shows a review card (what it does, where it connects, files it reads and writes, risks); press [confirm installing ○○] to install. Packages are installed on first run. Installed tools run with full permissions on your PC.
- **Share a tool:** say "export the ○○ tool" (it checks for personal traces and secrets and shows a catalog entry with the content fingerprint), fork this repository, add the tool folder and the entry to `catalog.json`, and open a pull request. For a tool kept in your own repository, give `url` (a zip address pinned to a commit) and the `path` inside it instead. `id` is `author/tool`, `license` is required, no personal data, no automation that breaks other services' terms. Maintainers read the code before merging; update `sha256` when a tool changes.
