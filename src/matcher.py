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


def _assign_clips(client: genai.Client, cuts: list[dict], sentence_plans: list[dict],
                  clips_analysis: dict[str, ClipAnalysis], style: dict,
                  narration_text: str) -> tuple[list[str], list[str]]:
    """Stage 2: with the plans + thumbnails, pick a clip for each cut.

    Returns (clip_paths_per_cut, reasons_per_cut).
    """
    paths_ordered = list(clips_analysis.keys())

    tmp_dir = Path(tempfile.mkdtemp(prefix="match_thumbs_"))
    print(f"  [Stage 2/2] Extracting {len(paths_ordered)} scene thumbnails...")
    thumbnails: list[bytes] = []
    for i, scene_id in enumerate(paths_ordered):
        a = clips_analysis[scene_id]
        try:
            thumb = _extract_thumbnail(
                Path(a.source_file), a.start, a.end, tmp_dir, slug=f"s{i}"
            )
            thumbnails.append(thumb.read_bytes())
        except Exception as e:
            print(f"    thumbnail failed for {scene_id}: {e}")
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
            "contrast_with_previous": (plan or {}).get("contrast_with_previous", ""),
            "critical": (plan or {}).get("critical", False),
            "alternates_ok": (plan or {}).get("alternates_ok", True),
        })

    style_signature = style.get("capcut_automation_hints", {}).get("style_signature", "")

    header = f"""너는 짜집기 쇼츠 편집자다. 1단계에서 디렉터가 짜둔 [시각 계획]을 따라, 2단계로 각 컷에 클립을 배정한다.

[편집 스타일]
{style_signature}

[전체 나레이션 — 흐름 파악용]
{narration_text}

[키워드 매칭 vs 의미 매칭 — 가장 중요]
⚠️ "같은 단어가 있다 = 매칭" 아니다. 의미/의도가 같아야 매칭이다.

나쁜 예 (키워드만 맞춤):
- "가스 뱉어내야 한다" 문장에 "김치 위에 쌀밥 올리기" 클립 → 둘 다 "김치" 키워드지만 의미 완전 다름, 0점
- "이모 솔루션(특별한 덮개)" 문장에 "얇은 비닐 랩 덮기" 클립 → 둘 다 "덮다" 키워드지만 직전 문장에서 비닐이 문제라고 했음. **논리 모순**. 0점
- "맛 차이가 확 난다" 문장에 "양념 푸기" 클립 → "김치" 공통이지만 "맛 = 먹는 행위/완성된 모습"이 필요, 1점

좋은 예 (의미 일치):
- "꾹 덮어준다" (솔루션) → 손이 단단한 뚜껑/누름판 누르는 클립 (비닐 랩 NO)
- "곰팡이 생긴다" (문제) → 곰팡이 핀 음식 클로즈업 (깨끗한 김치 NO)
- "맛이 미쳤다" (결과) → 먹는 장면/신선한 결과물 (조리 과정 NO)
- "납품 이모" (권위) → 대량/전문 주방 (가정 주방 NO)

[절대 규칙]
1. 각 컷의 **visual_intent**를 먼저 읽고, required_elements가 썸네일에 보이는 클립을 골라라.
2. **forbidden_elements가 보이는 클립은 매칭에서 제외.** 점수 0으로 취급.
3. **contrast_with_previous** 필드를 반드시 확인 — 앞 문장 대비 달라야 할 점. 이걸 위반하면 논리 붕괴.
4. **critical=true** 컷은 매칭 강도 9점 이상만 허용. 9점 이상 클립 없으면 가장 가까운 거 + reason에 "타협"이라고 명시.
5. **첫 컷 (cut 0, hook)**: visual_impact 8 이상 + retention 8 이상 클립만 사용. 충격 없는 클립으로 시작하면 시청자 이탈.
6. 같은 클립(같은 scene_id)을 한 영상에서 **최대 2번까지만** 사용. 3번째부터는 다른 클립 강제.
7. 같은 클립을 연속 2컷에 배치 금지. 같은 문장 내에서도 변화 줘라.
8. retention_value 가 낮은 (1-3) 클립은 가능한 피해라. 시청자 이탈 위험.
9. 안 쓰인 클립이 있으면 손해. 가능한 골고루.

[컷 목록 — visual_intent에 맞는 클립을 찾아라]
{json.dumps(enriched_cuts, ensure_ascii=False, indent=2)}

[사용 가능한 클립 — 아래에 썸네일 첨부]
"""

    for i, scene_id in enumerate(paths_ordered):
        a = clips_analysis[scene_id]
        retention = getattr(a, "retention_value", a.visual_impact)
        header += (
            f"\nC{i}: {scene_id}\n"
            f"  desc: {a.description}\n"
            f"  tags: {a.tags}\n"
            f"  emotion: {a.emotion} / impact: {a.visual_impact}/10 / retention: {retention}/10\n"
        )

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
    data = _generate_json_with_retry(client, GEMINI_TEXT_MODEL, parts, label="Stage2")

    for f in tmp_dir.glob("*"):
        f.unlink(missing_ok=True)
    tmp_dir.rmdir()
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
