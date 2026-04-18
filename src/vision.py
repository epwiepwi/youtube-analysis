"""Scene content analyzer using Gemini Vision (video input).

Each Clip is a sub-scene of a source file (path + start + end). We cut a small
compressed video clip for the scene's range and send it to Gemini directly so
the model sees motion/timing, not just three still frames. Scenes are analyzed
in parallel to keep wall-clock time low.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .config import GEMINI_API_KEY, GEMINI_VISION_MODEL
from .plan import Clip


# Concurrency for vision analysis. Free tier is ~15 RPM so default 10 leaves
# headroom; the retry wrapper handles 429s anyway.
ANALYSIS_CONCURRENCY = int(os.environ.get("VISION_CONCURRENCY", "10"))
# How many scenes to send together in one Gemini call. Batching trades a
# slightly larger prompt for far fewer round-trips and lets the model
# compare scenes relative to each other, which improves retention/impact
# calibration.
ANALYSIS_BATCH_SIZE = int(os.environ.get("VISION_BATCH_SIZE", "6"))


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


ANALYSIS_PROMPT = """이 영상 씬(scene)을 보고 JSON으로만 답해줘. 한국어로.

너는 짜집기 쇼츠 편집자가 인서트로 쓸지 말지 판단하는 중이다. 시청자가 이 화면을 보고 다음 컷까지 멈추지 않을지가 핵심.

영상의 동작/변화/속도/감정까지 모두 관찰해서 답해라 (정지화면이 아니라 실제 움직이는 영상이다).

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

description에는 동작도 적어라 ("여자가 손을 꾹꾹 눌러 김치를 통에 담는다", "양념이 점점 진해지면서 휘저어진다" 같이).

{
  "usable": true 또는 false,
  "clip_type": "content | outro | intro | logo | watermark | transition_only | thumbnail | other",
  "skip_reason": "usable=false인 경우만 짧게 사유. true면 빈 문자열",
  "description": "이 씬에서 일어나는 동작/사건 2-3문장. 인물(성별/행동), 등장 사물, 변화, 분위기 포함",
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


def _extract_scene_clip(clip: Clip) -> Path:
    """Cut a compressed mp4 of the scene (no audio, 480p, 15fps) for inline upload."""
    tmp = Path(tempfile.mkdtemp(prefix="sceneclip_"))
    out = tmp / f"{clip.path.stem}_{clip.start:.1f}_{clip.end:.1f}.mp4"
    duration = max(0.5, clip.duration)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{clip.start:.2f}",
            "-i", str(clip.path),
            "-t", f"{duration:.2f}",
            "-vf", "scale='min(480,iw)':-2,fps=15",
            "-an",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "30",
            "-movflags", "+faststart",
            str(out),
        ],
        check=True,
    )
    return out


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


def _safe_int(value, default: int = 5) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_analysis(data: dict, clip: Clip) -> ClipAnalysis:
    return ClipAnalysis(
        path=clip.id,
        source_file=str(clip.path),
        start=clip.start,
        end=clip.end,
        description=str(data.get("description") or ""),
        tags=[str(t) for t in (data.get("tags") or [])],
        visual_impact=_safe_int(data.get("visual_impact"), 5),
        retention_value=_safe_int(data.get("retention_value"), 5),
        emotion=str(data.get("emotion") or "neutral"),
        suitability={k: _safe_int(v, 0) for k, v in (data.get("suitability") or {}).items()},
        usable=bool(data.get("usable", True)) if data.get("usable") is not None else True,
        clip_type=str(data.get("clip_type") or "content"),
        skip_reason=str(data.get("skip_reason") or ""),
    )


def analyze_scene(clip: Clip, client: genai.Client) -> ClipAnalysis:
    """Send one scene as video. Kept for single-clip fallbacks."""
    video_path = _extract_scene_clip(clip)
    try:
        parts: list = [
            ANALYSIS_PROMPT,
            types.Part.from_bytes(data=video_path.read_bytes(), mime_type="video/mp4"),
        ]
        resp = _generate_with_retry(client, GEMINI_VISION_MODEL, parts)
        data = _parse_json(resp.text)
    finally:
        try:
            video_path.unlink(missing_ok=True)
            video_path.parent.rmdir()
        except OSError:
            pass
    return _build_analysis(data, clip)


BATCH_PROMPT_HEADER = """아래 영상 씬 여러 개를 한 번에 분석한다. 각 씬에 대해 아래 스키마를 정확히 따르는 JSON을 배열로 돌려준다.

영상은 순서대로 [S0], [S1], [S2], ... 로 첨부된다. 각 영상은 1~8초 분량의 실제 움직이는 영상이며 동작/변화/감정을 모두 관찰해서 판단할 것.

각 씬 기본값 usable=true. 다음일 때만 false:
- 화면 80%+가 채널 로고/프로필 정적 이미지
- 구독안내만 있고 콘텐츠 없음
- 검은/단색 화면, 전환 효과만
- 모든 프레임 동일한 정지 이미지

retention_value (시청자 유지력):
- 10: 즉각 시선 강탈 (충격, 클로즈업, 극적 변화)
- 7-9: 명확한 행동/사건 진행 중
- 4-6: 정보는 있지만 정적
- 1-3: 지루함, 시청자가 떠날 위험

description은 동작까지 포함 ("손이 누름판을 꾹 누르는 중" 같이).
여러 씬을 비교하면서 retention/impact를 상대적으로 일관성 있게 매겨라.

[스키마 — 각 씬]
{
  "index": 0,  // [S0]을 가리키는 인덱스
  "usable": true/false,
  "clip_type": "content | outro | intro | logo | watermark | transition_only | thumbnail | other",
  "skip_reason": "",
  "description": "2-3문장",
  "tags": ["키워드 5-8개"],
  "visual_impact": 1-10,
  "retention_value": 1-10,
  "emotion": "shock | warning | calm | clean | disgusting | action | mundane",
  "suitability": {"hook": 0-10, "problem": 0-10, "solution": 0-10, "cta": 0-10}
}

[출력]
{"scenes": [위 스키마 * N]}
JSON만 출력. 다른 설명 금지. 반드시 배열 순서가 [S0][S1]... 순서와 같아야 함."""


def analyze_batch(clips: list[Clip], client: genai.Client) -> list[ClipAnalysis]:
    """Send up to ANALYSIS_BATCH_SIZE scenes in one Gemini call."""
    if not clips:
        return []
    parts: list = [BATCH_PROMPT_HEADER]
    video_paths: list[Path] = []
    try:
        for i, clip in enumerate(clips):
            video_path = _extract_scene_clip(clip)
            video_paths.append(video_path)
            parts.append(f"\n[S{i}] {clip.path.name} {clip.start:.1f}-{clip.end:.1f}s:")
            parts.append(types.Part.from_bytes(
                data=video_path.read_bytes(), mime_type="video/mp4"
            ))

        resp = _generate_with_retry(client, GEMINI_VISION_MODEL, parts)
        data = _parse_json(resp.text)
    finally:
        for vp in video_paths:
            try:
                vp.unlink(missing_ok=True)
                vp.parent.rmdir()
            except OSError:
                pass

    scenes_data = data.get("scenes", [])
    # Key by explicit index if given, else fall back to positional order.
    by_index: dict[int, dict] = {}
    for i, entry in enumerate(scenes_data):
        idx = _safe_int(entry.get("index", i), i)
        by_index[idx] = entry

    out: list[ClipAnalysis] = []
    for i, clip in enumerate(clips):
        entry = by_index.get(i) or (scenes_data[i] if i < len(scenes_data) else {})
        out.append(_build_analysis(entry or {}, clip))
    return out


def build_clips_index(clips: list[Clip], cache_path: Path) -> dict[str, ClipAnalysis]:
    """Analyze each scene in parallel via video input. Cached by file sig + range."""
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
    prompt_version = "v7_batch"

    # Group scenes by file so we only signature-hash each file once.
    sigs: dict[Path, str] = {}
    for c in clips:
        if c.path not in sigs:
            sigs[c.path] = _clip_signature(c.path)

    def cache_key_for(clip: Clip) -> str:
        sig = sigs[clip.path]
        return f"{prompt_version}:{clip.path.name}:{sig[:12]}:{clip.start:.2f}-{clip.end:.2f}"

    pending: list[Clip] = []
    for clip in clips:
        key = cache_key_for(clip)
        if key in cache:
            result[clip.id] = ClipAnalysis(**cache[key])
            continue
        pending.append(clip)

    if not pending:
        return result

    # Group pending into batches of ANALYSIS_BATCH_SIZE, preferring same-file
    # clips adjacent so one batch talks about consistent footage.
    pending.sort(key=lambda c: (str(c.path), c.start))
    batches: list[list[Clip]] = [
        pending[i:i + ANALYSIS_BATCH_SIZE]
        for i in range(0, len(pending), ANALYSIS_BATCH_SIZE)
    ]

    total = len(pending)
    done_count = 0
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_lock = __import__("threading").Lock()

    def run_batch(batch: list[Clip]) -> tuple[list[Clip], list[ClipAnalysis] | None, str | None]:
        try:
            return batch, analyze_batch(batch, client), None
        except Exception as e:
            return batch, None, f"{type(e).__name__}: {e}"

    print(
        f"  Analyzing {total} new scene(s) in {len(batches)} batch(es) of up to "
        f"{ANALYSIS_BATCH_SIZE} scene(s), with {ANALYSIS_CONCURRENCY} parallel workers..."
    )

    with ThreadPoolExecutor(max_workers=ANALYSIS_CONCURRENCY) as pool:
        futures = [pool.submit(run_batch, b) for b in batches]
        for fut in as_completed(futures):
            batch, analyses, error = fut.result()
            if error or analyses is None:
                print(f"    batch FAILED ({len(batch)} scenes): {error} — retrying per-scene")
                # Fall back to per-scene for this batch so one bad clip
                # doesn't poison the whole batch.
                for clip in batch:
                    try:
                        analysis = analyze_scene(clip, client)
                    except Exception as e:
                        print(f"    per-scene FAIL {clip.id}: {e}")
                        continue
                    result[clip.id] = analysis
                    with cache_lock:
                        cache[cache_key_for(clip)] = asdict(analysis)
                        cache_path.write_text(
                            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    done_count += 1
                    print(f"    [{done_count}/{total}] (fallback) {clip.path.name} {clip.start:.1f}-{clip.end:.1f}s")
                continue

            for clip, analysis in zip(batch, analyses):
                result[clip.id] = analysis
                with cache_lock:
                    cache[cache_key_for(clip)] = asdict(analysis)
                done_count += 1
            with cache_lock:
                cache_path.write_text(
                    json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            first = batch[0]
            last = batch[-1]
            print(
                f"    [{done_count}/{total}] batch of {len(batch)} — "
                f"{first.path.name} {first.start:.1f}s → {last.path.name} {last.end:.1f}s"
            )

    return result
