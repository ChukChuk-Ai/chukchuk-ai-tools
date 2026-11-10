"""얼굴 찾기(성장 영상): 사진마다 얼굴 위치·눈 위치·얼굴 지문(임베딩)을 구한다.

OpenCV 의 두 모델을 쓴다(상업 이용이 되는 라이선스, OpenCV Zoo 공식 저장소에서 처음 쓸 때 받는다):
- YuNet(얼굴 찾기, MIT): face_detection_yunet_2023mar.onnx
- SFace(얼굴 알아보기, Apache 2.0): face_recognition_sface_2021dec.onnx
결과 {키: {"w", "h", "faces": [{"bbox": [x1, y1, x2, y2], "kps": [[x, y] x5], "det": 점수, "emb": [...]}]}}
kps 는 그림 기준 왼쪽 눈, 오른쪽 눈, 코, 왼쪽 입꼬리, 오른쪽 입꼬리. emb 는 길이 1 로 맞춘 지문(내적 = 코사인 닮음).
좌표는 EXIF·회전을 바로 세운 원본 크기 기준. 원본 사진은 읽기만 한다. 결과는 사진마다 캐시에 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

from common import T

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
MODELS = {   # 이름: (주소, sha256)
    "face_detection_yunet_2023mar.onnx": (ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
                                          "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "face_recognition_sface_2021dec.onnx": (ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
                                            "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}
SIG_MODEL = "opencv-yunet-sface"
DET_MAX = 1280      # 찾을 때 긴 쪽을 이 크기로 줄인다(좌표는 원본 크기로 되돌림)
MIN_SCORE = 0.6     # 이보다 확실하지 않은 얼굴은 버린다


class FaceError(RuntimeError):
    pass


def ensure_models(folder: Path) -> tuple[Path, Path]:
    """모델 파일(없으면 받는다). (찾기 모델, 알아보기 모델). 받은 파일은 sha256 으로 확인한다."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, (url, digest) in MODELS.items():
        path = folder / name
        if not path.exists():
            part = path.with_suffix(".part")
            try:
                with urllib.request.urlopen(url, timeout=120) as response, open(part, "wb") as f:
                    while chunk := response.read(1 << 20):
                        f.write(chunk)
            except OSError as exc:
                part.unlink(missing_ok=True)
                raise FaceError(T("model_download_failed", err=type(exc).__name__)) from None
            if hashlib.sha256(part.read_bytes()).hexdigest() != digest:
                part.unlink(missing_ok=True)
                raise FaceError(T("model_mismatch", name=name))
            part.replace(path)
        paths.append(path)
    return paths[0], paths[1]


def load_oriented(path: str, rotate: int):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        img = ImageOps.exif_transpose(im)
        deg = rotate % 360
        if deg == 90:
            img = img.transpose(Image.Transpose.ROTATE_270)
        elif deg == 180:
            img = img.transpose(Image.Transpose.ROTATE_180)
        elif deg == 270:
            img = img.transpose(Image.Transpose.ROTATE_90)
        return img.convert("RGB")


def find_faces(items: list[dict], cache_path: Path, models_dir: Path) -> dict:
    """items: [{"key", "path", "rotate"}]. 결과는 위 모양. 바뀌지 않은 사진은 캐시에서."""
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    todo, out = [], {}
    for it in items:
        p = Path(it["path"])
        try:
            sig = f"{p.stat().st_size}|{p.stat().st_mtime_ns}|{it.get('rotate', 0)}|{SIG_MODEL}"
        except OSError:
            continue
        c = cache.get(it["key"])
        if c and c.get("sig") == sig:
            out[it["key"]] = c
        else:
            todo.append((it, sig))
    if not todo:
        return out
    import cv2
    import numpy as np
    det_path, rec_path = ensure_models(models_dir)
    # 모델은 바이트로 읽어 넘긴다: Windows 의 OpenCV 는 한글·특수 문자가 든 경로의 파일을 열지 못한다
    empty = np.array([], np.uint8)
    detector = cv2.FaceDetectorYN.create("onnx", np.frombuffer(det_path.read_bytes(), np.uint8), empty, (320, 320), MIN_SCORE, 0.3, 5000)
    recognizer = cv2.FaceRecognizerSF.create("onnx", np.frombuffer(rec_path.read_bytes(), np.uint8), empty)
    for it, sig in todo:
        try:
            img = load_oriented(it["path"], int(it.get("rotate", 0)))
        except Exception as e:
            print(T("image_read_failed", path=it["path"], err=e), file=sys.stderr)
            continue
        w, h = img.size
        scale = min(1.0, DET_MAX / max(w, h))
        small = img.resize((round(w * scale), round(h * scale))) if scale < 1 else img
        bgr = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2BGR)
        detector.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, rows = detector.detect(bgr)
        faces = []
        for row in rows if rows is not None else []:
            x, y, fw, fh = (float(v) for v in row[:4])
            pts = [(float(row[4 + 2 * k]), float(row[5 + 2 * k])) for k in range(5)]   # 눈 둘, 코, 입꼬리 둘
            eyes = sorted(pts[0:2])
            mouth = sorted(pts[3:5])
            kps = [eyes[0], eyes[1], pts[2], mouth[0], mouth[1]]   # 그림 기준 왼쪽부터
            feature = recognizer.feature(recognizer.alignCrop(bgr, row)).flatten().astype(float)
            norm = float(np.linalg.norm(feature)) or 1.0
            faces.append({"bbox": [x / scale, y / scale, (x + fw) / scale, (y + fh) / scale],
                          "kps": [[px / scale, py / scale] for px, py in kps], "det": float(row[14]),
                          "emb": [float(v) / norm for v in feature]})
        rec = {"sig": sig, "w": w, "h": h, "faces": faces}
        cache[it["key"]] = rec
        out[it["key"]] = rec
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    return out
