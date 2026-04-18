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
    """Lenient JSON extraction. Handles ```json fences, trailing commas,
    single quotes, and truncation around the main object."""
    text = text.strip()
    # Strip fenced code blocks.
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
        raise ValueError(f"No JSON object in response: {text[:400]}")
    body = text[start : end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError as e1:
        cleaned = body.replace(",\n}", "\n}").replace(",\n]", "\n]")
        cleaned = cleaned.replace(",}", "}").replace(",]", "]")
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            raise ValueError(
                f"JSON decode failed at {e1.lineno}:{e1.colno}. "
                f"Body around error: ...{body[max(0, e1.pos-80):e1.pos+80]}..."
            ) from e1


def _tag_cuts_with_captions(cuts: list[tuple[float, float]], captions: list[dict]) -> list[str]:
    texts: list[str] = []
    for c_start, c_end in cuts:
        matching = [c["text"] for c in captions if c_start <= c["start"] < c_end]
        texts.append(" ".join(matching).strip() or "(no caption)")
    return texts


def _split_sentences(words: list[dict]) -> list[dict]:
    """Group words into sentence-like units.

    Splits on any of: (a) a speaker pause of >=0.25s, (b) accumulated sentence
    duration exceeds 4s, or (c) more than 12 words. Keeps sentences at least
    2 words long so single-word blurts don't create 1-word sentences.
    """
    if not words:
        return []
    sentences: list[dict] = []
    bucket: list[dict] = [words[0]]
    gap_threshold = 0.25
    max_duration = 4.0
    max_words = 12

    def flush():
        nonlocal bucket
        sentences.append({
            "start": bucket[0]["start"],
            "end": bucket[-1]["end"],
            "text": " ".join(w["text"] for w in bucket).strip(),
        })
        bucket = []

    for prev, cur in zip(words, words[1:]):
        gap = cur["start"] - prev["end"]
        duration = bucket[-1]["end"] - bucket[0]["start"] if bucket else 0.0
        should_split = (
            (gap >= gap_threshold and len(bucket) >= 2)
            or duration >= max_duration
            or len(bucket) >= max_words
        )
        if should_split:
            flush()
        bucket.append(cur)
    if bucket:
        flush()
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


def _extract_thumbnail(source_file: Path, scene_start: float, scene_end: float,
                       tmp_dir: Path, slug: str) -> Path:
    """Pull a thumbnail from the middle of a scene's range inside its source file."""
    t = scene_start + max(0.0, (scene_end - scene_start) / 2)
    out = tmp_dir / f"{slug}_thumb.jpg"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
         "-i", str(source_file), "-frames:v", "1", "-vf", "scale=320:-1",
         "-q:v", "6", str(out)],
        check=True,
    )
    return out


def _generate_json_with_retry(client: genai.Client, model: str, contents,
                               label: str = "call", max_json_retries: int = 2) -> dict:
    """Wrapper that retries on malformed JSON output."""
    last_error = None
    for attempt in range(max_json_retries + 1):
        resp = _generate_with_retry(client, model, contents)
        try:
            return _parse_json(resp.text)
        except ValueError as e:
            last_error = e
            print(f"  [{label}] JSON parse failed (attempt {attempt + 1}): {e}")
            if attempt < max_json_retries:
                # On retry, append a stern reminder to the prompt.
                if isinstance(contents, str):
                    contents = contents + "\n\n반드시 유효한 JSON 하나만 출력해. 다른 텍스트 금지. 모든 문자열은 큰따옴표. 트레일링 콤마 금지."
                elif isinstance(contents, list):
                    contents = contents + ["\n\n반드시 유효한 JSON 하나만 출력해. 다른 텍스트 금지. 모든 문자열은 큰따옴표. 트레일링 콤마 금지."]
    raise last_error  # type: ignore[misc]


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

[절대 지켜야 할 원칙 — 내러티브 대조]
쇼츠의 문제-해결 구조에서 "문제"로 지목된 요소는 "해결책" 문장의 forbidden_elements에 반드시 포함시켜야 한다.
예:
- 문장2가 "비닐이 꽉 막아서 곰팡이 생김" (문제 = 비닐) 이라면,
- 문장3 "이모는 이걸 꾹 덮어준다" (해결)의 forbidden_elements = ["일반 비닐 랩", "얇은 비닐", "싸구려 비닐봉지"]
- 왜냐면 문제라고 했던 걸 해결책에 다시 보여주면 논리 붕괴. 시청자가 배신감 느낌.
- 이전 문장들을 모두 읽고 "무엇이 부정적 원인으로 지목됐는지" 파악해 해당 요소를 차단.

또 하나:
- "맛 있다/맛 차이" 문장엔 먹는 장면/완성된 결과물. 조리/손질 장면 forbidden.
- "발효/가스 배출" 문장엔 밀폐된 통이나 공기 관련. 먹는 장면/씻는 장면 forbidden.
- CTA("남겨주세요") 문장엔 제목 키워드 관련 컷. 관계없는 B-roll forbidden.

[작업 — 아래 문장 각각에 대해 시각 계획을 세워라]
{json.dumps(sentences, ensure_ascii=False, indent=2)}

[출력 규칙]
각 문장마다:
- visual_intent: 이 순간 화면이 시청자에게 전달해야 할 메시지 (예: "잘못된 보관 방법을 보여줘서 공감 유발")
- required_elements: 화면에 반드시 보여야 하는 구체 요소 (예: ["비닐", "김치통", "덮는 동작"])
- forbidden_elements: 화면에 절대 나오면 안 되는 요소. **선행 문장에서 "문제"로 지목된 것 필수 포함**
- contrast_with_previous: 앞 문장 대비 무엇이 달라야 하는지 1문장 (예: "앞 문장은 비닐 = 나쁨이므로, 이 문장의 해결책은 비닐이 아닌 다른 도구여야 함")
- visual_phase: hook | problem | solution | cta | bridge
- critical: true/false (hook 첫 문장, 솔루션 등장, CTA는 critical=true)
- alternates_ok: true/false (꼭 정확한 매칭 아니어도 분위기만 맞으면 OK인 문장인가)

JSON만 출력:
{{"plans": [
  {{"sentence_index": 0, "visual_intent": "...", "required_elements": [...], "forbidden_elements": [...], "contrast_with_previous": "...", "visual_phase": "hook", "critical": true, "alternates_ok": false}},
  ...
]}}
"""
    print(f"  [Stage 1/2] Planning visuals for {len(sentences)} sentences...")
    data = _generate_json_with_retry(client, GEMINI_TEXT_MODEL, prompt, label="Stage1")
    return data.get("plans", [])


def _score_scene_for_plan(analysis: ClipAnalysis, plan: dict) -> float:
    """Cheap heuristic to rank scenes against a sentence plan before sending to Gemini."""
    required = [str(r).lower() for r in plan.get("required_elements", [])]
    forbidden = [str(f).lower() for f in plan.get("forbidden_elements", [])]
    intent = str(plan.get("visual_intent", "")).lower()
    phase = str(plan.get("visual_phase", ""))

    desc = (analysis.description or "").lower()
    tags = " ".join(analysis.tags).lower() + " " + desc

    req_hits = sum(1 for r in required if r and r in tags)
    forb_hits = sum(1 for f in forbidden if f and f in tags)
    intent_words = [w for w in intent.split() if len(w) > 1]
    intent_hits = sum(1 for w in intent_words if w in tags)

    suitability_for_phase = analysis.suitability.get(phase, 5) if phase else 5

    return (
        req_hits * 6.0
        + intent_hits * 1.5
        + suitability_for_phase * 0.6
        + analysis.retention_value * 0.4
        + analysis.visual_impact * 0.3
        - forb_hits * 12.0
    )


def _prefilter_for_sentence(plan: dict, clips_analysis: dict[str, ClipAnalysis],
                             top_k: int = 20) -> list[str]:
    """Pick the top-k most plausible scenes for this sentence to send to Gemini."""
    scored = [(_score_scene_for_plan(a, plan), sid) for sid, a in clips_analysis.items()]
    scored.sort(reverse=True)
    return [sid for _, sid in scored[:top_k]]


def _enforce_reuse_cap(paths: list[str], reasons: list[str],
                        clips_analysis: dict[str, ClipAnalysis],
                        plans_by_sent: dict[int, dict],
                        cuts: list[dict],
                        max_per_scene: int = 2,
                        max_per_file: int = 2) -> tuple[list[str], list[str]]:
    """Swap over-used clips with the best unused alternative.

    Caps are applied on two axes:
    - per-scene: same scene_id used more than `max_per_scene` times.
    - per-source-file: same underlying mp4 contributed more than `max_per_file`
      scenes to the output. Prevents the 'same person appears 3x in different
      moments from one source file' failure mode.
    """
    def file_of(scene_id: str) -> str:
        a = clips_analysis.get(scene_id)
        return str(a.source_file) if a else scene_id.split("#")[0]

    scene_uses: dict[str, int] = {}
    file_uses: dict[str, int] = {}
    for p in paths:
        scene_uses[p] = scene_uses.get(p, 0) + 1
        file_uses[file_of(p)] = file_uses.get(file_of(p), 0) + 1

    new_paths = list(paths)
    new_reasons = list(reasons)

    for i, p in enumerate(new_paths):
        over_scene = scene_uses[p] > max_per_scene
        over_file = file_uses[file_of(p)] > max_per_file
        if not (over_scene or over_file):
            continue
        plan = plans_by_sent.get(cuts[i].get("owning_sentence_index"), {})
        neighbors = set()
        if i > 0:
            neighbors.add(new_paths[i - 1])
        if i + 1 < len(new_paths):
            neighbors.add(new_paths[i + 1])
        candidates = []
        for sid, a in clips_analysis.items():
            if scene_uses.get(sid, 0) >= max_per_scene:
                continue
            if file_uses.get(str(a.source_file), 0) >= max_per_file and str(a.source_file) != file_of(p):
                continue
            if sid in neighbors:
                continue
            candidates.append((_score_scene_for_plan(a, plan), sid))
        if not candidates:
            continue
        candidates.sort(reverse=True)
        new_sid = candidates[0][1]
        scene_uses[p] = max(0, scene_uses[p] - 1)
        scene_uses[new_sid] = scene_uses.get(new_sid, 0) + 1
        file_uses[file_of(p)] = max(0, file_uses[file_of(p)] - 1)
        file_uses[file_of(new_sid)] = file_uses.get(file_of(new_sid), 0) + 1
        reason_tag = "reuse-cap(scene)" if over_scene else "reuse-cap(file)"
        new_paths[i] = new_sid
        new_reasons[i] = f"[{reason_tag}] swapped → {Path(new_sid).name}"
    return new_paths, new_reasons


def _assign_clips_for_sentence(client: genai.Client, sentence_idx: int,
                                sentence_text: str, plan: dict,
                                cuts_in_sentence: list[dict],
                                candidate_ids: list[str],
                                clips_analysis: dict[str, ClipAnalysis],
                                style: dict, narration_text: str,
                                tmp_dir: Path) -> dict[int, tuple[str, str, int]]:
    """Run Stage 2 for ONE sentence. Returns {cut_index: (scene_id, reason, score)}."""
    style_signature = style.get("capcut_automation_hints", {}).get("style_signature", "")

    thumbnails: list[bytes] = []
    for i, sid in enumerate(candidate_ids):
        a = clips_analysis[sid]
        try:
            thumb = _extract_thumbnail(
                Path(a.source_file), a.start, a.end, tmp_dir, slug=f"s{sentence_idx}_c{i}"
            )
            thumbnails.append(thumb.read_bytes())
        except Exception:
            thumbnails.append(b"")

    header = f"""너는 짜집기 쇼츠 편집자다. 한 문장에 속한 컷들에 클립을 배정한다.

[전체 나레이션 — 흐름 파악만]
{narration_text}

[편집 스타일]
{style_signature}

[지금 배정할 문장]
"{sentence_text}"

[이 문장의 시각 계획 (1단계 디렉터의 지시)]
- visual_intent: {plan.get("visual_intent", "")}
- required_elements: {plan.get("required_elements", [])}
- forbidden_elements: {plan.get("forbidden_elements", [])}
- contrast_with_previous: {plan.get("contrast_with_previous", "")}
- visual_phase: {plan.get("visual_phase", "")}
- critical: {plan.get("critical", False)}

[참조 영상의 해당 순간 (이것을 모방해야 함)]
각 컷의 reference_target_visual / reference_target_elements 필드가 있다면, 그것은 이 타이밍에 "잘 편집된 참조 영상이 보여준 장면"이다.
네 목표는 내 클립 풀에서 **그 참조 장면과 가장 비슷한 클립**을 고르는 것. 참조를 레시피처럼 따라하라.

[키워드 매칭 vs 의미 매칭 — 가장 중요]
⚠️ "단어 같다 = 매칭" 절대 아니다. 의미/의도가 같아야 매칭이다.

나쁜 예:
- "솔루션 도구 등장" 문장에 "외국인이 병뚜껑 못 여는 밈" → 0점
- "맛 차이" 문장에 "지폐를 지갑에 넣는 영상" → 무관함, 0점
- "곰팡이 생긴다" 문장에 "신선한 깍두기" → 정반대, 0점
- "이걸 덮어준다" (솔루션) 문장에 "그냥 비닐 랩" → 직전 문장이 비닐 문제라 했음. 모순. 0점

[절대 규칙]
1. reference_target_visual이 있으면 그것과 최대한 닮은 클립을 우선으로 골라라.
2. 참조가 없거나 본인 클립 풀에 비슷한 게 없으면, visual_intent + required_elements로 판단.
3. forbidden_elements가 썸네일에 보이면 0점.
4. critical=true면 9점 이상만 허용. 9점 이상 없으면 가장 가까운 거 + reason에 "타협" 명시.
5. 같은 클립을 연속 컷에 배치 금지.
6. 첫 문장(hook)이면 visual_impact + retention 모두 8↑인 클립 우선.
7. retention 1-3 클립은 가능한 회피.

[배정할 컷 목록]
{json.dumps(cuts_in_sentence, ensure_ascii=False, indent=2)}

[후보 클립 목록 — 아래 썸네일 첨부 순서대로]
"""

    for i, sid in enumerate(candidate_ids):
        a = clips_analysis[sid]
        retention = getattr(a, "retention_value", a.visual_impact)
        header += (
            f"\nC{i}: {sid}\n"
            f"  desc: {a.description}\n"
            f"  tags: {a.tags}\n"
            f"  emotion: {a.emotion} / impact: {a.visual_impact}/10 / retention: {retention}/10\n"
        )

    footer = """

각 컷에 클립을 배정해 JSON으로만 출력. reason에는 (1) 컷의 visual_intent (2) 썸네일에 실제로 보이는 시각 요소 (3) 둘이 어떻게 일치하는지 명시.

{"assignments": [
  {"cut": <글로벌_cut_index>, "clip_id": "C3", "score": 9, "reason": "..."},
  ...
]}
"""
    parts: list = [header]
    for i, (sid, thumb) in enumerate(zip(candidate_ids, thumbnails)):
        parts.append(f"[C{i} 썸네일:]")
        if thumb:
            parts.append(types.Part.from_bytes(data=thumb, mime_type="image/jpeg"))
    parts.append(footer)

    data = _generate_json_with_retry(client, GEMINI_TEXT_MODEL, parts,
                                      label=f"S2.sent{sentence_idx}")
    id_to_path = {f"C{i}": candidate_ids[i] for i in range(len(candidate_ids))}

    out: dict[int, tuple[str, str, int]] = {}
    for a in data.get("assignments", []):
        try:
            cut_idx = int(a["cut"])
            cid = str(a["clip_id"])
            if cid not in id_to_path:
                continue
            out[cut_idx] = (id_to_path[cid], str(a.get("reason", "")), int(a.get("score", 0)))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _assign_clips(client: genai.Client, cuts: list[dict], sentence_plans: list[dict],
                  clips_analysis: dict[str, ClipAnalysis], style: dict,
                  narration_text: str) -> tuple[list[str], list[str]]:
    """Stage 2 done per-sentence so each Gemini call sees a focused candidate set."""
    plans_by_sent = {p["sentence_index"]: p for p in sentence_plans}

    tmp_dir = Path(tempfile.mkdtemp(prefix="match_thumbs_"))
    print(f"  [Stage 2/2] Per-sentence matching ({len(sentence_plans)} sentence(s))...")

    by_cut: dict[int, tuple[str, str, int]] = {}
    paths_ordered = list(clips_analysis.keys())

    # Group cuts by sentence.
    cuts_by_sent: dict[int, list[dict]] = {}
    for c in cuts:
        sidx = c.get("owning_sentence_index")
        cuts_by_sent.setdefault(sidx, []).append(c)

    for sidx, cuts_in_sent in cuts_by_sent.items():
        plan = plans_by_sent.get(sidx, {})
        sentence_text = next(
            (p.get("text", "") for p in sentence_plans if p["sentence_index"] == sidx), ""
        )
        candidates = _prefilter_for_sentence(plan, clips_analysis, top_k=20)
        print(f"    sentence {sidx}: {len(cuts_in_sent)} cut(s) ← {len(candidates)} candidates")
        try:
            assignments = _assign_clips_for_sentence(
                client, sidx, sentence_text, plan, cuts_in_sent,
                candidates, clips_analysis, style, narration_text, tmp_dir,
            )
        except Exception as e:
            print(f"    sentence {sidx} FAILED: {e}")
            assignments = {}
        by_cut.update(assignments)

    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)
    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    result_paths: list[str] = []
    result_reasons: list[str] = []
    for i in range(len(cuts)):
        entry = by_cut.get(i)
        if entry is None:
            cid = paths_ordered[i % len(paths_ordered)]
            result_paths.append(cid)
            result_reasons.append("(fallback: matcher skipped this cut)")
        else:
            sid, reason, score = entry
            result_paths.append(sid)
            result_reasons.append(f"[score={score}] {reason}")

    # Hard reuse cap as a safety net for when Gemini ignores the rule.
    result_paths, result_reasons = _enforce_reuse_cap(
        result_paths, result_reasons, clips_analysis, plans_by_sent, cuts,
        max_per_scene=2, max_per_file=2,
    )
    return result_paths, result_reasons


def match_clips(
    cuts: list[tuple[float, float]],
    captions: list[dict],
    clips_analysis: dict[str, ClipAnalysis],
    style: dict,
    total_duration: float,
    narration_text: str = "",
    words: list[dict] | None = None,
    reference_beats: list[dict] | None = None,
) -> list[str]:
    """Two-stage matching. Filters unusable clips. Returns clip path per cut."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set.")
    client = genai.Client(api_key=GEMINI_API_KEY)

    usable = {p: a for p, a in clips_analysis.items() if a.usable}
    skipped = [(p, a) for p, a in clips_analysis.items() if not a.usable]
    if skipped:
        print(f"  Filtering out {len(skipped)} unusable clip(s):")
        for p, a in skipped:
            print(f"    - {Path(p).name} [{a.clip_type}]: {a.skip_reason or '(no reason)'}")
    if not usable:
        raise RuntimeError(
            "All clips marked as unusable. The filter may be too aggressive — "
            "delete output/clips_index.json and re-run to re-analyze."
        )
    if len(usable) < 3:
        print(f"  Warning: only {len(usable)} usable clips. Matching quality will suffer.")

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

    ref_by_cut = {b["cut_index"]: b for b in (reference_beats or [])}

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
        ref = ref_by_cut.get(i)
        entry = {
            "index": i,
            "time": f"{t_start:.2f}-{t_end:.2f}s",
            "phase": phase,
            "owning_sentence_index": owning_idx,
            "owning_sentence_text": owning_text,
        }
        if ref:
            entry["reference_target_visual"] = ref.get("reference_visual", "")
            entry["reference_target_elements"] = ref.get("reference_elements", [])
            entry["reference_target_emotion"] = ref.get("reference_emotion", "")
            entry["reference_target_phase"] = ref.get("reference_phase", "")
        cut_entries.append(entry)

    paths, reasons = _assign_clips(client, cut_entries, plans, usable, style, narration_text)

    # Post-check: warn when the hook slot didn't land a high-impact clip.
    if paths:
        hook_clip = usable.get(paths[0])
        if hook_clip and hook_clip.visual_impact < 8:
            high_impact = [
                p for p, a in usable.items()
                if a.visual_impact >= 8 and p != paths[0]
            ]
            if high_impact:
                print(
                    f"  Warning: hook clip '{Path(paths[0]).name}' has impact "
                    f"{hook_clip.visual_impact}/10. {len(high_impact)} higher-impact clip(s) "
                    "were available — matcher may have violated the hook rule."
                )

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
