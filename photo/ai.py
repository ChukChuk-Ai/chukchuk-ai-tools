"""구독(Claude Code CLI) 연동 AI 사진 분류, 상세 분석, 이야기 작성 모듈."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any
from PIL import Image, ImageOps

from cli import ask
from common import T, output_language
from sheet import make_thumbnail_sheets


def encode_image_to_base64(path: Path, max_dimension: int = 1568, quality: int = 85) -> str:
    """이미지를 EXIF 회전 보정 및 긴 쪽 리사이즈 후 base64 JPEG 문자열로 인코딩한다."""
    with Image.open(path) as img:
        transposed = ImageOps.exif_transpose(img)
        w, h = transposed.size
        if max(w, h) > max_dimension:
            scale = max_dimension / max(w, h)
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            transposed = transposed.resize((new_w, new_h), Image.Resampling.LANCZOS)

        if transposed.mode != "RGB":
            transposed = transposed.convert("RGB")

        buffer = io.BytesIO()
        transposed.save(buffer, format="JPEG", quality=quality)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")


def extract_json(text: str) -> Any:
    """텍스트에서 JSON 객체 또는 배열을 추출한다. 끝이 잘린 배열도 가능한 복구한다."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)(?:```|$)", text)
    if match:
        content = match.group(1).strip()
    else:
        content = text

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 앞뒤에 계획 글 같은 군글이 붙은 경우(agy): 각 '{'·'[' 에서 짝이 맞는 끝까지 읽어 가장 긴 것을 쓴다
    decoder, best, best_len = json.JSONDecoder(), None, 0
    for m in re.finditer(r"[\[{]", content):
        try:
            obj, end = decoder.raw_decode(content, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, (list, dict)) and end - m.start() > best_len:
            best, best_len = obj, end - m.start()
    if best is not None:
        return best

    # { ... } 형태 시도
    start_brace = content.find("{")
    end_brace = content.rfind("}")
    if start_brace != -1 and end_brace > start_brace:
        try:
            return json.loads(content[start_brace : end_brace + 1])
        except json.JSONDecodeError:
            pass

    # [ ... ] 형태 시도 (잘린 경우 마지막 유효 항목까지 복구)
    start_bracket = content.find("[")
    if start_bracket != -1:
        sub = content[start_bracket:]
        end_bracket = sub.rfind("]")
        if end_bracket != -1:
            try:
                return json.loads(sub[: end_bracket + 1])
            except json.JSONDecodeError:
                pass
        # 잘린 경우: 마지막 '}'를 찾아 ']'로 닫아서 복구 시도
        last_item_brace = sub.rfind("}")
        if last_item_brace != -1:
            repaired = sub[: last_item_brace + 1] + "]"
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    raise json.JSONDecodeError(T("json_failed"), content, 0)


class PhotoAnalyzer:
    def __init__(self, sort_model: str, story_model: str):
        self.sort_model = sort_model
        self.story_model = story_model
        self.usage: dict[str, dict[str, int]] = {
            self.sort_model: {"input_tokens": 0, "output_tokens": 0},
            self.story_model: {"input_tokens": 0, "output_tokens": 0},
        }

    def _ask(self, model: str, content: list[dict]) -> str:
        """작업 분야 모델로 부르고 답 글을 돌려준다. 토큰 수는 usage 에 쌓는다(sdk 는 0)."""
        text, tokens = ask(model, content)
        u = self.usage.setdefault(model, {"input_tokens": 0, "output_tokens": 0})
        u["input_tokens"] += tokens["input_tokens"]
        u["output_tokens"] += tokens["output_tokens"]
        return text

    @staticmethod
    def _image(b64: str) -> dict:
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}

    def classify_sheet(self, sheet_path: Path, items: list[dict]) -> dict[str, dict]:
        """썸네일 판 1장을 Haiku 모델로 분류한다 (effort low)."""
        b64 = encode_image_to_base64(sheet_path, max_dimension=1568, quality=85)
        item_labels = [f"#{it['index']} {it['label']} ({it['filename']})" for it in items]
        labels_str = ", ".join(item_labels)

        prompt = f"""이 이미지는 여러 사진의 썸네일을 격자(Grid)로 모아놓은 썸네일 판입니다.
각 칸 아래에는 '#번호 라벨 (파일명)'이 적혀 있습니다.
포함된 사진 목록:
{labels_str}

각 사진을 확인하고 아래 필드만 갖는 컴팩트한 JSON 배열로 답해 주세요:
[
  {{
    "index": 칸번호(정수),
    "filename": "정확한파일명",
    "people_count": 사람수(정수),
    "readable": true/false(인물 표정/모습 식별 가능 여부),
    "blurry_or_bad": true/false(흔들림/천장/바닥/실수 여부),
    "scene_keyword": "장면낱말(식사,케이크,준비,야외 등)",
    "place_guess": "장소짐작"
  }}, ...
]
반드시 유효한 JSON 배열만 출력하세요.
{output_language("장면낱말·장소짐작")}"""

        raw_text = self._ask(self.sort_model, [self._image(b64), {"type": "text", "text": prompt}])
        data = extract_json(raw_text)

        results: dict[str, dict] = {}
        if isinstance(data, list):
            for row in data:
                fn = row.get("filename")
                if not fn:
                    idx = row.get("index")
                    if idx and 1 <= idx <= len(items):
                        fn = items[idx - 1]["filename"]
                if fn:
                    results[fn] = {
                        "people_count": int(row.get("people_count", 0)),
                        "readable": bool(row.get("readable", False)),
                        "blurry_or_bad": bool(row.get("blurry_or_bad", False)),
                        "scene_keyword": str(row.get("scene_keyword", "")),
                        "place_guess": str(row.get("place_guess", "")),
                    }

        for it in items:
            fn = it["filename"]
            if fn not in results:
                results[fn] = {
                    "people_count": 0,
                    "readable": False,
                    "blurry_or_bad": False,
                    "scene_keyword": "",
                    "place_guess": "",
                }
        return results

    def detect_rotations(
        self,
        photos: list[dict],
        sheets_dir: Path | None = None,
        sheet_size: int = 40,
    ) -> dict[str, int]:
        """고른 사진들의 썸네일 판을 Sonnet(story_model)에 보내(Haiku 는 11/8 시험에서 9장 중 3장을 틀렸다) 시계방향 회전 각도(0, 90, 180, 270)를 판정한다."""
        if not photos:
            return {}

        out_dir = sheets_dir or (Path.cwd() / "sheets_rot_tmp")
        out_dir.mkdir(parents=True, exist_ok=True)

        sheets = make_thumbnail_sheets(
            photos,
            out_dir,
            sheet_size=9,   # 방향은 작은 칸에서 자주 틀려(11/8 시험: 40칸 판 7장 중 3장 틀림) 3×3 큰 칸으로 본다
            cols=3,
            rows=3,
            max_dimension=1568,
            prefix="rot_sheet",
        )

        rotations: dict[str, int] = {}
        valid_angles = {0, 90, 180, 270}

        for sh in sheets:
            b64 = encode_image_to_base64(sh["sheet_path"], max_dimension=1568, quality=85)
            item_labels = [f"#{it['index']} {it['label']} ({it['filename']})" for it in sh["items"]]
            labels_str = ", ".join(item_labels)

            prompt = f"""이 이미지는 이야기책에 사용할 사진들의 썸네일을 격자(Grid)로 모아놓은 썸네일 판입니다.
각 칸 아래에는 '#번호 라벨 (파일명)'이 적혀 있습니다.
포함된 사진 목록:
{labels_str}

옛날 디지털 카메라로 촬영되어 세로로 찍은 사진이 가로로 누워 있거나 방향이 잘못되어 있을 수 있습니다.
각 칸 사진을 바로 세우려면 시계방향(clockwise)으로 몇 도(0, 90, 180, 270) 돌려야 하나요?
- 0: 이미 정상 방향으로 바로 서 있음 (대부분의 정상 가로/세로 사진)
- 90: 시계방향으로 90도 돌려야 바로 섬 (사람 머리가 왼쪽으로 누워 있는 경우)
- 180: 180도 돌려야 바로 섬 (상하가 거꾸로 뒤집힌 경우 — 카메라 사진에서는 거의 없으니 확실할 때만)
- 270: 시계방향으로 270도 돌려야 바로 섬 (사람 머리가 오른쪽으로 누워 있는 경우)

판단 근거: 사람 얼굴·몸이 똑바로 서는 쪽, 천장·하늘이 위, 바닥·식탁이 아래, 글자가 바로 읽히는 쪽.
특히 카메라가 찍은 노란 날짜 글자(예: 2003/09/07)는 원래 가로 화면의 오른쪽 아래에 바로 읽히게 찍힌다 — 날짜 글자가 세로로 서 있으면 사진이 누워 있는 것이고, 날짜 글자가 바로 읽히면 0이다.
헷갈리면 0으로 답한다(잘못 돌리는 것보다 그대로 두는 것이 낫다).

반드시 아래 필드만 갖는 컴팩트한 JSON 배열로만 답해 주세요:
[
  {{
    "index": 칸번호(정수),
    "filename": "정확한파일명",
    "rotate": 0 또는 90 또는 180 또는 270
  }}, ...
]
반드시 유효한 JSON 배열만 출력하세요.
{output_language()}"""

            raw_text = self._ask(self.story_model, [self._image(b64), {"type": "text", "text": prompt}])
            try:
                data = extract_json(raw_text)
            except Exception:
                data = []

            if isinstance(data, list):
                for row in data:
                    fn = row.get("filename")
                    if not fn:
                        idx = row.get("index")
                        if idx and 1 <= idx <= len(sh["items"]):
                            fn = sh["items"][idx - 1]["filename"]
                    if fn:
                        try:
                            deg = int(row.get("rotate", 0))
                            if deg not in valid_angles:
                                deg = (deg % 360) if (deg % 360) in valid_angles else 0
                        except Exception:
                            deg = 0
                        rotations[fn] = deg

            for it in sh["items"]:
                fn = it["filename"]
                if fn not in rotations:
                    rotations[fn] = 0

        return rotations

    def select_photos_for_story(
        self,
        scenes: list[dict],
        classification: dict[str, dict],
        max_per_scene: int = 12,
        max_photos: int = 60,
    ) -> list[dict]:
        """판독 가능한 인물 사진 위주로 장면별, 전체 상한에 맞춰 고른다."""
        selected: list[dict] = []

        for sc in scenes:
            scene_photos = sc["photos"]
            candidates: list[dict] = []

            for p in scene_photos:
                info = classification.get(p["filename"], {})
                score = 0
                if info.get("readable"):
                    score += 10
                if info.get("people_count", 0) > 0:
                    score += 5
                if not info.get("blurry_or_bad"):
                    score += 3
                candidates.append((score, p))

            candidates.sort(key=lambda x: x[0], reverse=True)
            chosen_in_scene = [item[1] for item in candidates[:max_per_scene]]
            chosen_in_scene.sort(key=lambda x: x["timestamp"])

            for p in chosen_in_scene:
                if len(selected) < max_photos:
                    selected.append(p)

        return selected

    def analyze_scene_photos(
        self,
        scene_idx: int,
        scene_info: dict,
        photos: list[dict],
        existing_people: list[dict],
        folder_hint: str,
    ) -> tuple[list[dict], list[dict]]:
        """고른 사진들을 자세히 보고 인물 및 상황을 분석한다."""
        if not photos:
            return [], existing_people

        chunk_size = 6
        all_photo_analyses: list[dict] = []
        current_people = list(existing_people)

        for i in range(0, len(photos), chunk_size):
            chunk = photos[i : i + chunk_size]
            content: list[dict] = []

            for p in chunk:
                b64 = encode_image_to_base64(p["path"], max_dimension=1568, quality=85)
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": b64,
                    },
                })

            prompt_people = json.dumps(current_people, ensure_ascii=False, indent=2) if current_people else "아직 등록된 인물 없음"
            photos_meta = ", ".join([f"{p['filename']}(촬영:{p['time_str']})" for p in chunk])

            prompt = f"""당신은 가족 사진 이야기책을 만드는 전문 사진 분석가입니다.
이 사진들은 제 {scene_idx}번째 장면({scene_info.get('start_time_str')} ~ {scene_info.get('end_time_str')})의 고른 사진들입니다.
폴더 힌트: {folder_hint} (폴더 이름에 행사나 주인공 이름이 들어 있으면 그 사람을 주인공으로 보고 그 이름을 호칭으로 쓰세요)

지금까지 파악된 인물 목록(people.json):
{prompt_people}

이번 사진 목록:
{photos_meta}

[분석 지침]
1. 같은 인물은 위 기존 인물 목록의 호칭을 그대로 사용하세요.
2. 새 인물이 나타나면 옷차림이나 외모를 이름으로 삼지 마세요('분홍 티셔츠 할머니', '안경 쓴 여성' 등 금지). 사진의 주인공(폴더 이름에 나온 사람이나 가장 많이 나오는 사람)과의 관계를 추정하여 가족·친구 호칭(아빠, 엄마, 할머니, 할아버지, 형, 누나, 이모, 고모, 삼촌, 사촌, 친구 등)을 우선 붙이세요.
3. 확실하지 않은 관계는 반드시 '(추정)'을 붙이세요(예: 엄마(추정), 할머니(추정), 사촌 형(추정)).
4. 둘 이상 같은 호칭이면 '할머니1, 할머니2'가 아니라 '친할머니(추정), 외할머니(추정)'나 특징을 짧게 붙여 구별하세요.
5. 인물의 시각적 특징(옷차림, 머리 스타일, 안경, 장신구 등)은 이름이 아니라 'features'에 자세히 기록하세요.
6. 사진 속에 보이는 실제 인물들의 행동, 표정, 상황을 객관적 사실 위주로 설명하세요. 사진에 없는 사실을 지어내지 마세요.

응답은 반드시 유효한 JSON 객체로 출력하세요:
{{
  "photos": [
    {{
      "filename": "파일명",
      "people_present": ["호칭1", "호칭2"],
      "action_and_mood": "이 사진에서 일어난 일, 인물들의 행동과 표정(1~2문장)"
    }}
  ],
  "new_or_updated_people": [
    {{
      "name": "호칭 (예: 주인공 이름, 엄마(추정), 친할머니(추정), 사촌 누나(추정) 등)",
      "features": "인물의 구체적인 시각적 특징 (옷차림, 머리 스타일, 외모 등)",
      "first_seen": "처음 등장한 파일명"
    }}
  ]
}}
{output_language()}"""

            content.append({"type": "text", "text": prompt})

            raw_text = self._ask(self.story_model, content)
            parsed = extract_json(raw_text)

            p_list = parsed.get("photos", []) if isinstance(parsed, dict) else []
            all_photo_analyses.extend(p_list)

            new_people = parsed.get("new_or_updated_people", []) if isinstance(parsed, dict) else []
            for np in new_people:
                name = np.get("name")
                if not name:
                    continue
                found = False
                for ep in current_people:
                    if ep.get("name") == name:
                        if np.get("features"):
                            existing_feat = ep.get("features", "")
                            new_feat = np["features"]
                            if new_feat not in existing_feat:
                                ep["features"] = f"{existing_feat}; {new_feat}".strip("; ")
                        found = True
                        break
                if not found:
                    current_people.append({
                        "name": name,
                        "features": np.get("features", ""),
                        "first_seen": np.get("first_seen", chunk[0]["filename"] if chunk else ""),
                    })

        return all_photo_analyses, current_people

    def regroup_people(
        self,
        folder_name: str,
        people: list[dict],
        scenes: list[dict],
        photo_analyses_by_scene: dict[int, list[dict]],
        preserved_names: set[str] | None = None,
    ) -> tuple[dict[str, str], list[dict]]:
        """story_model에 글만 보내 인물 목록을 가족 호칭 중심으로 정리하고 중복을 통합한다."""
        preserved = preserved_names or set()

        appearances: dict[str, list[dict]] = {}
        for sc_idx, analyses in photo_analyses_by_scene.items():
            for an in analyses:
                for p in an.get("people_present", []):
                    appearances.setdefault(p, []).append({
                        "filename": an.get("filename", ""),
                        "action": an.get("action_and_mood", ""),
                    })

        summary_people = []
        for p in people:
            name = p.get("name", "")
            apps = appearances.get(name, [])
            app_count = len(apps)
            sample_actions = [f"[{a['filename']}] {a['action']}" for a in apps[:4]]
            summary_people.append({
                "name": name,
                "current_features": p.get("features", ""),
                "first_seen": p.get("first_seen", ""),
                "photo_count": app_count,
                "sample_appearances": sample_actions,
                "is_preserved": name in preserved,
            })

        prompt = f"""당신은 가족 사진 이야기책을 위해 등장인물 정보를 체계적으로 정리하는 전문 편집자입니다.
이번 사진 모음 폴더명: {folder_name} (폴더 이름에 행사나 주인공 이름이 들어 있으면 그 사람이 주인공입니다.)

지금까지 상세 판독을 통해 등록된 인물 목록과 각 인물의 등장 정보입니다:
{json.dumps(summary_people, ensure_ascii=False, indent=2)}

사용자가 수동으로 지정하여 절대 이름을 바꾸면 안 되는 인물(preserved_names):
{list(preserved) if preserved else "없음"}

[인물 정리 지침]
1. 옷차림 이름 대신 가족 호칭 붙이기:
   - '분홍 티셔츠 할머니(추정)', '검정 꽃무늬 할머니(추정)', '안경 쓴 여성(추정)', '넥타이 맨 남성(추정)' 같은 옷차림/외모 이름은 사용하지 마세요.
   - 주인공을 기준으로 가족·친구 호칭(아빠, 엄마, 친할머니, 외할머니, 할아버지, 삼촌, 이모, 고모, 사촌 형, 사촌 누나, 친구 등)을 추정해 부여하세요.
   - 주인공이 아이라면 아이를 안고 돌보거나 함께 있는 젊은 남녀는 아빠·엄마(추정), 나이 든 여성은 할머니(추정)일 수 있습니다.
   - 근거가 약하면 반드시 '(추정)'을 붙이세요.
   - 같은 호칭이 둘 이상이면 '할머니1·할머니2'가 아니라 '친할머니(추정)·외할머니(추정)'나 특징을 짧게 붙여 구별하세요.
2. 같은 사람 합치기 (대응표 name_mapping 생성):
   - 같은 사람이 옷을 갈아입거나 다른 이름으로 분리된 경우 하나로 합치세요.
     예: '하늘'과 '하늘(추정)' -> '하늘'
     예: '줄무늬 티셔츠 아기'와 '검정 줄무늬 민소매 남자아이' 등 동일 인물로 추정되는 경우 통합
3. 특징(features) 하나로 합치기:
   - 세미콜론(';')으로 두 번 이어 붙었거나 중복된 설명들을 깔끔하고 자연스러운 한두 문장으로 합치세요.
4. 소수 등장 인물(사진 2장 이하) 묶기:
   - 주인공이나 핵심 가족(아빠, 엄마, 할머니 등)을 제외하고, 사진에 2장 이하로 잠깐 등장한 인물(배경 하객, 잠깐 찍힌 친척 아이 등)은 '손님들' 한 줄로 묶어 정리하세요.
   - '손님들'의 features에는 그 자리에 함께한 손님·친지들임을 한 문장으로 요약하세요.
5. preserved_names에 포함된 이름은 이름(name)을 변경하지 마세요.

반드시 유효한 JSON 객체로 출력하세요:
{{
  "name_mapping": {{
    "기존이름1": "새이름",
    "기존이름2": "새이름"
  }},
  "regrouped_people": [
    {{
      "name": "새이름",
      "features": "인물의 구체적 시각적 특징과 역할 (한두 문장)",
      "first_seen": "처음 등장한 파일명"
    }}
  ]
}}
{output_language()}"""

        raw_text = self._ask(self.story_model, [{"type": "text", "text": prompt}])
        try:
            parsed = extract_json(raw_text)
            name_mapping = parsed.get("name_mapping", {}) if isinstance(parsed, dict) else {}
            regrouped = parsed.get("regrouped_people", []) if isinstance(parsed, dict) else []
            # preserved_names 보호
            for p_name in preserved:
                if p_name in name_mapping:
                    del name_mapping[p_name]
            return name_mapping, regrouped
        except Exception:
            return {}, people

    def write_story_texts(
        self,
        folder_name: str,
        scenes: list[dict],
        photo_analyses_by_scene: dict[int, list[dict]],
        people: list[dict],
    ) -> dict[int, dict]:
        """장면별 분석과 인물 목록을 종합하여 장면마다 3~5문장의 이야기를 한국어로 작성한다."""
        scenes_summary = []
        for sc in scenes:
            idx = sc["scene_idx"]
            pa = photo_analyses_by_scene.get(idx, [])
            p_desc = [f"- {p.get('filename')}: 인물({', '.join(p.get('people_present', []))}) / 행동:{p.get('action_and_mood', '')}" for p in pa]
            scenes_summary.append({
                "scene_idx": idx,
                "time": f"{sc.get('start_time_str')} ~ {sc.get('end_time_str')}",
                "photo_count": len(sc.get("photos", [])),
                "details": p_desc,
            })

        prompt = f"""당신은 따뜻하고 진솔한 가족 사진 이야기책 작가입니다.
폴더: {folder_name}
등장인물: {json.dumps(people, ensure_ascii=False, indent=2)}

각 장면별 시간과 사진 분석 내용:
{json.dumps(scenes_summary, ensure_ascii=False, indent=2)}

[이야기 작성 지침]
1. 각 장면(scene)마다 3~5문장으로 그 시간대에 일어난 일을 이야기로 서술하세요.
2. 보이는 것과 짐작을 자연스럽게 구분하고, 사진에 없는 허구의 사실을 꾸며내지 마세요.
3. 등장인물 호칭은 위 등장인물 목록의 호칭을 일관되게 사용하세요.
4. 문체는 정중하고 따뜻한 해요체(또는 평서체)의 한국어로 작성하세요.

반드시 유효한 JSON 객체로 답해 주세요:
{{
  "stories": [
    {{
      "scene_idx": 1,
      "title": "장면 소제목 (예: 여행 첫날의 분주한 아침)",
      "story": "3~5문장의 따뜻한 이야기 글..."
    }}
  ]
}}
{output_language()}"""

        raw_text = self._ask(self.story_model, [{"type": "text", "text": prompt}])
        parsed = extract_json(raw_text)

        result_stories: dict[int, dict] = {}
        if isinstance(parsed, dict) and "stories" in parsed:
            for s in parsed["stories"]:
                s_idx = s.get("scene_idx")
                if s_idx:
                    result_stories[s_idx] = {
                        "title": s.get("title", T("scene_n", n=s_idx)),
                        "text": s.get("story", ""),
                    }
        return result_stories


def apply_name_mapping(
    name_mapping: dict[str, str],
    regrouped_people: list[dict],
    photo_analyses_by_scene: dict[int, list[dict]],
    preserved_people: list[dict] | None = None,
) -> tuple[list[dict], dict[int, list[dict]]]:
    """이름 대응표(name_mapping)를 사진별 인물 등장 기록과 인물 목록에 적용한다.
    preserved_people(사용자가 고친 이름의 인물)은 AI 가 빼거나 바꿔도 그대로 되살리고 manual=True 로 남긴다.

    1. 사진별 등장 인물(people_present)을 새 이름으로 치환하고 중복을 제거한다. 설명 글(action_and_mood)의 옛 호칭도 함께 바꾼다.
    2. 인물 목록(regrouped_people)의 특징이 비어있거나 중복된 항목을 정리한다.
    """
    updated_analyses: dict[int, list[dict]] = {}
    text_map = text_name_mapping(name_mapping)
    for sc_idx, analyses in photo_analyses_by_scene.items():
        new_list = []
        for an in analyses:
            item = dict(an)
            old_peeps = item.get("people_present", [])
            new_peeps = []
            for p in old_peeps:
                mapped = name_mapping.get(p, p)
                if mapped and mapped not in new_peeps:
                    new_peeps.append(mapped)
            item["people_present"] = new_peeps
            item["action_and_mood"] = rewrite_names_in_text(item.get("action_and_mood", ""), text_map)
            new_list.append(item)
        updated_analyses[sc_idx] = new_list

    final_people = []
    seen_names = set()
    for rp in regrouped_people:
        name = rp.get("name", "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        final_people.append({
            "name": name,
            "features": rp.get("features", "").strip(),
            "first_seen": rp.get("first_seen", ""),
        })

    for keep in preserved_people or []:
        name = keep.get("name", "")
        if not name:
            continue
        for fp in final_people:
            if fp["name"] == name:
                fp["manual"] = True
                if not fp.get("features"):
                    fp["features"] = keep.get("features", "")
                break
        else:
            final_people.append({"name": name, "features": keep.get("features", ""),
                                 "first_seen": keep.get("first_seen", ""), "manual": True})

    return final_people, updated_analyses



_JOSA_PAIRS = {"이": ("이", "가"), "가": ("이", "가"), "은": ("은", "는"), "는": ("은", "는"),
               "을": ("을", "를"), "를": ("을", "를"), "와": ("과", "와"), "과": ("과", "와")}


def _has_batchim(name: str) -> bool | None:
    for ch in reversed(re.sub(r"\([^)]*\)", "", name)):  # '아빠(추정)' 은 '빠'로 본다
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28 != 0
    return None


def rewrite_names_in_text(text: str, mapping: dict[str, str]) -> str:
    """설명 글 속의 옛 이름을 새 이름으로 바꾼다(AI 없음). 뒤의 조사(이/가, 은/는, 을/를, 과/와)는 새 이름에 맞춘다.
    긴 이름부터 바꾸고, 한글 낱말 가운데는 건드리지 않는다."""
    if not text:
        return text
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        if not old or old == new:
            continue
        pattern = re.compile(
            r"(?<![가-힣])" + re.escape(old) + r"(?P<j>이|가|은|는|을|를|와|과)?"
            r"(?=(?:의|에게|에서|에|랑|도|만|께|한테|으로|로)?(?![가-힣]))"
        )

        def repl(m: re.Match, new: str = new) -> str:
            j = m.group("j") or ""
            if j:
                bt = _has_batchim(new)
                if bt is not None:
                    j = _JOSA_PAIRS[j][0 if bt else 1]
            return new + j

        text = pattern.sub(repl, text)
    return text


def rewrite_analysis_texts(analysis_data: dict, mapping: dict[str, str]) -> int:
    """analysis.json 의 사진별 설명(action_and_mood), 인물 특징(features)에서 옛 이름을 바꾼다. 바뀐 글 수를 돌려준다."""
    changed = 0
    targets = [(a, "action_and_mood") for sc in analysis_data.get("scenes", []) for a in sc.get("analyses", [])]
    targets += [(p, "features") for p in analysis_data.get("people", [])]
    for obj, key in targets:
        old = obj.get(key, "")
        new = rewrite_names_in_text(old, mapping)
        if new != old:
            obj[key] = new
            changed += 1
    return changed


def text_name_mapping(name_mapping: dict[str, str]) -> dict[str, str]:
    """인물 정리 대응표 중 설명 글에 쓰는 것만 고른다: 호칭 짐작 '○○(추정)'(영어는 '○○ (guess)') 이름. 옷차림 묘사 같은 낱말은 글에서 건드리지 않는다."""
    return {k: v for k, v in name_mapping.items() if k.endswith(("(추정)", "(guess)")) and v and k != v}
