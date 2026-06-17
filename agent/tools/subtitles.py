"""자막 툴 — TTS 타임스탬프로 자막 세그먼트와 SRT를 만든다.

- 버리는 멘트(intro)는 건너뛰고, 본 대본 구간만 자막으로.
- 모든 시간은 intro_offset 만큼 빼서 '트리밍된 음성' 기준 0초부터 시작.
- 사용자는 UI에서 각 세그먼트의 '텍스트(오타)'만 고치고, 타이밍은 유지한 채 SRT 재생성.
"""
from __future__ import annotations

from pathlib import Path

# 문장 종료 부호 / 끊는 기준
_BREAK_CHARS = set(".!?…。！？\n")
_MAX_LEN = 28  # 자막 한 줄 최대 글자(대략)


def segment_from_tts(tts_result: dict) -> list[dict]:
    """tts.synthesize() 결과 → [{index, start, end, text}, ...]"""
    char_times = tts_result.get("char_times", [])
    intro_len = tts_result.get("intro_len", 0)
    offset = float(tts_result.get("intro_offset", 0.0))

    real = char_times[intro_len:]
    segments: list[dict] = []
    buf: list[dict] = []

    def flush():
        if not buf:
            return
        text = "".join(c["ch"] for c in buf).strip()
        if text:
            segments.append(
                {
                    "start": max(0.0, round(buf[0]["start"] - offset, 3)),
                    "end": max(0.0, round(buf[-1]["end"] - offset, 3)),
                    "text": text,
                }
            )
        buf.clear()

    for c in real:
        buf.append(c)
        ch = c["ch"]
        cur_len = len("".join(x["ch"] for x in buf).strip())
        if ch in _BREAK_CHARS:
            flush()
        elif cur_len >= _MAX_LEN and ch == " ":
            flush()
    flush()

    for i, s in enumerate(segments, start=1):
        s["index"] = i
    return segments


def _fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(segments: list[dict]) -> str:
    """[{start, end, text}] → SRT 문자열."""
    blocks = []
    for i, seg in enumerate(segments, start=1):
        blocks.append(
            f"{i}\n{_fmt_ts(seg['start'])} --> {_fmt_ts(seg['end'])}\n{seg['text'].strip()}\n"
        )
    return "\n".join(blocks)


def save_srt(srt_text: str, path: str | Path) -> str:
    p = Path(path)
    p.write_text(srt_text, encoding="utf-8")
    return str(p)
