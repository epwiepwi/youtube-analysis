# 🎬 숏폼 자동화 에이전트

벤치 영상 → 대본 → TTS → 자막 → **캡컷 드래프트**까지 한 프로그램에서.
캡컷을 열면 자막·TTS 트랙이 이미 깔려 있어서, **메인 영상만 올리면** 편집이 끝나도록 만드는 게 목표.

> 이건 전체 비전의 **코어 MVP(1·2·5·6단계)**입니다.
> 이후 영상검색 → 화질 업스케일/중국어 자막 제거 → 캡션 추천 → 인포크/머닝 등록을 같은 틀에 툴로 추가합니다.

## 단계
1. **대본** — 기존 인스타 스크립트 도우미를 병합. 벤치 자막/아이디어 → 공감형 대본 (방법론은 `agent/knowledge/methodology.md`, 근거는 `/analysis` 리포트). 완성 대본 붙여넣기도 가능.
2. **TTS** — ElevenLabs. 앞에 '버리는 멘트'를 넣어 초반 구린 음성을 대신 받아내고 본 음성만 사용.
3. **자막** — TTS 타임스탬프로 자동 분할. 오타만 수정(타이밍 유지) → SRT.
4. **캡컷** — 자막 트랙 + TTS 오디오 트랙이 들어간 드래프트 생성.

## 실행 (윈도우)
```bat
run.bat
```
처음 실행하면 `.env` 가 생깁니다. 키를 채우세요 (없어도 **목업 모드**로 UI는 끝까지 동작).

수동 실행:
```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   REM 키 입력
python -m agent.server
```

## .env 키
| 키 | 용도 |
|---|---|
| `ANTHROPIC_API_KEY` 또는 `GEMINI_API_KEY` | 대본 생성 (`LLM_PROVIDER`로 선택) |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | TTS |
| `CAPCUT_DRAFT_DIR` | 캡컷 Projects 폴더. 비우면 `output/`에 생성 |
| `THROWAWAY_INTRO` | 초반 잘라낼 버리는 멘트 |

## 산출물 (`output/` 또는 캡컷 폴더)
- `*.srt` 자막 (캡컷 드래프트 실패 시 수동 임포트용 — 항상 생성됨)
- `tts_*.mp3` 음성
- `<세션>/draft_content.json` 캡컷 드래프트

## 참고
- 캡컷 드래프트 생성은 `pyJianYingDraft`에 의존하며 **캡컷 버전에 따라 호환이 안 될 수 있음**. 실패해도 SRT/MP3는 정상 산출되니 캡컷에서 수동 임포트하면 됩니다.
- 키가 없으면 각 단계는 목업으로 동작합니다(더미 음성/간단 정리). 실제 결과는 키를 넣어야 나옵니다.
