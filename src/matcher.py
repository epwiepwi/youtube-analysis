"""Two-stage semantic matcher.

Stage 1: per-sentence visual planning (text-only Gemini call). For every
sentence in the narration, decide what the viewer should SEE: required
elements, forbidden contradictions, and whether it is a critical moment
(solution reveal, CTA hook).

Stage 2: per-cut clip assignment (vision Gemini call). Given the plan and
the actual clip thumbnails, assign one usable clip per cut. Junk clips
(outro/logo) are filtered out before this stage.
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


def _plan_sentences(client: genai.Client, sentences: list[dict], narration_text: str,
                    style: dict) -> list[dict]:
    """Stage 1: ask Gemini what each sentence needs to show visually."""
    style_signature = style.get("capcut_automation_hints", {}).get("style_signature", "")
    content_template = style.get("content_structure", {}).get("template", "")

    prompt = f"""너는 짜집기 쇼츠 편집 디렉터다. 나레이션 문장 하나하나에 대해, "이 순간 시청자가 무엇을 봐야 하는지"를 미리 계획한다.

[전체 나레이션]
{narration_text}

[편집 스타일]
{style_signature}

[내러티브 템플릿]
{content_template}

[작업 — 아래 문장 각각에 대해 시각 계획을 세워라]
{json.dumps(sentences, ensure_ascii=False, indent=2)}

[출력 규칙]
각 문장마다:
- visual_intent: 이 순간 화면이 시청자에게 전달해야 할 메시지 (예: "잘못된 보관 방법을 보여줘서 공감 유발")
- required_elements: 화면에 반드시 보여야 하는 구체 요소 (예: ["비닐", "김치통", "덮는 동작"])
- forbidden_elements: 화면에 절대 나오면 안 되는 요소 (예: ["깨끗한 결과물", "먹는 장면"])
- visual_phase: hook | problem | solution | cta | bridge
- critical: true/false (이 문장이 영상의 결정적 순간인가? — hook 첫 문장, 솔루션 등장, CTA는 critical=true)
- alternates_ok: true/false (꼭 정확한 매칭 아니어도 분위기만 맞으면 OK인 문장인가)

JSON만 출력:
{{"plans": [
  {{"sentence_index": 0, "visual_intent": "...", "required_elements": [...], "forbidden_elements": [...], "visual_phase": "hook", "critical": true, "alternates_ok": false}},
  ...
]}}
"""
    print(f"  [Stage 1/2] Planning visuals for {len(sentences)} sentences...")
    resp = _generate_with_retry(client, GEMINI_TEXT_MODEL, prompt)
    data = _parse_json(resp.text)
    return data.get("plans", [])


def _assign_clips(client: genai.Client, cuts: list[dict], sentence_plans: list[dict],
                  clips_analysis: dict[str, ClipAnalysis], style: dict,
                  narration_text: str) -> tuple[list[str], list[str]]:
    """Stage 2: with the plans + thumbnails, pick a clip for each cut.

    Returns (clip_paths_per_cut, reasons_per_cut).
    """
    paths_ordered = list(clips_analysis.keys())

    tmp_dir = Path(tempfile.mkdtemp(prefix="match_thumbs_"))
    print(f"  [Stage 2/2] Extracting {len(paths_ordered)} thumbnails...")
    thumbnails: list[bytes] = []
    for p in paths_ordered:
        try:
            thumb = _extract_thumbnail(Path(p), tmp_dir)
            thumbnails.append(thumb.read_bytes())
        except Exception as e:
            print(f"    thumbnail failed for {Path(p).name}: {e}")
            thumbnails.append(b"")

    plans_by_sent = {p["sentence_index"]: p for p in sentence_plans}

    enriched_cuts = []
    for c in cuts:
        plan = plans_by_sent.get(c.get("owning_sentence_index"))
        enriched_cuts.append({
            **c,
            "visual_intent": (plan or {}).get("visual_intent", ""),
            "required_elements": (plan or {}).get("required_elements", []),
            "forbidden_elements": (plan or {}).get("forbidden_elements", []),
            "critical": (plan or {}).get("critical", False),
            "alternates_ok": (plan or {}).get("alternates_ok", True),
        })

    style_signature = style.get("capcut_automation_hints", {}).get("style_signature", "")

    header = f"""너는 짜집기 쇼츠 편집자다. 1단계에서 디렉터가 짜둔 [시각 계획]을 따라, 2단계로 각 컷에 클립을 배정한다.

[편집 스타일]
{style_signature}

[전체 나레이션 — 흐름 파악용]
{narration_text}

[금지 매칭 예시 — 절대 하지 마라]
- "보관/덮는다" 문장에 "씻는다/썬다" 클립 → 동작이 다름, 0점
- "깔끔한 결과" 문장에 "썩은/곰팡이" 클립 → 의미 정반대, 0점
- "먹다/맛" 문장에 "재료 손질" 클립 → 단계가 다름, 1점
- "솔루션 도구 등장" 문장(critical=true)에 도구 안 보이는 클립 → 핵심 실패, 1점
- 어떤 문장에도 outro/로고/워터마크 클립 → 의미 무관, 0점

[좋은 매칭 예시]
- "꾹 덮어준다" 문장 → 손이 뚜껑을 누르는 클립
- "곰팡이 생긴다" 문장 → 부패한 음식 클로즈업
- "맛이 미쳤다" 문장 → 김치+밥 먹방 클로즈업

[절대 규칙]
1. 각 컷의 visual_intent를 먼저 읽고, required_elements가 썸네일에 보이는 클립을 골라라.
2. forbidden_elements가 보이는 클립은 점수 0으로 취급.
3. critical=true 컷은 매칭 강도 9점 이상만 허용. 9점 이상 클립 없으면 가장 가까운 거 + reason에 "타협"이라고 명시.
4. 같은 클립을 연속 2컷에 배치 금지. 같은 문장 내에서도 변화 줘라.
5. 안 쓰인 클립이 있으면 손해. 가능한 골고루.

[컷 목록 — visual_intent에 맞는 클립을 찾아라]
{json.dumps(enriched_cuts, ensure_ascii=False, indent=2)}

[사용 가능한 클립 — 아래에 썸네일 첨부]
"""

    for i, path in enumerate(paths_ordered):
        a = clips_analysis[path]
        header += f"\nC{i}: {Path(path).name}\n  desc: {a.description}\n  tags: {a.tags}\n  emotion: {a.emotion} / impact: {a.visual_impact}/10\n"

    footer = """

각 컷에 클립을 배정해 JSON으로만 출력. reason에는 (1) 컷의 visual_intent, (2) 썸네일에 실제로 보이는 시각 요소, (3) 둘이 어떻게 일치하는지 적어.

{"assignments": [
  {"cut": 0, "clip_id": "C3", "score": 9, "reason": "intent='보관 행위 보여주기' / 썸네일에 비닐 덮인 김치통 보임 / 정확히 일치"},
  ...
]}
"""

    parts: list = [header]
    for i, (path, thumb_bytes) in enumerate(zip(paths_ordered, thumbnails)):
        parts.append(f"[C{i} 썸네일:]")
        if thumb_bytes:
            parts.append(types.Part.from_bytes(data=thumb_bytes, mime_type="image/jpeg"))
    parts.append(footer)

    print(f"  Matching {len(cuts)} cuts to {len(paths_ordered)} usable clips...")
    resp = _generate_with_retry(client, GEMINI_TEXT_MODEL, parts)

    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)
    tmp_dir.rmdir()

    data = _parse_json(resp.text)
    id_to_path = {f"C{i}": paths_ordered[i] for i in range(len(paths_ordered))}

    assignments = data.get("assignments", [])
    by_cut: dict[int, tuple[str, str, int]] = {}
    for a in assignments:
        by_cut[int(a["cut"])] = (str(a["clip_id"]), str(a.get("reason", "")), int(a.get("score", 0)))

    result_paths: list[str] = []
    result_reasons: list[str] = []
    for i in range(len(cuts)):
        entry = by_cut.get(i)
        if entry is None or entry[0] not in id_to_path:
            cid = f"C{i % len(paths_ordered)}"
            result_paths.append(id_to_path[cid])
            result_reasons.append("(fallback: matcher skipped this cut)")
        else:
            cid, reason, score = entry
            result_paths.append(id_to_path[cid])
            result_reasons.append(f"[score={score}] {reason}")
    return result_paths, result_reasons


def match_clips(
    cuts: list[tuple[float, float]],
    captions: list[dict],
    clips_analysis: dict[str, ClipAnalysis],
    style: dict,
    total_duration: float,
    narration_text: str = "",
    words: list[dict] | None = None,
) -> list[str]:
    """Two-stage matching. Filters unusable clips. Returns clip path per cut."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set.")
    client = genai.Client(api_key=GEMINI_API_KEY)

    usable = {p: a for p, a in clips_analysis.items() if a.usable}
    skipped = len(clips_analysis) - len(usable)
    if skipped:
        print(f"  Filtering out {skipped} unusable clip(s) (outro/logo/watermark).")
    if not usable:
        raise RuntimeError("No usable clips after filtering. Check vision analysis.")

    hook_end = float(style.get("hook", {}).get("opening_duration_sec", 2.0))
    ending_duration = float(style.get("ending", {}).get("duration_sec", 3.0))

    words = words or []
    sentences = _split_sentences(words)
    sentences_payload = [
        {"sentence_index": i, "start": round(s["start"], 2), "end": round(s["end"], 2),
         "text": s["text"]}
        for i, s in enumerate(sentences)
    ]

    plans = _plan_sentences(client, sentences_payload, narration_text, style)

    cut_entries = []
    for i, (t_start, t_end) in enumerate(cuts):
        phase = _classify_phase(i, len(cuts), t_start, hook_end, ending_duration, total_duration)
        owning_idx = next(
            (s["sentence_index"] for s in sentences_payload
             if s["start"] <= (t_start + t_end) / 2 <= s["end"]),
            None,
        )
        owning_text = next(
            (s["text"] for s in sentences_payload if s["sentence_index"] == owning_idx),
            "",
        )
        cut_entries.append({
            "index": i,
            "time": f"{t_start:.2f}-{t_end:.2f}s",
            "phase": phase,
            "owning_sentence_index": owning_idx,
            "owning_sentence_text": owning_text,
        })

    paths, reasons = _assign_clips(client, cut_entries, plans, usable, style, narration_text)
    match_clips.last_reasons = reasons  # type: ignore[attr-defined]
    match_clips.last_plans = plans  # type: ignore[attr-defined]
    return paths


def save_matches(cuts: list[tuple[float, float]], cut_texts: list[str],
                 paths: list[str], out_path: Path) -> None:
    reasons = getattr(match_clips, "last_reasons", [""] * len(cuts))
    plans = getattr(match_clips, "last_plans", [])
    payload = {
        "sentence_plans": plans,
        "cuts": [
            {"cut": i, "start": cuts[i][0], "end": cuts[i][1],
             "caption": cut_texts[i], "clip": Path(paths[i]).name,
             "reason": reasons[i] if i < len(reasons) else ""}
            for i in range(len(cuts))
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
