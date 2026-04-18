"""Semantic matcher: decide which clip to show at each cut.

Gives Gemini the actual clip thumbnails plus the full narration so it matches
on what the footage looks like, not just the text description of it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from .config import GEMINI_API_KEY, GEMINI_TEXT_MODEL
from .vision import ClipAnalysis


def _generate_with_retry(client: genai.Client, model: str, contents, max_attempts: int = 5):
    delay = 4.0
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except (genai_errors.ServerError, genai_errors.APIError) as e:
            code = getattr(e, "code", None) or getattr(getattr(e, "response", None), "status_code", None)
            if code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                print(f"  [{code}] Gemini busy, retry {attempt}/{max_attempts} in {delay:.0f}s...")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON in response: {text[:300]}")
    return json.loads(text[start : end + 1])


def _tag_cuts_with_captions(cuts: list[tuple[float, float]], captions: list[dict]) -> list[str]:
    texts: list[str] = []
    for c_start, c_end in cuts:
        matching = [c["text"] for c in captions if c_start <= c["start"] < c_end]
        texts.append(" ".join(matching).strip() or "(no caption)")
    return texts


def _classify_phase(cut_index: int, total_cuts: int, t_start: float, hook_end: float,
                    ending_duration: float, total_duration: float) -> str:
    if t_start < hook_end:
        return "hook"
    if t_start > total_duration - ending_duration:
        return "cta"
    mid = total_cuts // 2
    if cut_index < mid:
        return "problem"
    return "solution"


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True,
    )
    return float(out.strip())


def _extract_thumbnail(clip_path: Path, tmp_dir: Path) -> Path:
    duration = _probe_duration(clip_path)
    t = duration * 0.5
    out = tmp_dir / f"{clip_path.stem}_thumb.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
         "-i", str(clip_path), "-frames:v", "1", "-vf", "scale=480:-1",
         "-q:v", "5", str(out)],
        check=True,
    )
    return out


def match_clips(
    cuts: list[tuple[float, float]],
    captions: list[dict],
    clips_analysis: dict[str, ClipAnalysis],
    style: dict,
    total_duration: float,
    narration_text: str = "",
) -> list[str]:
    """Return a list of clip paths, one per cut, in cut order."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set.")
    client = genai.Client(api_key=GEMINI_API_KEY)

    hook_end = float(style.get("hook", {}).get("opening_duration_sec", 2.0))
    ending_duration = float(style.get("ending", {}).get("duration_sec", 3.0))

    cut_texts = _tag_cuts_with_captions(cuts, captions)
    cut_entries = []
    for i, ((t_start, t_end), text) in enumerate(zip(cuts, cut_texts)):
        phase = _classify_phase(i, len(cuts), t_start, hook_end, ending_duration, total_duration)
        cut_entries.append({
            "index": i,
            "start": round(t_start, 2),
            "end": round(t_end, 2),
            "caption": text,
            "phase": phase,
        })

    paths_ordered = list(clips_analysis.keys())

    tmp_dir = Path(tempfile.mkdtemp(prefix="match_thumbs_"))
    print(f"  Extracting thumbnails for {len(paths_ordered)} clips...")
    thumbnails: list[bytes] = []
    for p in paths_ordered:
        try:
            thumb = _extract_thumbnail(Path(p), tmp_dir)
            thumbnails.append(thumb.read_bytes())
        except Exception as e:
            print(f"    thumbnail failed for {Path(p).name}: {e}")
            thumbnails.append(b"")

    style_signature = style.get("capcut_automation_hints", {}).get("style_signature", "")
    content_template = style.get("content_structure", {}).get("template", "")
    hook_tactics = "; ".join(style.get("hook", {}).get("retention_tactics", []))

    header = f"""너는 최고 수준의 짜집기 쇼츠 편집자다. 내가 주는 자막 흐름과 실제 클립 썸네일을 보고, 각 컷에 시각적으로 가장 어울리는 클립을 고른다.

[편집 스타일 시그니처]
{style_signature}

[내러티브 템플릿]
{content_template}

[훅 전략]
{hook_tactics}

[전체 나레이션 (맥락 파악용)]
{narration_text}

[절대 규칙]
1. 자막이 말하는 대상(명사/동사)이 실제로 화면에 보이는 클립을 최우선으로 골라라. 예: 자막이 "썩은 양파"면 곰팡이/썩은 모습이 보이는 클립, "주부"면 사람이 등장하는 클립.
2. phase=hook(맨 앞 {hook_end}s)은 visual_impact 8+이고 썸네일이 즉각 시선을 끄는 클립으로 시작.
3. phase=problem은 부정/경고/지저분한 장면. phase=solution은 깔끔/정돈/만족감 장면. 썸네일 색감과 상황으로 판별.
4. 같은 클립을 연속 컷에 넣지 마라. 최소 2컷 간격.
5. 모든 클립을 최소 1번은 쓰려고 시도해라 (안 쓰이는 게 있으면 낭비).
6. 클립 내용과 자막이 정반대일 땐 절대 쓰지 마라 (예: "신세계" 자막에 썩은 양파 썸네일).

[컷 목록]
{json.dumps(cut_entries, ensure_ascii=False, indent=2)}

[사용 가능한 클립 — 아래에 각 클립의 썸네일 이미지가 순서대로 첨부됨]
"""

    for i, path in enumerate(paths_ordered):
        a = clips_analysis[path]
        header += f"\nC{i}: {Path(path).name}\n  desc: {a.description}\n  tags: {a.tags}\n  impact: {a.visual_impact}/10 / emotion: {a.emotion} / suitability: {a.suitability}\n"

    footer = """

위 클립 목록 이미지를 모두 본 뒤, 각 컷마다 가장 어울리는 클립을 JSON으로만 출력해.
각 assignment에 reason(왜 이 클립을 골랐는지, 썸네일에서 본 실제 시각 요소 기반) 반드시 포함.
다른 설명 금지, JSON만:

{"assignments": [
  {"cut": 0, "clip_id": "C3", "reason": "썸네일에 썩은 양파가 보이고 자막이 '썩은 양파'라서 정확히 일치"},
  ...
]}
"""

    parts: list = [header]
    for i, (path, thumb_bytes) in enumerate(zip(paths_ordered, thumbnails)):
        parts.append(f"[C{i} 썸네일:]")
        if thumb_bytes:
            parts.append(types.Part.from_bytes(data=thumb_bytes, mime_type="image/jpeg"))
    parts.append(footer)

    print(f"  Matching {len(cuts)} cuts to {len(paths_ordered)} clips (visual + context)...")
    resp = _generate_with_retry(client, GEMINI_TEXT_MODEL, parts)

    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)
    tmp_dir.rmdir()

    data = _parse_json(resp.text)
    id_to_path = {f"C{i}": paths_ordered[i] for i in range(len(paths_ordered))}

    assignments = data.get("assignments", [])
    by_cut: dict[int, tuple[str, str]] = {}
    for a in assignments:
        by_cut[int(a["cut"])] = (str(a["clip_id"]), str(a.get("reason", "")))

    result: list[str] = []
    reasons: list[str] = []
    for i in range(len(cuts)):
        entry = by_cut.get(i)
        if entry is None or entry[0] not in id_to_path:
            cid = f"C{i % len(paths_ordered)}"
            reasons.append("(fallback: matcher skipped this cut)")
        else:
            cid = entry[0]
            reasons.append(entry[1])
        result.append(id_to_path[cid])

    # Stash reasons for save_matches to pick up.
    match_clips.last_reasons = reasons  # type: ignore[attr-defined]
    return result


def save_matches(cuts: list[tuple[float, float]], cut_texts: list[str],
                 paths: list[str], out_path: Path) -> None:
    reasons = getattr(match_clips, "last_reasons", [""] * len(cuts))
    payload = [
        {"cut": i, "start": cuts[i][0], "end": cuts[i][1],
         "caption": cut_texts[i], "clip": Path(paths[i]).name,
         "reason": reasons[i] if i < len(reasons) else ""}
        for i in range(len(cuts))
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
