"""Reference video analyzer.

Takes a successful reference short (already-edited mp4) and extracts a
beat-by-beat recipe: cut boundaries, what's spoken during each cut, and what
the screen shows. The rest of the pipeline can then try to reproduce that
recipe with the user's own clips, instead of inventing editing decisions.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from google import genai
from google.genai import types

from .config import GEMINI_API_KEY, GEMINI_VISION_MODEL
from .transcribe import transcribe
from .vision import _generate_with_retry, _parse_json, _safe_int


@dataclass
class ReferenceBeat:
    index: int
    start: float
    end: float
    duration: float
    spoken: str
    visual_description: str
    visual_elements: list[str] = field(default_factory=list)
    emotion: str = "neutral"
    phase: str = "body"
    retention_value: int = 5


@dataclass
class ReferenceTemplate:
    video_path: str
    total_duration: float
    narration_text: str
    sentences: list[dict]
    beats: list[ReferenceBeat]

    def to_json(self) -> dict:
        return {
            "video_path": self.video_path,
            "total_duration": self.total_duration,
            "narration_text": self.narration_text,
            "sentences": self.sentences,
            "beats": [asdict(b) for b in self.beats],
        }


REFERENCE_PROMPT = """이 영상은 이미 편집된 '성공한 쇼츠'다. 너는 이 영상을 비트 단위로 분해해서
다른 사람이 똑같이 모방할 수 있는 레시피를 만드는 편집 분석가다.

[작업 규칙]
1. 화면이 바뀌는 지점(컷 전환)을 모두 찾아라.
2. 각 컷마다 다음을 기록:
   - start_sec, end_sec (초 단위, 소수점 둘째 자리까지)
   - spoken: 그 시간대에 나레이터가 실제로 말하는 원문
   - visual_description: 화면에 무엇이 보이는지 2-3문장 (인물 행동/사물/색감/분위기 포함)
   - visual_elements: 구체적 시각 요소 키워드 4-8개
   - emotion: shock/warning/calm/clean/disgusting/action/mundane/satisfaction
   - phase: hook | problem | solution | cta | bridge
   - retention_value: 1-10 (시청자가 이 구간에서 계속 볼 가능성)
3. 컷 1초 미만이어도 실제 전환이면 별개로 기록.
4. 동일한 소스의 미세 움직임은 컷 아님 — 화면이 실제로 바뀐 것만.

[출력 스키마]
{
  "total_duration_sec": 0.0,
  "narration_full_text": "영상 처음부터 끝까지 모든 나레이션/대사",
  "sentences": [
    {"index": 0, "start_sec": 0.00, "end_sec": 0.00, "text": "문장 원문", "intent": "..."}
  ],
  "beats": [
    {
      "index": 0,
      "start_sec": 0.00,
      "end_sec": 0.00,
      "spoken": "이 구간에 들리는 말",
      "visual_description": "화면에 보이는 것 서술",
      "visual_elements": ["키워드 4-8개"],
      "emotion": "shock",
      "phase": "hook",
      "retention_value": 9
    }
  ]
}

JSON만 출력. 다른 설명 금지."""


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True,
    )
    return float(out.strip())


def _compress_for_upload(video_path: Path) -> Path:
    """Lightly re-encode so inline upload stays under 20MB even for longer refs."""
    tmp = Path(tempfile.mkdtemp(prefix="refvid_"))
    out = tmp / "reference.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_path),
            "-vf", "scale='min(540,iw)':-2,fps=24",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(out),
        ],
        check=True,
    )
    return out


def analyze_reference(video_path: Path, cache_path: Path | None = None) -> ReferenceTemplate:
    """Run Gemini over the reference mp4 and return a beat-by-beat recipe."""
    if cache_path and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            beats = [ReferenceBeat(**b) for b in data.get("beats", [])]
            return ReferenceTemplate(
                video_path=data["video_path"],
                total_duration=float(data["total_duration"]),
                narration_text=data.get("narration_text", ""),
                sentences=data.get("sentences", []),
                beats=beats,
            )
        except Exception:
            pass

    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set for reference analysis.")
    client = genai.Client(api_key=GEMINI_API_KEY)

    duration = _probe_duration(video_path)
    compressed = _compress_for_upload(video_path)

    print(f"  Analyzing reference video ({duration:.1f}s) with Gemini...")
    parts = [
        REFERENCE_PROMPT,
        types.Part.from_bytes(data=compressed.read_bytes(), mime_type="video/mp4"),
    ]
    resp = _generate_with_retry(client, GEMINI_VISION_MODEL, parts)
    data = _parse_json(resp.text)

    try:
        compressed.unlink(missing_ok=True)
        compressed.parent.rmdir()
    except OSError:
        pass

    beats: list[ReferenceBeat] = []
    for b in data.get("beats", []):
        try:
            start = float(b["start_sec"])
            end = float(b["end_sec"])
            beats.append(ReferenceBeat(
                index=int(b.get("index", len(beats))),
                start=start,
                end=end,
                duration=max(0.0, end - start),
                spoken=str(b.get("spoken", "")),
                visual_description=str(b.get("visual_description", "")),
                visual_elements=[str(x) for x in b.get("visual_elements", [])],
                emotion=str(b.get("emotion", "neutral")),
                phase=str(b.get("phase", "body")),
                retention_value=_safe_int(b.get("retention_value"), 5),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    beats.sort(key=lambda x: x.start)

    template = ReferenceTemplate(
        video_path=str(video_path),
        total_duration=float(data.get("total_duration_sec") or duration),
        narration_text=str(data.get("narration_full_text", "")),
        sentences=list(data.get("sentences", [])),
        beats=beats,
    )

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(template.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return template


def align_to_user_timeline(template: ReferenceTemplate, user_duration: float,
                           user_cut_points: list[float]) -> list[ReferenceBeat]:
    """For each user cut, pick the reference beat at the same fractional position.

    Returns a list of ReferenceBeat aligned 1:1 with user cuts. If user has more
    cuts than reference, the tail reuses the closest beat.
    """
    if not template.beats:
        return []
    ref_total = max(1e-6, template.total_duration)
    out: list[ReferenceBeat] = []
    for i in range(len(user_cut_points) - 1):
        cut_start = user_cut_points[i]
        cut_end = user_cut_points[i + 1]
        mid = (cut_start + cut_end) / 2
        frac = mid / max(1e-6, user_duration)
        target_t = frac * ref_total
        # Find beat whose range contains target_t, else the closest mid-point.
        best = min(
            template.beats,
            key=lambda b: abs(((b.start + b.end) / 2) - target_t),
        )
        for b in template.beats:
            if b.start <= target_t <= b.end:
                best = b
                break
        out.append(best)
    return out
