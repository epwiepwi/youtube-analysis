"""Clip content analyzer using Gemini Vision.

For each clip: extract a few representative frames with ffmpeg, send them to
Gemini, and cache the returned description + tags + visual_impact score so we
only pay for each clip once.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .config import GEMINI_API_KEY, GEMINI_VISION_MODEL


@dataclass
class ClipAnalysis:
    path: str
    description: str
    tags: list[str] = field(default_factory=list)
    visual_impact: int = 5
    emotion: str = "neutral"
    suitability: dict[str, int] = field(default_factory=dict)
    usable: bool = True
    clip_type: str = "content"
    skip_reason: str = ""


ANALYSIS_PROMPT = """이 영상 클립의 프레임 여러 장을 보고 JSON으로만 답해줘. 한국어로 작성.

**1단계: 이 클립이 실제 콘텐츠인지 판단**
다음 중 하나면 usable=false:
- 크리에이터 로고/프로필 사진만 있음 (아웃트로/인트로)
- "구독해주세요" 같은 텍스트만 있고 콘텐츠 없음
- 워터마크/채널 마크가 화면을 거의 덮음
- 검은 화면/색만 있는 전환구간
- 썸네일 화면 (정지된 그림 한 장)

**2단계: 콘텐츠 클립인 경우만 채워**

{
  "usable": true 또는 false,
  "clip_type": "content | outro | intro | logo | watermark | transition_only | thumbnail | other",
  "skip_reason": "usable=false인 경우만 짧게 사유. true면 빈 문자열",
  "description": "이 클립에서 실제로 보이는 것 2-3문장. 인물(성별/행동), 등장 사물, 배경, 분위기 포함. usable=false면 짧게.",
  "tags": ["구체적 키워드 5-8개 — 사물명, 행동, 상태. 예: '양파', '곰팡이', '주방', '썰기'. usable=false면 빈 배열"],
  "visual_impact": 1-10 (시각적 충격/자극도, usable=false면 0),
  "emotion": "shock | warning | calm | clean | disgusting | action | mundane 중 하나, usable=false면 'none'",
  "suitability": {
    "hook": 0-10 (영상 맨 앞 2초 훅),
    "problem": 0-10 (문제/경고 장면),
    "solution": 0-10 (해결책/결과 장면),
    "cta": 0-10 (마무리/CTA)
  }
}

다른 설명 없이 JSON만 출력."""


def _extract_frames(clip_path: Path, count: int = 3) -> list[Path]:
    duration = _probe_duration(clip_path)
    tmp_dir = Path(tempfile.mkdtemp(prefix="clipframes_"))
    frames: list[Path] = []
    for i in range(count):
        t = duration * (i + 1) / (count + 1)
        out = tmp_dir / f"frame_{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
             "-i", str(clip_path), "-frames:v", "1", "-q:v", "4", str(out)],
            check=True,
        )
        frames.append(out)
    return frames


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True,
    )
    return float(out.strip())


def _clip_signature(clip_path: Path) -> str:
    stat = clip_path.stat()
    raw = f"{clip_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in response: {text[:200]}")
    return json.loads(text[start : end + 1])


def _generate_with_retry(client: genai.Client, model: str, contents, max_attempts: int = 5):
    delay = 4.0
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except (genai_errors.ServerError, genai_errors.APIError) as e:
            code = getattr(e, "code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                print(f"    [{code}] Gemini busy, retry {attempt}/{max_attempts} in {delay:.0f}s...")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise


def analyze_clip(clip_path: Path, client: genai.Client) -> ClipAnalysis:
    frames = _extract_frames(clip_path, count=3)
    parts: list = [ANALYSIS_PROMPT]
    for f in frames:
        parts.append(types.Part.from_bytes(data=f.read_bytes(), mime_type="image/jpeg"))
    resp = _generate_with_retry(client, GEMINI_VISION_MODEL, parts)
    data = _parse_json(resp.text)
    for f in frames:
        f.unlink(missing_ok=True)
    return ClipAnalysis(
        path=str(clip_path),
        description=str(data.get("description", "")),
        tags=[str(t) for t in data.get("tags", [])],
        visual_impact=int(data.get("visual_impact", 5)),
        emotion=str(data.get("emotion", "neutral")),
        suitability={k: int(v) for k, v in (data.get("suitability") or {}).items()},
        usable=bool(data.get("usable", True)),
        clip_type=str(data.get("clip_type", "content")),
        skip_reason=str(data.get("skip_reason", "")),
    )


def build_clips_index(clip_paths: list[Path], cache_path: Path) -> dict[str, ClipAnalysis]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set. Create .env with GEMINI_API_KEY=... or skip semantic matching.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    result: dict[str, ClipAnalysis] = {}
    dirty = False
    prompt_version = "v3"
    for cp in clip_paths:
        sig = _clip_signature(cp)
        key = f"{prompt_version}:{cp.name}:{sig[:12]}"
        if key in cache:
            result[str(cp)] = ClipAnalysis(**cache[key])
            continue
        print(f"  Analyzing {cp.name}...")
        analysis = analyze_clip(cp, client)
        result[str(cp)] = analysis
        cache[key] = asdict(analysis)
        dirty = True
        # Persist after every successful clip so a later failure doesn't lose work.
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    if dirty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
