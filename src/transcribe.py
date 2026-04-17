from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from faster_whisper import WhisperModel

from .config import WHISPER_COMPUTE, WHISPER_DEVICE, WHISPER_MODEL


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    language: str
    duration: float
    words: list[Word]

    def to_json(self) -> dict:
        return {
            "language": self.language,
            "duration": self.duration,
            "words": [asdict(w) for w in self.words],
        }


def transcribe(audio_path: Path, language: str = "ko") -> Transcript:
    model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
    )
    words: list[Word] = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            token = (w.word or "").strip()
            if not token:
                continue
            words.append(Word(start=float(w.start), end=float(w.end), text=token))
    return Transcript(language=info.language, duration=float(info.duration), words=words)


def save_transcript(transcript: Transcript, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(transcript.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def group_words_into_captions(
    words: Iterable[Word],
    max_words: int,
    max_chars: int,
    max_duration: float,
) -> list[dict]:
    captions: list[dict] = []
    bucket: list[Word] = []
    for w in words:
        if not bucket:
            bucket.append(w)
            continue
        text_len = sum(len(x.text) for x in bucket) + len(w.text)
        duration = w.end - bucket[0].start
        if (
            len(bucket) >= max_words
            or text_len > max_chars
            or duration > max_duration
        ):
            captions.append(_flush(bucket))
            bucket = [w]
        else:
            bucket.append(w)
    if bucket:
        captions.append(_flush(bucket))
    return captions


def _flush(bucket: list[Word]) -> dict:
    return {
        "start": bucket[0].start,
        "end": bucket[-1].end,
        "text": " ".join(w.text for w in bucket).strip(),
    }
