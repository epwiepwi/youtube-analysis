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


def _split_sentences(words: list[dict]) -> list[dict]:
    """Group words into sentence-like units based on gaps between word ends and next starts.
    words: list of {"start": float, "end": float, "text": str}
    Returns list of {"start": float, "end": float, "text": str}
    """
    if not words:
        return []
    sentences: list[dict] = []
    bucket: list[dict] = [words[0]]
    gap_threshold = 0.35
    for prev, cur in zip(words, words[1:]):
        gap = cur["start"] - prev["end"]
        if gap >= gap_threshold and len(bucket) >= 3:
            sentences.append({
                "start": bucket[0]["start"],
                "end": bucket[-1]["end"],
                "text": " ".join(w["text"] for w in bucket).strip(),
            })
            bucket = [cur]
        else:
            bucket.append(cur)
    if bucket:
        sentences.append({
            "start": bucket[0]["start"],
            "end": bucket[-1]["end"],
            "text": " ".join(w["text"] for w in bucket).strip(),
        })
    return sentences


def _context_for_cut(cut_start: float, cut_end: float, words: list[dict],
                      sentences: list[dict], window: int = 8) -> dict:
    """Build rich context: words spoken in this cut, window before/after, owning sentence."""
    spoken = [w for w in words if cut_start <= w["start"] < cut_end]
    if not spoken:
        nearest = min(words, key=lambda w: abs(w["start"] - cut_start))
        idx = words.index(nearest)
    else:
        idx = words.index(spoken[0])
    last_idx = words.index(spoken[-1]) if spoken else idx
    before = words[max(0, idx - window):idx]
    after = words[last_idx + 1:last_idx + 1 + window]
    owning = next(
        (s for s in sentences if s["start"] <= (cut_start + cut_end) / 2 <= s["end"]),
        None,
    )
    return {
        "spoken_here": " ".join(w["text"] for w in spoken) or "(silence)",
        "context_before": " ".join(w["text"] for w in before),
        "context_after": " ".join(w["text"] for w in after),
        "full_sentence": owning["text"] if owning else "",
    }


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
    words: list[dict] | None = None,
) -> list[str]:
    """Return a list of clip paths, one per cut, in cut order.

    Matching is sentence-anchored: each cut carries its owning sentence plus
    a short word window before/after. The prompt tells Gemini to pick based
    on that sentence's meaning, not on the 1-2 word display caption.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set.")
    client = genai.Client(api_key=GEMINI_API_KEY)

    hook_end = float(style.get("hook", {}).get("opening_duration_sec", 2.0))
    ending_duration = float(style.get("ending", {}).get("duration_sec", 3.0))

    words = words or []
    sentences = _split_sentences(words)

    cut_entries = []
    for i, (t_start, t_end) in enumerate(cuts):
        phase = _classify_phase(i, len(cuts), t_start, hook_end, ending_duration, total_duration)
        ctx = _context_for_cut(t_start, t_end, words, sentences)
        cut_entries.append({
            "index": i,
            "time": f"{t_start:.2f}-{t_end:.2f}s",
            "phase": phase,
            "spoken_here": ctx["spoken_here"],
            "full_sentence": ctx["full_sentence"],
            "context_before": ctx["context_before"],
            "context_after": ctx["context_after"],
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

    sentences_json = json.dumps(
        [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"]}
         for s in sentences],
        ensure_ascii=False, indent=2,
    )

    header = f"""너는 최고 수준의 짜집기 쇼츠 편집자다. 전체 스토리는 참고만 하고, **각 컷의 "full_sentence"가 말하는 내용에 어울리는 클립**을 고른다.

[전체 스토리 (참고용, 큰 흐름 파악만)]
{narration_text}

[문장 단위 분해 (이것이 진짜 매칭 기준)]
{sentences_json}

[편집 스타일 시그니처]
{style_signature}

[내러티브 템플릿]
{content_template}

[훅 전략]
{hook_tactics}

[절대 규칙 — 순서대로 적용]
1. **매칭 기준은 컷의 "full_sentence"이다.** 그 문장이 무엇을 말하는지 파악하고, 그 의미에 맞는 클립을 골라라. "spoken_here" 1-2어절만 보고 결정하지 마라.
2. 문장이 말하는 **명사/동사/상태가 실제로 썸네일에 보이는지** 확인. 예: 문장이 "양파 껍질 벗기면 곰팡이가 있다"면 곰팡이/껍질/양파가 보이는 클립.
3. **같은 문장 내 여러 컷**은 같은 주제를 다루므로, 변화를 주되 그 문장 주제를 벗어나지 마라 (다양한 각도/상세컷 느낌).
4. phase=hook(맨 앞 {hook_end}s)은 visual_impact 8+이고 즉각 시선을 끄는 클립.
5. phase=problem은 부정/경고/지저분한 장면. phase=solution은 깔끔/정돈/만족감 장면.
6. **문장 의미와 정반대 클립 절대 금지.** "신세계다" 문장에 썩은 양파 같은 건 0점.
7. 연속 컷에 같은 클립 쓰지 마라 (최소 2컷 간격).
8. 안 쓰인 클립이 있으면 낭비다. 가능한 골고루 써라.

[컷 목록 — 각 컷의 full_sentence를 핵심 기준으로 사용]
{json.dumps(cut_entries, ensure_ascii=False, indent=2)}

[사용 가능한 클립 — 아래에 각 클립의 썸네일 이미지가 순서대로 첨부됨]
"""

    for i, path in enumerate(paths_ordered):
        a = clips_analysis[path]
        header += f"\nC{i}: {Path(path).name}\n  desc: {a.description}\n  tags: {a.tags}\n  impact: {a.visual_impact}/10 / emotion: {a.emotion} / suitability: {a.suitability}\n"

    footer = """

위 모든 썸네일을 본 뒤, 각 컷의 full_sentence 의미에 가장 어울리는 클립을 JSON으로만 출력해.
reason에는 반드시: (1) 해당 문장이 말하는 것, (2) 썸네일에 실제로 보이는 시각 요소, (3) 왜 둘이 맞는지.

{"assignments": [
  {"cut": 0, "clip_id": "C3", "reason": "문장 '혹시 양파 망째로 보관하시면'→양파와 보관이 주제 / 썸네일에 망에 담긴 양파 보임 / 정확히 일치"},
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
