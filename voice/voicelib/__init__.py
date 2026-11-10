"""음성 도구 부품. AI Jarvis 의 음성 부품(원본 AI_Partner 사본)을 옮겨 듣기·말하기만 남겼다.

- settings: 설정 묶음(defaults.toml + 도구 설정)
- listen/: 말소리 검출·말 끝·받아쓰기·끼어들기
- speak/: 음성 합성(edge → Windows 내장)과 재생
- resident: 호출어·호출 유지·대화 끝 판단
- mic, audio_devices, audio_level: 마이크 입력·장치 감시·소리 크기
- os_audio: OS 별 코드 한 곳(기본 장치 식별자, 내장 음성). macOS 판은 이 파일만 새로 짠다
"""
