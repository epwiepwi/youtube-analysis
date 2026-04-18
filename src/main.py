from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Callable

from .capcut_draft import copy_assets, write_draft
from .config import PATHS
from .plan import build_plan, save_plan
from .transcribe import save_transcript, transcribe


ProgressFn = Callable[[str, float], None]  # (message, fraction 0..1)


def load_style(profile_path: Path) -> dict:
    return json.loads(profile_path.read_text(encoding="utf-8"))


def _default_progress(msg: str, frac: float) -> None:
    print(msg)


def remove_silence(
    input_path: Path,
    output_path: Path,
    min_silence_sec: float = 0.2,
    threshold_db: int = -35,
) -> Path:
    """Strip silences from the narration in-place into `output_path`.

    Uses ffmpeg's silenceremove filter to cut every silence longer than
    `min_silence_sec` throughout the file (not just the start/end). The
    output preserves the original sample rate so Whisper handles it the
    same way.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_chain = (
        "silenceremove="
        f"stop_periods=-1:"
        f"stop_duration={min_silence_sec:.3f}:"
        f"stop_threshold={threshold_db}dB:"
        "start_periods=1:"
        f"start_duration=0:"
        f"start_threshold={threshold_db}dB"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(input_path),
        "-af", filter_chain,
        "-ar", "16000",
        "-ac", "1",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)
    return output_path


def run(
    narration: Path,
    clips_dir: Path,
    project_name: str,
    profile_path: Path,
    draft_root: Path,
    output_dir: Path,
    use_semantic: bool = False,
    reference_video: Path | None = None,
    use_selections: bool = False,
    progress: ProgressFn | None = None,
    strip_silence: bool = True,
) -> Path:
    cb = progress or _default_progress
    style = load_style(profile_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    working_narration = narration
    if strip_silence:
        cb("[0/4] Stripping silence from narration...", 0.02)
        cleaned = output_dir / f"{narration.stem}_nosilence.wav"
        try:
            remove_silence(narration, cleaned)
            working_narration = cleaned
            cb(f"  -> {cleaned.name}", 0.04)
        except subprocess.CalledProcessError as e:
            cb(f"  silence removal failed, using original ({e})", 0.04)

    cb("[1/4] Transcribing narration...", 0.05)
    transcript = transcribe(working_narration, language="ko")
    save_transcript(transcript, output_dir / "transcript.json")
    cb(f"  -> {len(transcript.words)} words, {transcript.duration:.2f}s", 0.20)

    cb("[2/4] Building edit plan...", 0.25)
    plan = build_plan(
        working_narration, transcript, clips_dir, style,
        use_semantic=use_semantic, output_dir=output_dir,
        reference_video=reference_video,
        use_selections=use_selections,
    )
    save_plan(plan, output_dir / "plan.json")
    cb(f"  -> {len(plan.video_segments)} cuts, {len(plan.captions)} captions", 0.80)

    cb("[3/4] Writing CapCut draft...", 0.85)
    project_dir = write_draft(project_name, plan, draft_root)
    plan = copy_assets(plan, project_dir)
    write_draft(project_name, plan, draft_root)
    cb(f"  -> {project_dir}", 0.95)

    cb("[4/4] Done. Open CapCut and find the project in your draft list.", 1.0)
    return project_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn narration + clips into a CapCut draft.")
    parser.add_argument("--narration", required=True, type=Path, help="Path to narration .wav/.mp3")
    parser.add_argument("--clips", required=True, type=Path, help="Folder with source video clips")
    parser.add_argument("--name", required=True, help="CapCut project name")
    parser.add_argument("--profile", type=Path, default=PATHS.profile)
    parser.add_argument("--draft-root", type=Path, default=PATHS.capcut_draft_root,
                        help="CapCut draft root folder (defaults to Windows CapCut path)")
    parser.add_argument("--output", type=Path, default=PATHS.output_root)
    parser.add_argument("--semantic", action="store_true",
                        help="Use Gemini to analyze clips and match them to captions semantically")
    parser.add_argument("--reference", type=Path, default=None,
                        help="Path to a successful reference short (.mp4) to imitate beat-by-beat")
    parser.add_argument("--use-selections", action="store_true",
                        help="Skip Gemini matching and use output/selections.json from the viewer")
    parser.add_argument("--keep-silence", action="store_true",
                        help="Skip silence-removal preprocessing on the narration audio")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    run(
        narration=args.narration,
        clips_dir=args.clips,
        project_name=args.name,
        profile_path=args.profile,
        draft_root=args.draft_root,
        output_dir=args.output,
        use_semantic=args.semantic,
        reference_video=args.reference,
        use_selections=args.use_selections,
        strip_silence=not args.keep_silence,
    )


if __name__ == "__main__":
    main()
