@echo off
REM 숏폼 자동화 에이전트 실행 (윈도우)
cd /d "%~dp0"

IF NOT EXIST ".venv" (
  echo [setup] 가상환경 생성 중...
  python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [setup] 의존성 설치/확인...
pip install -q -r requirements.txt

IF NOT EXIST ".env" (
  echo [info] .env 가 없어 .env.example 을 복사합니다. 키를 채워주세요.
  copy .env.example .env
)

echo [run] 서버 시작 → 브라우저가 자동으로 열립니다 (http://127.0.0.1:8765)
python -m agent.server
pause
