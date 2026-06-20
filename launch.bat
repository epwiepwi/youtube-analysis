@echo off
REM Shorts Editor — launch the desktop GUI.
cd /d "%~dp0"
set PYTHONUTF8=1
python -m gui.app
if errorlevel 1 (
    echo.
    echo [launch failed]  Python 설치 여부와 requirements 설치 상태를 확인하세요.
    echo   1) python --version
    echo   2) python -m pip install -r requirements.txt
    pause
)
