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
    pacing = style.get("pacing", {})
    avg_cut = float(pacing.get("avg_cut_length_sec", 1.5))
    shortest = float(pacing.get("shortest_cut_sec", 1.0))
    longest = float(pacing.get("longest_cut_sec", 3.0))

    total = transcript.duration
    points: list[float] = [0.0]
    words = transcript.words
    if not words:
        return [0.0, total]

    cursor = 0.0
    i = 0
    while cursor < total and i < len(words):
        target = cursor + avg_cut
        best_idx = i
        best_diff = float("inf")
        j = i
        while j < len(words) and words[j].end <= cursor + longest:
            if words[j].end <= cursor + shortest:
                j += 1
                continue
            diff = abs(words[j].end - target)
            if diff < best_diff:
                best_diff = diff
                best_idx = j
            j += 1
        if best_idx == i and words[i].end <= cursor + shortest:
            best_idx = min(j, len(words) - 1)
        cut_time = words[best_idx].end
        if cut_time - cursor < shortest:
            cut_time = cursor + shortest
        if cut_time > total:
            break
        points.append(round(cut_time, 3))
        cursor = cut_time
        i = best_idx + 1
    if points[-1] < total:
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


def assign_clips_by_ids(cut_points: list[float], ids: list[str], clips: list[Clip]) -> list[VideoSegment]:
    """Build VideoSegment list using explicit scene id per cut (from semantic matcher).

    Each id is a Clip.id (file#start-end). source_start/end in the resulting
    VideoSegment are absolute timestamps inside the source file.
    """
    by_id = {c.id: c for c in clips}
    segments: list[VideoSegment] = []
    revisit: dict[str, int] = {}
    for idx in range(len(cut_points) - 1):
        t_start = cut_points[idx]
        t_end = cut_points[idx + 1]
        needed = t_end - t_start
        scene_id = ids[idx]
        clip = by_id.get(scene_id) or clips[idx % len(clips)]
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

        segments.append(VideoSegment(
            clip=clip,
            source_start=round(src_start, 3),
            source_end=round(src_end, 3),
            timeline_start=round(t_start, 3),
            timeline_end=round(t_end, 3),
        ))
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
) -> EditPlan:
    clips = load_clips(clip_dir)
    cut_points = compute_cut_points(transcript, style)
    if cut_points[-1] < transcript.duration:
        cut_points.append(transcript.duration)
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
        print("  Matching scenes to cuts with Gemini...")
        scene_ids = match_clips(
            cuts=cuts_tuples,
            captions=captions_dicts,
            clips_analysis=clips_analysis,
            style=style,
            total_duration=transcript.duration,
            narration_text=narration_text,
            words=words_dicts,
        )
        if output_dir:
            from .matcher import _tag_cuts_with_captions
            cut_texts = _tag_cuts_with_captions(cuts_tuples, captions_dicts)
            save_matches(cuts_tuples, cut_texts, scene_ids, output_dir / "matches.json")
        segments = assign_clips_by_ids(cut_points, scene_ids, clips)
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
