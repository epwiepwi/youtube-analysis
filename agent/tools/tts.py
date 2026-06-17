"""TTS 툴 — ElevenLabs로 음성 생성 + 문자 단위 타임스탬프 확보.

- 본 대본 앞에 THROWAWAY_INTRO(버리는 멘트)를 붙여서 워밍업 구간을 대신 받아낸다.
- with-timestamps 엔드포인트로 문자별 시작/끝 시간을 받아 자막(SRT) 타이밍에 사용.
- 반환하는 offset(초)은 '버리는 멘트'가 끝나는 지점 → 본 음성/자막은 이 지점부터.

키가 없으면 mock: 무음 mp3 비슷한 더미 파일과 추정 타임스탬프를 만들어 파이프라인을 잇는다.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import requests

from .. import config


def _eleven_with_timestamps(text: str) -> dict:
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/"
        f"{config.ELEVENLABS_VOICE_ID}/with-timestamps"
    )
    resp = requests.post(
        url,
        headers={
            "xi-api-key": config.ELEVENLABS_API_KEY,
            "content-type": "application/json",
        },
        json={
            "text": text,
            "model_id": config.ELEVENLABS_MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def _alignment_to_chars(alignment: dict) -> list[dict]:
    """{characters, character_start_times_seconds, character_end_times_seconds}
    → [{"ch", "start", "end"}, ...]"""
    chars = alignment.get("characters", [])
    starts = alignment.get("character_start_times_seconds", [])
    ends = alignment.get("character_end_times_seconds", [])
    out = []
    for i, ch in enumerate(chars):
        out.append(
            {
                "ch": ch,
                "start": starts[i] if i < len(starts) else 0.0,
                "end": ends[i] if i < len(ends) else 0.0,
            }
        )
    return out


def _intro_offset(char_times: list[dict], intro_len: int) -> float:
    """버리는 멘트(앞 intro_len 글자)가 끝나는 시각."""
    if intro_len <= 0 or not char_times:
        return 0.0
    idx = min(intro_len, len(char_times)) - 1
    return float(char_times[idx]["end"])


def _mock(script: str, out_path: Path, use_intro: bool) -> dict:
    # 더미 mp3 헤더만 가진 파일(실제 재생 X, 파이프라인 연결 확인용)
    out_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    intro = (config.THROWAWAY_INTRO + " ") if use_intro else ""
    full = intro + script
    # 글자당 0.08초로 가짜 타임스탬프
    per = 0.08
    char_times, t = [], 0.0
    for ch in full:
        char_times.append({"ch": ch, "start": round(t, 3), "end": round(t + per, 3)})
        t += per
    offset = _intro_offset(char_times, len(intro)) if use_intro else 0.0
    return {
        "audio_path": str(out_path),
        "char_times": char_times,
        "intro_offset": offset,
        "intro_len": len(intro),
        "duration": round(t, 3),
        "mode": "mock",
    }


def synthesize(script: str, use_intro: bool = True) -> dict:
    """대본 → 음성 mp3 + 타임스탬프.

    returns: {audio_path, char_times[list], intro_offset, intro_len, duration, mode}
    """
    out_path = config.OUTPUT_DIR / f"tts_{int(time.time())}.mp3"

    if not config.tts_available():
        return _mock(script, out_path, use_intro)

    intro = (config.THROWAWAY_INTRO + " ") if use_intro else ""
    full_text = intro + script

    data = _eleven_with_timestamps(full_text)
    audio_b64 = data.get("audio_base64", "")
    out_path.write_bytes(base64.b64decode(audio_b64))

    char_times = _alignment_to_chars(data.get("alignment") or {})
    offset = _intro_offset(char_times, len(intro)) if use_intro else 0.0
    duration = char_times[-1]["end"] if char_times else 0.0
    return {
        "audio_path": str(out_path),
        "char_times": char_times,
        "intro_offset": offset,
        "intro_len": len(intro),
        "duration": round(float(duration), 3),
        "mode": "live",
    }
