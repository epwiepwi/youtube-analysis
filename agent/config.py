"""환경설정 로더. 모든 API 키/경로는 .env 에서 읽는다.

키가 없으면 각 툴은 '목업(mock) 모드'로 동작한다 → UI/파이프라인을 키 없이도 끝까지 돌려볼 수 있다.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 프로젝트 루트 (.../youtube-analysis)
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# ---- 출력 폴더 ----
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", ROOT / "output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- LLM (대본 생성/정제) ----
# provider: "anthropic" | "gemini"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ---- ElevenLabs (TTS) ----
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# TTS 초반 '구린 음성' 잘라내기용 버리는 멘트.
# 이 멘트가 워밍업 구간(앞 몇 초)을 대신 받아내고, 이후 본 대본만 사용한다.
THROWAWAY_INTRO = os.getenv("THROWAWAY_INTRO", "음... 자, 그럼 시작해볼게요.")

# ---- 캡컷 ----
# 윈도우 기본 경로 예: %LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft
CAPCUT_DRAFT_DIR = os.getenv("CAPCUT_DRAFT_DIR", "")

# ---- 영상 규격 ----
VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "1080"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "1920"))


def llm_available() -> bool:
    if LLM_PROVIDER == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    if LLM_PROVIDER == "gemini":
        return bool(GEMINI_API_KEY)
    return False


def tts_available() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)
