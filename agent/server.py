"""에이전트 로컬 서버 (FastAPI).

브라우저 UI를 띄우고, 단계별 툴을 API로 노출한다.
실행: python -m agent.server   (또는 run.bat)
"""
from __future__ import annotations

import time
import webbrowser
from pathlib import Path
from threading import Timer

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .tools import capcut, script as script_tool, subtitles, tts

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

app = FastAPI(title="숏폼 자동화 에이전트")
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

# 세션 상태(메모리). 단일 사용자 로컬 앱이라 단순 dict로 충분.
SESSIONS: dict[str, dict] = {}


# ---------- 요청 모델 ----------
class ScriptReq(BaseModel):
    source: str = ""
    product: str = ""
    extra: str = ""


class TtsReq(BaseModel):
    script: str
    use_intro: bool = True


class ExportReq(BaseModel):
    session_id: str
    segments: list[dict]


# ---------- 페이지 ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.get("/analysis", response_class=HTMLResponse)
def analysis():
    """원본 두유노홈디노 분석 리포트(공식의 근거)."""
    f = ROOT / "index (1).html"
    if f.exists():
        return f.read_text(encoding="utf-8")
    return HTMLResponse("<h1>분석 리포트 파일 없음</h1>", status_code=404)


@app.get("/api/status")
def status():
    return {
        "llm": config.llm_available(),
        "llm_provider": config.LLM_PROVIDER,
        "tts": config.tts_available(),
        "capcut_dir": bool(config.CAPCUT_DRAFT_DIR),
    }


# ---------- 1. 대본 ----------
@app.post("/api/script")
def api_script(req: ScriptReq):
    try:
        return script_tool.generate(req.source, req.product, req.extra)
    except Exception as e:
        raise HTTPException(500, f"대본 생성 오류: {e}")


# ---------- 2. TTS → 자막 세그먼트 ----------
@app.post("/api/tts")
def api_tts(req: TtsReq):
    if not req.script.strip():
        raise HTTPException(400, "대본이 비어있음")
    try:
        result = tts.synthesize(req.script, use_intro=req.use_intro)
        segs = subtitles.segment_from_tts(result)
        sid = f"s{int(time.time()*1000)}"
        SESSIONS[sid] = {"tts": result, "segments": segs}
        return {
            "session_id": sid,
            "audio_url": f"/api/audio/{sid}",
            "duration": result["duration"],
            "intro_offset": result["intro_offset"],
            "mode": result["mode"],
            "segments": segs,
        }
    except Exception as e:
        raise HTTPException(500, f"TTS 오류: {e}")


@app.get("/api/audio/{sid}")
def api_audio(sid: str):
    sess = SESSIONS.get(sid)
    if not sess:
        raise HTTPException(404, "세션 없음")
    return FileResponse(sess["tts"]["audio_path"], media_type="audio/mpeg")


# ---------- 3+4. 자막 확정 → SRT + 캡컷 드래프트 ----------
@app.post("/api/export")
def api_export(req: ExportReq):
    sess = SESSIONS.get(req.session_id)
    if not sess:
        raise HTTPException(404, "세션 없음. TTS부터 다시.")
    try:
        srt_text = subtitles.build_srt(req.segments)
        srt_path = config.OUTPUT_DIR / f"{req.session_id}.srt"
        subtitles.save_srt(srt_text, srt_path)

        result = sess["tts"]
        draft = capcut.build_draft(
            audio_path=result["audio_path"],
            segments=req.segments,
            intro_offset=result["intro_offset"],
            audio_duration=result["duration"],
            project_name=req.session_id,
        )
        return {
            "srt_text": srt_text,
            "srt_url": f"/api/srt/{req.session_id}",
            "audio_path": result["audio_path"],
            "capcut": draft,
        }
    except Exception as e:
        raise HTTPException(500, f"내보내기 오류: {e}")


@app.get("/api/srt/{sid}")
def api_srt(sid: str):
    p = config.OUTPUT_DIR / f"{sid}.srt"
    if not p.exists():
        raise HTTPException(404, "SRT 없음")
    return FileResponse(p, media_type="text/plain", filename=f"{sid}.srt")


def _open_browser():
    webbrowser.open("http://127.0.0.1:8765")


def main():
    import uvicorn

    Timer(1.2, _open_browser).start()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
