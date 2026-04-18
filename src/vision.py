"""Scene content analyzer using Gemini Vision.

Each Clip is a sub-scene of a source file (path + start + end). We pull a few
frames from inside that scene, send them to Gemini, and cache the returned
description + retention scoring. With scene-level granularity, a 9-minute
source produces many candidates instead of one.
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
from .plan import Clip


@dataclass
class ClipAnalysis:
    path: str  # Clip.id (file#start-end)
    source_file: str = ""
    start: float = 0.0
    end: float = 0.0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    visual_impact: int = 5
    retention_value: int = 5
    emotion: str = "neutral"
    suitability: dict[str, int] = field(default_factory=dict)
    usable: bool = True
    clip_type: str = "content"
    skip_reason: str = ""


ANALYSIS_PROMPT = """이 영상 씬(scene)의 프레임 여러 장을 보고 JSON으로만 답해줘. 한국어로.

너는 짜집기 쇼츠 편집자가 인서트로 쓸지 말지 판단하는 중이다. 시청자가 이 화면을 보고 다음 컷까지 멈추지 않을지가 핵심.

**1단계: usable 판정 — 매우 보수적**
기본값 usable=true. 다음일 때만 false:
- 화면 80%+가 채널 로고/프로필 정적 이미지
- "구독해주세요" 안내만 있고 영상 콘텐츠 없음
- 검은/단색 화면, 화면 전환 효과만
- 모든 프레임이 동일한 정지 이미지

usable=true 유지:
- 작은 워터마크/외국어 자막 있어도 콘텐츠가 있으면 OK
- 손/도구만 보여도 사물·행동이 있으면 OK

**2단계: 콘텐츠 씬이면 풍부하게 분석**

특히 retention_value (시청자 유지력)는:
- 10: 즉각 시선 강탈 (충격, 클로즈업, 극적 변화, 얼굴 표정)
- 7-9: 명확한 행동/사건 진행 중 (요리, 손 움직임, 결과물 등장)
- 4-6: 정보는 있지만 정적 (배경, 풀샷, 평범한 일상)
- 1-3: 지루함, 의미 모호, 시청자가 떠날 위험

{
  "usable": true 또는 false,
  "clip_type": "content | outro | intro | logo | watermark | transition_only | thumbnail | other",
  "skip_reason": "usable=false인 경우만 짧게 사유. true면 빈 문자열",
  "description": "이 씬에서 실제로 보이는 것 2-3문장. 인물(성별/행동), 등장 사물, 배경, 분위기 포함",
  "tags": ["구체적 키워드 5-8개 — 사물명/행동/상태"],
  "visual_impact": 1-10 (시각 자극도),
  "retention_value": 1-10 (시청자가 멈추고 보게 되는 정도),
  "emotion": "shock | warning | calm | clean | disgusting | action | mundane",
  "suitability": {
    "hook": 0-10,
    "problem": 0-10,
    "solution": 0-10,
    "cta": 0-10
  }
}

다른 설명 없이 JSON만 출력."""


def _extract_scene_frames(clip: Clip, count: int = 3) -> list[Path]:
    """Pull `count` evenly-spaced frames from inside the scene's time range."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="sceneframes_"))
    frames: list[Path] = []
    duration = max(0.5, clip.duration)
    for i in range(count):
        offset = duration * (i + 1) / (count + 1)
        t = clip.start + offset
        out = tmp_dir / f"frame_{i}.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
             "-i", str(clip.path), "-frames:v", "1", "-q:v", "4", str(out)],
            check=True,
        )
        frames.append(out)
    return frames


def _clip_signature(clip_path: Path) -> str:
    stat = clip_path.stat()
    raw = f"{clip_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
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


def analyze_scene(clip: Clip, client: genai.Client) -> ClipAnalysis:
    frames = _extract_scene_frames(clip, count=3)
    parts: list = [ANALYSIS_PROMPT]
    for f in frames:
        parts.append(types.Part.from_bytes(data=f.read_bytes(), mime_type="image/jpeg"))
    resp = _generate_with_retry(client, GEMINI_VISION_MODEL, parts)
    data = _parse_json(resp.text)
    for f in frames:
        f.unlink(missing_ok=True)
    return ClipAnalysis(
        path=clip.id,
        source_file=str(clip.path),
        start=clip.start,
        end=clip.end,
        description=str(data.get("description", "")),
        tags=[str(t) for t in data.get("tags", [])],
        visual_impact=int(data.get("visual_impact", 5)),
        retention_value=int(data.get("retention_value", 5)),
        emotion=str(data.get("emotion", "neutral")),
        suitability={k: int(v) for k, v in (data.get("suitability") or {}).items()},
        usable=bool(data.get("usable", True)),
        clip_type=str(data.get("clip_type", "content")),
        skip_reason=str(data.get("skip_reason", "")),
    )


def build_clips_index(clips: list[Clip], cache_path: Path) -> dict[str, ClipAnalysis]:
    """Analyze each scene. Cached by source file signature + scene range."""
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
    prompt_version = "v5"

    # Group scenes by file so we only signature-hash each file once.
    sigs: dict[Path, str] = {}
    for c in clips:
        if c.path not in sigs:
            sigs[c.path] = _clip_signature(c.path)

    for i, clip in enumerate(clips):
        sig = sigs[clip.path]
        key = f"{prompt_version}:{clip.path.name}:{sig[:12]}:{clip.start:.2f}-{clip.end:.2f}"
        if key in cache:
            result[clip.id] = ClipAnalysis(**cache[key])
            continue
        print(f"  [{i + 1}/{len(clips)}] Analyzing {clip.path.name} {clip.start:.1f}-{clip.end:.1f}s...")
        analysis = analyze_scene(clip, client)
        result[clip.id] = analysis
        cache[key] = asdict(analysis)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    return result
