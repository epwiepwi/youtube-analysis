from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .transcribe import Transcript, group_words_into_captions


@dataclass
class Clip:
    """A usable visual unit. Either a whole file or a detected scene within one.

    `path` is the source file. `start`/`end` define the usable range inside
    that file. `file_duration` is the full file's length (needed by CapCut
    when generating the video material).
    """
    path: Path
    start: float
    end: float
    file_duration: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def id(self) -> str:
        return f"{self.path.name}#{self.start:.2f}-{self.end:.2f}"


@dataclass
class VideoSegment:
    clip: Clip
    source_start: float
    source_end: float
    timeline_start: float
    timeline_end: float
    speed: float = 1.0


@dataclass
class CaptionSegment:
    text: str
    start: float
    end: float


@dataclass
class EditPlan:
    narration_path: Path
    narration_duration: float
    video_segments: list[VideoSegment]
    captions: list[CaptionSegment]
    style: dict


def probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
    )
    return float(out.strip())


def load_files(clip_dir: Path) -> list[tuple[Path, float]]:
    """List source video files and their full durations."""
    files: list[tuple[Path, float]] = []
    for path in sorted(clip_dir.iterdir()):
        if path.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
            continue
        files.append((path, probe_duration(path)))
    if not files:
        raise RuntimeError(f"No video files found in {clip_dir}")
    return files


def detect_scenes(file_path: Path, file_duration: float,
                  threshold: float = 0.3, min_scene_sec: float = 1.5,
                  max_scene_sec: float = 8.0,
                  max_scenes: int = 25) -> list[Clip]:
    """Use ffmpeg's scene filter to split a source file into candidate scenes.

    Long scenes (continuous takes) are sub-divided into chunks of at most
    `max_scene_sec` so each candidate is short enough for a fast video upload
    and so a 6-minute continuous shot doesn't become a single useless scene.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-i", str(file_path),
        "-filter:v", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = proc.stderr or ""

    cut_times: list[float] = []
    for line in output.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            token = line.split("pts_time:")[1].split()[0]
            cut_times.append(float(token))
        except (IndexError, ValueError):
            continue

    boundaries = [0.0] + sorted(set(cut_times)) + [file_duration]
    scenes: list[Clip] = []
    for a, b in zip(boundaries, boundaries[1:]):
        if b - a < min_scene_sec:
            continue
        # Sub-divide long takes into max_scene_sec chunks.
        sub = a
        while sub < b:
            sub_end = min(sub + max_scene_sec, b)
            if sub_end - sub >= min_scene_sec:
                scenes.append(Clip(path=file_path, start=round(sub, 2),
                                   end=round(sub_end, 2),
                                   file_duration=file_duration))
            sub = sub_end

    if not scenes:
        step = max(min_scene_sec, min(max_scene_sec, file_duration / 8))
        t = 0.0
        while t + min_scene_sec <= file_duration and len(scenes) < max_scenes:
            scenes.append(Clip(path=file_path, start=round(t, 2),
                               end=round(min(t + step, file_duration), 2),
                               file_duration=file_duration))
            t += step

    if len(scenes) > max_scenes:
        stride = len(scenes) / max_scenes
        scenes = [scenes[int(i * stride)] for i in range(max_scenes)]

    return scenes


def load_clips(clip_dir: Path) -> list[Clip]:
    """Backward-compatible loader: returns a Clip per scene across all files."""
    out: list[Clip] = []
    for path, dur in load_files(clip_dir):
        out.extend(detect_scenes(path, dur))
    if not out:
        raise RuntimeError(f"No scenes detected in {clip_dir}")
    return out


def compute_cut_points(transcript: Transcript, style: dict) -> list[float]:
    """Choose cut points based on the speaker's natural pauses, not a clock.

    The old logic targeted ``cursor + avg_cut`` every iteration and snapped
    to the nearest word end, which routinely cut mid-sentence because the
    fixed cadence didn't care about meaning. Real짜집기 편집자는 호흡 끊기는
    지점에서 자른다 — so we collect every silence gap between consecutive
    words (a "breath"), then pick breaths inside the [shortest, longest]
    window from the previous cut, preferring the strongest (longest) pause
    closest to the average target. Only when no breath exists in range do
    we fall back to a forced cut at the latest word boundary.
    """
    pacing = style.get("pacing", {})
    avg_cut = float(pacing.get("avg_cut_length_sec", 1.5))
    shortest = float(pacing.get("shortest_cut_sec", 0.8))
    longest = float(pacing.get("longest_cut_sec", 3.5))
    breath_threshold = float(pacing.get("breath_threshold_sec", 0.15))

    total = transcript.duration
    words = transcript.words
    if not words:
        return [0.0, total]

    # Collect every speaker pause between consecutive words.
    # Each "breath" carries its strength (gap length) so longer pauses
    # — sentence-ish boundaries — win ties.
    breaths: list[tuple[float, float]] = []
    for prev, curr in zip(words, words[1:]):
        gap = curr.start - prev.end
        if gap >= breath_threshold:
            breaths.append((prev.end, gap))

    points: list[float] = [0.0]
    breath_idx = 0

    while points[-1] < total:
        last = points[-1]
        target_min = last + shortest
        target_max = last + longest
        if target_min >= total:
            break

        # Advance past breaths that are too early to count.
        while breath_idx < len(breaths) and breaths[breath_idx][0] < target_min:
            breath_idx += 1

        # Gather breaths inside the legal window.
        candidates: list[tuple[float, float]] = []
        i = breath_idx
        while i < len(breaths) and breaths[i][0] <= target_max:
            candidates.append(breaths[i])
            i += 1

        cut_time: float | None = None
        if candidates:
            # Pick the breath that combines a strong pause with proximity
            # to the style profile's average cut length. Heavily weight gap
            # strength; lightly penalize distance from the soft target.
            target = last + avg_cut
            cut_time = max(
                candidates,
                key=lambda b: b[1] * 4.0 - abs(b[0] - target),
            )[0]
        else:
            # No breath in window — cut at the latest legal word boundary.
            forced = [
                w.end for w in words
                if target_min <= w.end <= target_max
            ]
            if forced:
                cut_time = max(forced)

        if cut_time is None:
            # Truly nothing in range. Cap at the next word end after last+shortest
            # so we still progress, even if it pushes a bit past longest.
            next_word_end = next((w.end for w in words if w.end > target_min), total)
            cut_time = min(next_word_end, total)

        cut_time = min(cut_time, total)
        if cut_time <= last + 1e-3:
            break
        points.append(round(cut_time, 3))

    if points[-1] < total - 0.05:
        points.append(round(total, 3))
    return points


def assign_clips_to_cuts(cut_points: list[float], clips: list[Clip]) -> list[VideoSegment]:
    """Rotate through clips so each cut shows a different clip (짜집기 style).

    Picks a different clip for every cut. Within each clip, takes a slice from
    a rotating offset so revisits to the same clip don't show identical frames.
    """
    segments: list[VideoSegment] = []
    n = len(clips)
    revisit_count: dict[int, int] = {i: 0 for i in range(n)}

    for idx in range(len(cut_points) - 1):
        t_start = cut_points[idx]
        t_end = cut_points[idx + 1]
        needed = t_end - t_start
        clip_idx = idx % n
        clip = clips[clip_idx]
        revisit = revisit_count[clip_idx]
        revisit_count[clip_idx] += 1

        if clip.duration <= needed:
            src_start = clip.start
            src_end = clip.end
        else:
            slack = clip.duration - needed
            step = slack / max(1, (n if revisit > 0 else 2))
            offset = min(slack, revisit * step + slack * 0.1)
            src_start = clip.start + offset
            src_end = src_start + needed

        segments.append(
            VideoSegment(
                clip=clip,
                source_start=round(src_start, 3),
                source_end=round(src_end, 3),
                timeline_start=round(t_start, 3),
                timeline_end=round(t_end, 3),
            )
        )
    return segments


def build_captions(transcript: Transcript, style: dict) -> list[CaptionSegment]:
    captions_cfg = style.get("captions", {})
    max_words = int(captions_cfg.get("max_words_per_caption", 2))
    max_chars = int(captions_cfg.get("avg_chars_per_caption", 6)) + 4
    lead = 0.1 if "0.1" in str(captions_cfg.get("timing", "")) else 0.0
    raw = group_words_into_captions(
        transcript.words,
        max_words=max_words,
        max_chars=max_chars,
        max_duration=1.5,
    )
    out: list[CaptionSegment] = []
    for c in raw:
        start = max(0.0, c["start"] - lead)
        out.append(CaptionSegment(text=c["text"], start=round(start, 3), end=round(c["end"], 3)))
    return out


def assign_clips_by_ids(
    cut_points: list[float],
    ids: list[str],
    clips: list[Clip],
    ranked_by_cut: dict[int, list[dict]] | None = None,
    speed_min: float = 0.7,
    speed_max: float = 1.5,
) -> list[VideoSegment]:
    """Build VideoSegments from per-cut scene ids, fitting clips by speed.

    Old code truncated long clips (src_end = src_start + needed) which threw
    away most of the chosen scene and often left a slow, half-finished
    motion in the cut. Real editors stretch/compress the clip's playback
    speed to match the cut length instead — that preserves the whole
    chosen moment.

    For each cut we compute the required speed = clip.duration / needed.
    If it falls inside [speed_min, speed_max], we keep the entire scene
    and write `speed` into the segment so CapCut plays it back at that
    rate. If it's outside the range (clip too long-or-too-short to feel
    natural at any reasonable speed), we walk down the ranked candidate
    list for that cut and try the next picks. As a last resort we fall
    back to the original truncation behavior so we never end up with no
    segment at all.
    """
    by_id = {c.id: c for c in clips}
    segments: list[VideoSegment] = []
    revisit: dict[str, int] = {}

    def speed_in_range(clip: Clip, needed: float) -> float | None:
        if needed <= 0 or clip.duration <= 0:
            return None
        speed = clip.duration / needed
        if speed_min <= speed <= speed_max:
            return round(speed, 4)
        return None

    for idx in range(len(cut_points) - 1):
        t_start = cut_points[idx]
        t_end = cut_points[idx + 1]
        needed = t_end - t_start
        primary_id = ids[idx]

        # Build the ordered list of clips to try for this cut: the matcher's
        # top pick first, then alternates from the ranked list.
        tried_ids: list[str] = [primary_id]
        if ranked_by_cut and idx in ranked_by_cut:
            for pick in ranked_by_cut[idx]:
                sid = pick.get("scene_id")
                if sid and sid not in tried_ids and sid in by_id:
                    tried_ids.append(sid)

        chosen: VideoSegment | None = None
        for sid in tried_ids:
            clip = by_id.get(sid)
            if not clip:
                continue
            speed = speed_in_range(clip, needed)
            if speed is None:
                continue
            # Use the entire scene; CapCut will play it back at `speed`.
            chosen = VideoSegment(
                clip=clip,
                source_start=round(clip.start, 3),
                source_end=round(clip.end, 3),
                timeline_start=round(t_start, 3),
                timeline_end=round(t_end, 3),
                speed=speed,
            )
            break

        if chosen is None:
            # Fallback: every candidate was outside speed range. Truncate
            # the primary pick so the timeline still has a clip there.
            clip = by_id.get(primary_id) or clips[idx % len(clips)]
            visits = revisit.get(clip.id, 0)
            revisit[clip.id] = visits + 1
            if clip.duration <= needed:
                src_start = clip.start
                src_end = clip.end
            else:
                slack = clip.duration - needed
                offset = min(slack, visits * (slack / 3) + slack * 0.1)
                src_start = clip.start + offset
                src_end = src_start + needed
            chosen = VideoSegment(
                clip=clip,
                source_start=round(src_start, 3),
                source_end=round(src_end, 3),
                timeline_start=round(t_start, 3),
                timeline_end=round(t_end, 3),
                speed=1.0,
            )

        segments.append(chosen)
    return segments


# Back-compat alias.
assign_clips_by_paths = assign_clips_by_ids


def build_plan(
    narration_path: Path,
    transcript: Transcript,
    clip_dir: Path,
    style: dict,
    use_semantic: bool = False,
    output_dir: Path | None = None,
    reference_video: Path | None = None,
    use_selections: bool = False,
) -> EditPlan:
    clips = load_clips(clip_dir)
    cut_points = compute_cut_points(transcript, style)
    if cut_points[-1] < transcript.duration:
        cut_points.append(transcript.duration)

    reference_template = None
    reference_beats_per_cut: list = []
    if reference_video:
        from .reference import align_to_user_timeline, analyze_reference
        cache = (output_dir or Path("output")) / "reference_template.json"
        print(f"  Analyzing reference video {reference_video.name}...")
        reference_template = analyze_reference(reference_video, cache_path=cache)
        # Adopt reference's cut count: if reference has 16 beats in 26s and
        # user narration is 28s, scale to 16 * 28/26 ~ 17 cuts positioned
        # proportionally (snap to nearest word boundary for natural feel).
        if reference_template.beats:
            ref_total = reference_template.total_duration
            ref_beats = reference_template.beats
            scale = transcript.duration / max(1e-6, ref_total)
            scaled_points = [0.0]
            for b in ref_beats:
                t = min(transcript.duration, b.end * scale)
                if t > scaled_points[-1] + 0.4:
                    scaled_points.append(round(t, 3))
            if scaled_points[-1] < transcript.duration - 0.1:
                scaled_points.append(round(transcript.duration, 3))
            # Snap each interior point to the nearest word end.
            word_ends = [w.end for w in transcript.words]
            snapped = [0.0]
            for p in scaled_points[1:-1]:
                if not word_ends:
                    snapped.append(p)
                    continue
                closest = min(word_ends, key=lambda w: abs(w - p))
                if abs(closest - p) < 0.5 and closest > snapped[-1] + 0.4:
                    snapped.append(round(closest, 3))
                elif p > snapped[-1] + 0.4:
                    snapped.append(p)
            snapped.append(round(transcript.duration, 3))
            cut_points = snapped
        reference_beats_per_cut = align_to_user_timeline(
            reference_template, transcript.duration, cut_points,
        )

    captions = build_captions(transcript, style)

    if use_semantic:
        from .matcher import match_clips, save_matches
        from .vision import build_clips_index

        cache_path = (output_dir or Path("output")) / "clips_index.json"
        print(f"  Analyzing {len(clips)} scenes (from {len(set(c.path for c in clips))} files) with Gemini Vision...")
        clips_analysis = build_clips_index(clips, cache_path)

        cuts_tuples = [(cut_points[i], cut_points[i + 1]) for i in range(len(cut_points) - 1)]
        captions_dicts = [{"text": c.text, "start": c.start, "end": c.end} for c in captions]
        narration_text = " ".join(w.text for w in transcript.words)
        words_dicts = [{"start": w.start, "end": w.end, "text": w.text} for w in transcript.words]
        ref_beats_payload: list | None = None
        if reference_template and reference_beats_per_cut:
            ref_beats_payload = []
            for i, b in enumerate(reference_beats_per_cut):
                ref_beats_payload.append({
                    "cut_index": i,
                    "reference_spoken": b.spoken,
                    "reference_visual": b.visual_description,
                    "reference_elements": b.visual_elements,
                    "reference_emotion": b.emotion,
                    "reference_phase": b.phase,
                })
        from .viewer import (build_recommendations, load_selections,
                             save_recommendations, write_viewer_html)

        selections = load_selections(output_dir) if (output_dir and use_selections) else {}

        if use_selections and selections:
            # Skip Gemini matching entirely; replay the user's picks.
            recs_path = (output_dir or Path("output")) / "recommendations.json"
            prior: list[dict] = []
            if recs_path.exists():
                parsed = json.loads(recs_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    prior = parsed.get("recommendations", [])
                elif isinstance(parsed, list):
                    prior = parsed
            scene_ids: list[str] = []
            for i in range(len(cuts_tuples)):
                chosen = selections.get(i)
                if chosen and chosen in clips_analysis:
                    scene_ids.append(chosen)
                elif prior and i < len(prior) and prior[i].get("candidates"):
                    scene_ids.append(prior[i]["candidates"][0]["scene_id"])
                else:
                    scene_ids.append(list(clips_analysis.keys())[i % len(clips_analysis)])
            print(f"  Applied {len(selections)} user selection(s); skipped Gemini matching.")
        else:
            print("  Matching scenes to cuts with Gemini...")
            scene_ids = match_clips(
                cuts=cuts_tuples,
                captions=captions_dicts,
                clips_analysis=clips_analysis,
                style=style,
                total_duration=transcript.duration,
                narration_text=narration_text,
                words=words_dicts,
                reference_beats=ref_beats_payload,
            )
            if output_dir:
                from .matcher import _tag_cuts_with_captions, match_clips as _mc
                cut_texts = _tag_cuts_with_captions(cuts_tuples, captions_dicts)
                save_matches(cuts_tuples, cut_texts, scene_ids, output_dir / "matches.json")
                ranked = getattr(_mc, "last_ranked", {})
                cut_entries = getattr(_mc, "last_cut_entries", [])
                if ranked and cut_entries:
                    print("  Building recommendations + viewer.html...")
                    recs, all_scenes = build_recommendations(
                        cut_entries, ranked, clips_analysis, output_dir,
                    )
                    save_recommendations(recs, all_scenes, output_dir)
                    viewer = write_viewer_html(recs, all_scenes, output_dir)
                    print(f"  -> open {viewer} to review and pick alternates")
        # Pass the ranked candidate list so speed-fitting can fall back
        # to the next pick when the primary clip is too long/short.
        from .matcher import match_clips as _mc_for_ranked
        ranked_for_fitting = getattr(_mc_for_ranked, "last_ranked", None)
        segments = assign_clips_by_ids(
            cut_points, scene_ids, clips, ranked_by_cut=ranked_for_fitting,
        )
    else:
        segments = assign_clips_to_cuts(cut_points, clips)

    return EditPlan(
        narration_path=narration_path,
        narration_duration=transcript.duration,
        video_segments=segments,
        captions=captions,
        style=style,
    )


def save_plan(plan: EditPlan, out_path: Path) -> None:
    payload = {
        "narration_path": str(plan.narration_path),
        "narration_duration": plan.narration_duration,
        "video_segments": [
            {
                "clip": str(s.clip.path),
                "source_start": s.source_start,
                "source_end": s.source_end,
                "timeline_start": s.timeline_start,
                "timeline_end": s.timeline_end,
            }
            for s in plan.video_segments
        ],
        "captions": [
            {"text": c.text, "start": c.start, "end": c.end} for c in plan.captions
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
