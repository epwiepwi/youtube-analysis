"""대본 툴 — 기존 '인스타 스크립트 도우미'를 병합한 단계.

벤치 영상의 자막/대본이나 거친 아이디어를 입력받아,
methodology.md(공감형 공식)에 맞춰 군더더기를 쳐낸 TTS용 완성 대본을 만든다.

provider 는 anthropic(Claude) 또는 gemini 를 지원.
키가 없으면 mock 으로 입력을 가볍게 정리만 해서 돌려준다(파이프라인 테스트용).
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

from .. import config

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "knowledge" / "methodology.md"


def _methodology() -> str:
    try:
        return _KNOWLEDGE.read_text(encoding="utf-8")
    except OSError:
        return ""


def _system_prompt() -> str:
    return (
        "너는 한국 인스타 릴스/숏폼 '공감형' 대본 전문가다. "
        "아래 방법론을 반드시 따른다. 광고/홍보/과장/명령형 표현을 제거하고, "
        "혀가 길어지는 군더더기 문장을 쳐내며, 친구한테 말하듯 짧은 구어체로 쓴다. "
        "TTS로 읽을 것이므로 발음하기 쉬운 문장으로, 자막 한 줄 호흡에 맞춰 끊는다.\n\n"
        + _methodology()
    )


def _user_prompt(source: str, product: str, extra: str) -> str:
    parts = [
        "다음 입력을 바탕으로 60초 분량의 완성된 TTS용 대본을 작성해라.",
        "출력은 순수한 대본 텍스트만. 설명/머리말/번호 없이 문장만 줄바꿈으로 구분.",
        "첫 문장은 반드시 '문제 제시 + 나만 그런가' 공감형 훅으로 시작.",
        "",
        f"[참고/벤치 대본 또는 아이디어]\n{source.strip() or '(없음 — 아래 제품 정보로 새로 작성)'}",
    ]
    if product.strip():
        parts.append(f"\n[소개할 제품/주제]\n{product.strip()}")
    if extra.strip():
        parts.append(f"\n[추가 요청]\n{extra.strip()}")
    return "\n".join(parts)


def _call_anthropic(system: str, user: str) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": config.ANTHROPIC_MODEL,
            "max_tokens": 2000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", [])).strip()


def _call_gemini(system: str, user: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        headers={"content-type": "application/json"},
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.8},
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    cands = data.get("candidates", [])
    if not cands:
        return ""
    return "".join(
        p.get("text", "") for p in cands[0].get("content", {}).get("parts", [])
    ).strip()


def _mock(source: str, product: str) -> str:
    base = source.strip() or product.strip() or "이거 나만 불편한 줄 알았는데"
    lines = [s.strip() for s in base.replace("\n", " ").split(".") if s.strip()]
    if not lines:
        lines = ["이거 나만 불편한 줄 알았는데"]
    return "\n".join(lines[:8])


def generate(source: str = "", product: str = "", extra: str = "") -> dict:
    """대본을 생성/정제해서 반환.

    returns: {"script": str, "mode": "llm"|"mock", "provider": str}
    """
    if not config.llm_available():
        return {"script": _mock(source, product), "mode": "mock", "provider": "none"}

    system, user = _system_prompt(), _user_prompt(source, product, extra)
    if config.LLM_PROVIDER == "gemini":
        script = _call_gemini(system, user)
    else:
        script = _call_anthropic(system, user)
    script = script.strip() or _mock(source, product)
    return {"script": script, "mode": "llm", "provider": config.LLM_PROVIDER}
