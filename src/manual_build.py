"""Build a CapCut draft directly from a manually-authored scene timeline.

Bypasses all AI matching. Use when the user has hand-crafted the cut
list and wants the pipeline to just assemble it exactly as specified.

Timeline JSON format:
{
  "capcut_editing_timeline": [
    {
      "scene_number": 1,
      "narration_sentence": "...",
      "source_file_name": "clip.mp4",
      "extract_start_sec": 0.0,
      "extract_end_sec": 3.0,
      ...
    },
    ...
  ]
}
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .capcut_draft import copy_assets, write_draft
from .config import PATHS
from .plan import (CaptionSegment, Clip, EditPlan, VideoSegment,
                    probe_duration)


def _index_clips(clips_dir: Path) -> dict[str, Path]:
    exts = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
    return {
        p.name: p
        for p in sorted(clips_dir.iterdir())
        if p.is_file() and p.suffix.lower() in exts
    }


def _find_source(requested: str, index: dict[str, Path]) -> Path | None:
    """Match the timeline's source_file_name to an actual file.

    Tries exact match first, then case-insensitive, then a loose
    substring fallback so renames/copies during staging don't break the
    lookup.
    """
    if requested in index:
        return index[requested]
    lower = {k.lower(): v for k, v in index.items()}
    if requested.lower() in lower:
        return lower[requested.lower()]
    stem = Path(requested).stem.lower()
    for name, path in index.items():
        path_stem = path.stem.lower()
        if stem and (stem in path_stem or path_stem in stem):
            return path
    return None


def build_from_timeline(
    timeline_path: Path,
    narration_path: Path,
    clips_dir: Path,
    project_name: str,
    draft_root: Path,
    profile_path: Path,
) -> Path:
    payload = json.loads(timeline_path.read_text(encoding="utf-8"))
    scenes = payload.get("capcut_editing_timeline") or payload.get("scenes") or []
    if not scenes:
        raise RuntimeError(
            "Timeline JSON has no 'capcut_editing_timeline' entries."
        )

    clip_index = _index_clips(clips_dir)
    if not clip_index:
        raise RuntimeError(f"No video files found under {clips_dir}")

    segments: list[VideoSegment] = []
    captions: list[CaptionSegment] = []
    cursor = 0.0
    skipped: list[str] = []

    for scene in scenes:
        name = str(scene.get("source_file_name", "")).strip()
        if not name:
            continue
        source = _find_source(name, clip_index)
        if not source:
            skipped.append(name)
            continue

        src_start = float(scene.get("extract_start_sec", 0.0))
        src_end = float(scene.get("extract_end_sec", src_start))
        duration = max(0.1, src_end - src_start)

        file_dur = probe_duration(source)
        clip = Clip(
            path=source, start=0.0, end=file_dur, file_duration=file_dur
        )

        segments.append(VideoSegment(
            clip=clip,
            source_start=round(src_start, 3),
            source_end=round(src_end, 3),
            timeline_start=round(cursor, 3),
            timeline_end=round(cursor + duration, 3),
            speed=1.0,
        ))

        sentence = str(scene.get("narration_sentence", "")).strip()
        if sentence:
            captions.append(CaptionSegment(
                text=sentence,
                start=round(cursor, 3),
                end=round(cursor + duration, 3),
            ))

        print(
            f"  scene {scene.get('scene_number', '?')}: "
            f"{source.name}  {src_start:.1f}-{src_end:.1f}s  "
            f"-> timeline {cursor:.1f}-{cursor + duration:.1f}s"
        )
        cursor += duration

    if not segments:
        raise RuntimeError(
            "No segments could be built — every source_file_name was missing. "
            f"Available files: {sorted(clip_index.keys())}"
        )
    if skipped:
        print(f"\n  [warn] {len(skipped)} source(s) not found, skipped:")
        for name in skipped:
            print(f"    - {name}")

    style = json.loads(profile_path.read_text(encoding="utf-8"))
    plan = EditPlan(
        narration_path=narration_path,
        narration_duration=cursor,
        video_segments=segments,
        captions=captions,
        style=style,
    )

    project_dir = write_draft(project_name, plan, draft_root)
    plan = copy_assets(plan, project_dir)
    # Rewrite once the asset paths point inside the project Resources dir.
    write_draft(project_name, plan, draft_root)
    return project_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a CapCut draft directly from a manual timeline JSON."
    )
    parser.add_argument("--timeline", required=True, type=Path,
                        help="Path to the timeline JSON file.")
    parser.add_argument("--narration", required=True, type=Path,
                        help="Path to the narration audio (.wav/.mp3).")
    parser.add_argument("--clips", required=True, type=Path,
                        help="Folder containing the source video files.")
    parser.add_argument("--name", required=True,
                        help="CapCut project name (becomes the draft folder).")
    parser.add_argument("--draft-root", type=Path,
                        default=PATHS.capcut_draft_root,
                        help="CapCut draft root (defaults to Windows install).")
    parser.add_argument("--profile", type=Path, default=PATHS.profile,
                        help="Style profile JSON (font/color/etc).")
    args = parser.parse_args()

    project_dir = build_from_timeline(
        timeline_path=args.timeline,
        narration_path=args.narration,
        clips_dir=args.clips,
        project_name=args.name,
        draft_root=args.draft_root,
        profile_path=args.profile,
    )
    print(f"\nDone: {project_dir}\nOpen CapCut to find the project.")


if __name__ == "__main__":
    main()
