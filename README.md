# YouTube Shorts Auto-Editor (CapCut)

나레이션 + 스톡 영상 → 스타일 프로파일 기반 CapCut 프로젝트 자동 생성.

`profiles/style_profile.json`의 페이싱/자막 규칙에 맞춰 컷 포인트와 어절 자막을 생성하고, CapCut이 바로 열 수 있는 draft 폴더(`draft_content.json`, `draft_meta_info.json`)를 만든다.

## 파이프라인

```
나레이션 오디오 + 스톡 영상 폴더 + style_profile.json
        │
        ▼
Whisper STT (어절 타임스탬프)
        │
        ▼
컷 포인트 계산 (avg_cut_length_sec 기준, 어절 경계에 스냅)
        │
        ▼
자막 그룹핑 (1~2어절 단위, 0.1초 선행)
        │
        ▼
CapCut draft 폴더 생성 + Resources/에 소스 복사
```

## 사용법 (Windows)

```powershell
pip install -r requirements.txt
# ffmpeg도 PATH에 있어야 함 (ffprobe가 클립 길이 측정)

python -m src.main `
  --narration "C:\path\to\narration.wav" `
  --clips "C:\path\to\clips_folder" `
  --name "양파_쇼츠_001"
```

실행 후 CapCut을 켜면 draft 목록에 `양파_쇼츠_001`이 보임.

## 주의사항

- **CapCut draft 포맷은 비공식**이다. 버전별로 필드가 달라질 수 있다. 안 열리면 CapCut에서 빈 프로젝트를 만든 뒤 `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\<이름>\draft_content.json`을 참고해 `src/capcut_draft.py`를 조정해야 한다.
- 온글잎 의연체 같은 커스텀 폰트는 CapCut에 별도 설치되어 있어야 적용된다. 폰트 이름만 프로파일에 기록되고, 경로는 CapCut이 로컬 설치분에서 찾는다.
- Whisper `large-v3` 모델은 GPU 없으면 느리다. `WHISPER_MODEL=medium` 환경변수로 경량화 가능.

## 다음 단계

- 클립-어절 의미 매칭 (Gemini vision으로 클립 내용 태깅 후 자막 키워드와 매칭)
- 톤업 컬러 필터 자동 적용 (CapCut `video_effects` 삽입)
- 이모지 자동 삽입 (부정/경고 어절 감지 시 ❌ 삽입)
- 썸네일 자동 생성

## 데스크탑 앱 빌드 (Windows .exe + 설치 프로그램)

본인이 직접 쓰거나 다른 사람한테 배포할 .exe 만드는 방법:

1. Windows에서 이 폴더 열고:
   ```
   build.bat
   ```
2. 자동으로:
   - PyInstaller 설치
   - ffmpeg 다운로드 (vendor/ffmpeg/)
   - dist\ShortsEditor\ 에 폴더 형태 .exe 빌드
   - Inno Setup 설치돼 있으면 dist\ShortsEditor-Setup.exe 까지 생성

설치 프로그램까지 만들려면 Inno Setup 무료 설치: https://jrsoftware.org/isdl.php
설치 안 해도 `dist\ShortsEditor` 폴더 통째로 ZIP해서 배포 가능.
