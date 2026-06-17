"""캡컷 드래프트 툴 — 자막 트랙 + TTS 오디오 트랙이 미리 깔린 드래프트를 생성.

사용자는 캡컷에서 이 드래프트를 열고 '메인 영상'만 올리면 된다.
(자막/TTS는 이미 트랙으로 들어가 있음)

- CAPCUT_DRAFT_DIR 설정 시: DraftFolder.create_draft 로 캡컷이 바로 인식하는 폴더 생성.
- 미설정 시: output/<프로젝트>/draft_content.json 으로 덤프(수동 위치 이동용).
- TTS intro_offset 만큼 오디오 소스를 잘라 '버리는 멘트'를 제외.

pyJianYingDraft(==0.2.6 기준)에 의존. 캡컷 버전에 따라 호환이 안 될 수 있어
방어적으로 감쌌고, 실패해도 SRT/MP3 산출물은 별도로 남는다.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import config

FPS = 30


def build_draft(
    audio_path: str,
    segments: list[dict],
    intro_offset: float = 0.0,
    audio_duration: float | None = None,
    project_name: str | None = None,
) -> dict:
    """드래프트 생성. returns {ok, path|error, mode}."""
    project_name = project_name or f"shorts_{int(time.time())}"

    try:
        import pyJianYingDraft as draft
    except Exception as e:  # 미설치 등
        return {
            "ok": False,
            "mode": "skipped",
            "error": f"pyJianYingDraft 미설치/로드 실패: {e}. "
            f"SRT/MP3는 정상 산출됨 — 캡컷에서 수동 임포트 가능.",
        }

    try:
        W, H = config.VIDEO_WIDTH, config.VIDEO_HEIGHT

        # --- 드래프트 생성 위치 ---
        if config.CAPCUT_DRAFT_DIR:
            folder = draft.DraftFolder(config.CAPCUT_DRAFT_DIR)
            script = folder.create_draft(project_name, W, H, FPS, allow_replace=True)
            out_path = str(Path(config.CAPCUT_DRAFT_DIR) / project_name)
            saver = script.save
        else:
            script = draft.ScriptFile(W, H, FPS, True)
            proj_dir = config.OUTPUT_DIR / project_name
            proj_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(proj_dir)
            saver = lambda: script.dump(str(proj_dir / "draft_content.json"))

        script.add_track(draft.TrackType.audio)
        script.add_track(draft.TrackType.text)

        # --- 오디오: intro_offset 이후만 사용 ---
        material = draft.AudioMaterial(audio_path)
        total = audio_duration or (getattr(material, "duration", 0) / 1_000_000)
        used = max(0.1, total - intro_offset)
        seg = draft.AudioSegment(
            material,
            draft.trange(0.0, used),
            source_timerange=draft.trange(intro_offset, used),
        )
        script.add_segment(seg)

        # --- 자막 텍스트 트랙 ---
        for s in segments:
            dur = max(0.1, float(s["end"]) - float(s["start"]))
            tseg = draft.TextSegment(
                s["text"].strip(), draft.trange(float(s["start"]), dur)
            )
            script.add_segment(tseg)

        saver()
        return {"ok": True, "mode": "live", "path": out_path}
    except Exception as e:
        return {
            "ok": False,
            "mode": "error",
            "error": f"드래프트 생성 실패: {e}. SRT/MP3로 수동 임포트하세요.",
        }
