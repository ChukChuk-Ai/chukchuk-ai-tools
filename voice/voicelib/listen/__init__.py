"""듣기: 말소리 검출, 말 끝 판단, 음성 인식."""

# 마이크 입력과 모델들이 공통으로 쓰는 값
SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # 음성 검출 모델이 한 번에 받는 길이 (0.032초)
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
