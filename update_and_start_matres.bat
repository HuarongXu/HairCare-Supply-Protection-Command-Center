@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MatRes One-Click Update + Start

REM ========== Machine-specific config ==========
if not defined MATRES_ADMIN_PASSWORD set "MATRES_ADMIN_PASSWORD=HR"

echo ========================================
echo   MatRes Dashboard - One Click Start
echo ========================================
echo.

REM -- Go to the folder where this bat file lives --
cd /d "%~dp0"
echo [INFO] Project Root: %CD%
echo [INFO] Computer:     %COMPUTERNAME%
echo.

REM -- Kill old dashboard if running --
echo [INFO] Stopping old dashboard (if any) ...
taskkill /f /fi "WINDOWTITLE eq MatRes Dashboard Server" >nul 2>nul
taskkill /f /fi "WINDOWTITLE eq MatRes One-Click Update + Start" /fi "PID ne %PID%" >nul 2>nul
REM Also kill whatever is listening on port 8050 (robust regardless of window title)
for /f "tokens=5" %%P in ('netstat -aon ^| findstr ":8050 " ^| findstr "LISTENING"') do (
  taskkill /f /pid %%P >nul 2>nul
)
timeout /t 3 /nobreak >nul
echo [OK] Ready.
echo.

REM -- Check Git --
where git >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Git not found. Install Git for Windows.
  goto :DONE
)

REM -- Init/repair git repo if needed --
if not exist ".git" (
  echo [WARN] Not a git repo, initializing ...
  git init
)
git status >nul 2>nul
if errorlevel 1 (
  echo [WARN] .git corrupted, rebuilding ...
  rmdir /s /q ".git" 2>nul
  git init
)

REM -- Setup remote --
REM Repository URL - change here if the repo moves
set "EXPECTED_REPO=https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git"

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin !EXPECTED_REPO!
)

REM -- Pull latest code --
echo [INFO] Pulling latest code from GitHub ...
git fetch origin --prune 2>nul
if errorlevel 1 (
  echo [WARN] fetch failed, repairing index ...
  if exist ".git\index" del /f /q ".git\index"
  git reset >nul 2>nul
  git fetch origin --prune
  if errorlevel 1 (
    echo [ERROR] Cannot fetch from GitHub.
    goto :DONE
  )
)

git checkout -B main origin/main >nul 2>nul
if errorlevel 1 (
  if exist "config" robocopy "config" "_backup_config_auto" /E >nul
  git clean -fd -e .venv -e .venv_* -e data -e config -e _backup_config_auto >nul 2>nul
  git checkout -B main origin/main >nul 2>nul
)

git pull origin main >nul 2>nul
for /f "delims=" %%C in ('git log -1 --oneline 2^>nul') do echo [OK] Commit: %%C
echo.

REM -- Find Python --
set "PY_CMD="
where python >nul 2>nul
if not errorlevel 1 (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
  )
)
if not defined PY_CMD (
  for %%V in (313 312 311 310) do (
    if not defined PY_CMD (
      if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
      )
    )
  )
)
if not defined PY_CMD (
  echo [ERROR] Python not found. Install Python 3.10+
  goto :DONE
)
echo [INFO] Python: %PY_CMD%

REM -- Setup venv --
set "VENV_DIR=.venv_%COMPUTERNAME%"
if exist "%VENV_DIR%\Scripts\python.exe" (
  "%VENV_DIR%\Scripts\python.exe" --version >nul 2>nul
  if errorlevel 1 (
    echo [WARN] Venv broken, recreating ...
    rmdir /s /q "%VENV_DIR%" 2>nul
  )
)
if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [INFO] Creating venv: %VENV_DIR% ...
  %PY_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] Failed to create venv.
    goto :DONE
  )
)
call "%VENV_DIR%\Scripts\activate.bat"
echo [INFO] Venv activated: %VENV_DIR%

REM -- Install dependencies --
echo [INFO] Installing dependencies ...
python -m pip install --upgrade pip --quiet 2>nul
pip install -r requirements.txt --quiet 2>nul
if errorlevel 1 (
  echo [WARN] Some dependencies may have failed.
)
echo [OK] Dependencies ready.
echo.

REM -- Run pipeline --
echo [INFO] Running data pipeline ...
python .\scripts\matres_pipeline.py
if errorlevel 1 (
  echo [WARN] Pipeline had errors, continuing anyway ...
)
echo [OK] Pipeline finished.
echo.

REM -- Start dashboard --
echo ========================================
echo   Starting Dashboard ...
echo   URL (local): http://localhost:8050
set "_LANIP="
for /f "tokens=2 delims=:" %%I in ('ipconfig ^| findstr /i "IPv4"') do (
  if not defined _LANIP (
    for /f "tokens=* delims= " %%A in ("%%I") do set "_LANIP=%%A"
  )
)
if defined _LANIP echo   URL (LAN):   http://!_LANIP!:8050
echo   Press Ctrl+C to stop
echo ========================================
echo.
title MatRes Dashboard Server
python -m waitress --listen=0.0.0.0:8050 dashboards.matres_app:app.server

echo.
echo [INFO] Dashboard has exited.

:DONE
echo.
echo ========================================
echo   Press any key to close ...
echo ========================================
pause >nul
endlocal