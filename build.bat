@echo off
REM Shorts Editor — one-shot build. Run on Windows. Produces:
REM   dist\ShortsEditor\ShortsEditor.exe   (PyInstaller bundle)
REM   dist\ShortsEditor-Setup.exe          (Inno Setup installer, if iscc found)
setlocal enableextensions
cd /d "%~dp0"

echo === [1/5] Verifying Python ===
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found on PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)
python --version

echo === [2/5] Installing build/runtime dependencies ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "pyinstaller>=6.5.0"

echo === [3/5] Fetching ffmpeg if missing ===
if not exist "vendor\ffmpeg\ffmpeg.exe" (
    if not exist "vendor" mkdir vendor
    if not exist "vendor\ffmpeg" mkdir "vendor\ffmpeg"
    echo Downloading ffmpeg release essentials...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'vendor\ffmpeg.zip'"
    if errorlevel 1 (
        echo ffmpeg 다운로드 실패. 수동으로 ffmpeg.exe, ffprobe.exe를 vendor\ffmpeg\에 넣고 다시 실행하세요.
        pause
        exit /b 1
    )
    powershell -NoProfile -Command "Expand-Archive -Force 'vendor\ffmpeg.zip' -DestinationPath 'vendor\ffmpeg_tmp'"
    for /d %%D in (vendor\ffmpeg_tmp\*) do (
        copy /Y "%%D\bin\ffmpeg.exe" vendor\ffmpeg\ >nul
        copy /Y "%%D\bin\ffprobe.exe" vendor\ffmpeg\ >nul
    )
    rmdir /s /q vendor\ffmpeg_tmp
    del vendor\ffmpeg.zip
)
echo   vendor\ffmpeg\ffmpeg.exe  : OK

echo === [4/5] Running PyInstaller ===
if exist "build" rmdir /s /q build
if exist "dist\ShortsEditor" rmdir /s /q "dist\ShortsEditor"
python -m PyInstaller --noconfirm --clean app.spec
if errorlevel 1 (
    echo PyInstaller build 실패. 위 로그를 확인하세요.
    pause
    exit /b 1
)
echo   dist\ShortsEditor\ShortsEditor.exe : OK

echo === [5/5] Building Inno Setup installer ===
set "ISCC_EXE="
where iscc >nul 2>nul
if not errorlevel 1 (
    set "ISCC_EXE=iscc"
) else (
    if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)
if defined ISCC_EXE (
    "%ISCC_EXE%" installer.iss
    if errorlevel 1 (
        echo 설치 프로그램 빌드 실패. installer.iss 로그 확인.
        pause
        exit /b 1
    )
    echo.
    echo ==================================================
    echo  설치 프로그램 완성: dist\ShortsEditor-Setup.exe
    echo ==================================================
) else (
    echo Inno Setup이 설치되어 있지 않습니다. https://jrsoftware.org/isdl.php 에서 받은 뒤
    echo 다시 build.bat 실행 또는 iscc installer.iss 수동 실행하세요.
    echo 현재까지 결과: dist\ShortsEditor\ShortsEditor.exe 폴더 그대로 배포 가능.
)

pause
endlocal
