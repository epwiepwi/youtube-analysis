"""Semantic matcher: decide which clip to show at each cut.

Single Gemini call assigns every cut in one shot so it can plan globally
(e.g. save the most shocking clip for the hook, avoid repeats, follow the
problem -> solution structure from the style profile).
"""

from __future__ import annotations

import json
from pathlib import Path

from google import genai

from .config import GEMINI_API_KEY, GEMINI_TEXT_MODEL
from .vision import ClipAnalysis


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
    """For each cut, collect caption text whose start falls inside the cut."""
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


def match_clips(
    cuts: list[tuple[float, float]],
    captions: list[dict],
    clips_analysis: dict[str, ClipAnalysis],
    style: dict,
    total_duration: float,
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

    clip_entries = []
    paths_ordered = list(clips_analysis.keys())
    for i, path in enumerate(paths_ordered):
        a = clips_analysis[path]
        clip_entries.append({
            "id": f"C{i}",
            "description": a.description,
            "tags": a.tags,
            "visual_impact": a.visual_impact,
            "emotion": a.emotion,
            "suitability": a.suitability,
        })

    style_signature = style.get("capcut_automation_hints", {}).get("style_signature", "")
    content_template = style.get("content_structure", {}).get("template", "")

    prompt = f"""너는 짜집기 쇼츠 편집자다. 각 컷에 가장 어울리는 클립을 고른다.

[편집 스타일]
{style_signature}

[내러티브 구조]
{content_template}

[규칙]
1. phase=hook 구간은 visual_impact와 suitability.hook이 가장 높은 클립으로 시작 (시청자 3초 이탈 방지)
2. phase=problem은 emotion이 shock/warning/disgusting이고 suitability.problem 높은 클립
3. phase=solution은 emotion이 clean/calm이고 suitability.solution 높은 클립
4. phase=cta는 마무리 느낌 clip, suitability.cta 고려
5. 같은 클립을 연속 컷에 배치하지 말 것 (직전 컷과 다른 클립)
6. 자막(caption) 내용과 의미적으로 가장 어울리는 클립 선택
7. 모든 클립을 골고루 써라 (반복 최소화). 단, 충분히 어울리면 재사용 허용.

[컷 목록]
{json.dumps(cut_entries, ensure_ascii=False, indent=2)}

[사용 가능한 클립]
{json.dumps(clip_entries, ensure_ascii=False, indent=2)}

각 컷에 클립을 할당해 JSON만 출력:
{{"assignments": [{{"cut": 0, "clip_id": "C3", "reason": "..." }}, ...]}}
"""

    resp = client.models.generate_content(
        model=GEMINI_TEXT_MODEL,
        contents=prompt,
    )
    data = _parse_json(resp.text)
    id_to_path = {f"C{i}": paths_ordered[i] for i in range(len(paths_ordered))}

    assignments = data.get("assignments", [])
    by_cut = {int(a["cut"]): str(a["clip_id"]) for a in assignments}
    result: list[str] = []
    for i in range(len(cuts)):
        cid = by_cut.get(i)
        if cid not in id_to_path:
            cid = f"C{i % len(paths_ordered)}"
        result.append(id_to_path[cid])
    return result


def save_matches(cuts: list[tuple[float, float]], cut_texts: list[str],
                 paths: list[str], out_path: Path) -> None:
    payload = [
        {"cut": i, "start": cuts[i][0], "end": cuts[i][1],
         "caption": cut_texts[i], "clip": Path(paths[i]).name}
        for i in range(len(cuts))
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
