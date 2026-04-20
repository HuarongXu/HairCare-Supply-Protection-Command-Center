@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MatRes One-Click Update + Start

echo ========================================
echo   MatRes Dashboard - One Click Start
echo ========================================
echo.

REM ========== 0) 项目目录 (用 bat 文件所在目录) ==========
cd /d "%~dp0"
set "PROJECT_ROOT=%CD%"
echo [INFO] Project Root: %PROJECT_ROOT%
echo [INFO] Computer:     %COMPUTERNAME%
echo [INFO] Time:         %DATE% %TIME%
echo.

REM ========== 1) 杀掉旧的 Dashboard 进程 ==========
echo [INFO] Checking for existing dashboard processes ...
for /f "tokens=2" %%P in ('tasklist /fi "WINDOWTITLE eq MatRes Dashboard Server" /fo list 2^>nul ^| findstr /i "PID:"') do (
  echo [INFO] Killing old dashboard PID: %%P
  taskkill /PID %%P /F >nul 2>nul
)
REM Also kill any python running matres_app.py
for /f "tokens=2 delims=," %%P in ('wmic process where "CommandLine like '%%matres_app%%' and name='python.exe'" get ProcessId /format:csv 2^>nul ^| findstr /r "[0-9]"') do (
  echo [INFO] Killing old matres_app.py PID: %%P
  taskkill /PID %%P /F >nul 2>nul
)
REM Wait for port 8050 to be released
timeout /t 3 /nobreak >nul

REM ========== 2) 检查 Git ==========
where git >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Git is not installed or not in PATH.
  echo [HINT] Install Git for Windows first.
  goto :FAIL
)

REM ========== 3) 初始化/修复 Git 仓库 ==========
if not exist ".git" (
  echo [WARN] Not a Git repo. Initializing ...
  git init
)
git status >nul 2>nul
if errorlevel 1 (
  echo [WARN] .git corrupted (OneDrive sync?). Rebuilding ...
  rmdir /s /q ".git" 2>nul
  git init
  echo [OK] .git rebuilt.
)

REM ========== 4) 绑定远程仓库 ==========
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo [INFO] Adding remote origin ...
  git remote add origin https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git
) else (
  for /f "delims=" %%R in ('git remote get-url origin') do set "REMOTE_URL=%%R"
  if /I not "!REMOTE_URL!"=="https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git" (
    echo [WARN] Fixing origin URL ...
    git remote set-url origin https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git
  )
)

REM ========== 5) 拉取最新代码 ==========
echo.
echo [INFO] Fetching latest code from GitHub ...
git fetch origin --prune
if errorlevel 1 (
  echo [WARN] git fetch failed, repairing .git/index ...
  if exist ".git\index" del /f /q ".git\index"
  git reset >nul 2>nul
  git fetch origin --prune
  if errorlevel 1 (
    echo [ERROR] git fetch still failed. Close any programs using project files and retry.
    goto :FAIL
  )
  echo [OK] .git/index repaired.
)

echo [INFO] Switching to main branch ...
git checkout -B main origin/main >nul 2>nul
if errorlevel 1 (
  echo [WARN] Checkout blocked by untracked files. Cleaning ...
  if exist "config" robocopy "config" "_backup_config_auto" /E >nul
  git clean -fd -e .venv -e .venv_* -e data -e config -e _backup_config_auto
  git checkout -B main origin/main
  if errorlevel 1 (
    echo [ERROR] Cannot switch to main. Check conflicting files manually.
    goto :FAIL
  )
)

git reset --hard origin/main
if errorlevel 1 (
  echo [ERROR] git reset failed.
  goto :FAIL
)

for /f "delims=" %%C in ('git log -1 --oneline') do set "LAST_COMMIT=%%C"
echo [OK] Current commit: !LAST_COMMIT!

REM ========== 6) 找到 Python ==========
echo.
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
  echo [ERROR] Python not found. Install Python 3.10+ and add to PATH.
  goto :FAIL
)

echo [INFO] Using Python: %PY_CMD%
%PY_CMD% --version

REM ========== 7) 虚拟环境 (每台电脑独立) ==========
set "VENV_DIR=.venv_%COMPUTERNAME%"
echo [INFO] Venv: %VENV_DIR%

REM Validate existing venv
if exist "%VENV_DIR%\Scripts\python.exe" (
  "%VENV_DIR%\Scripts\python.exe" --version >nul 2>nul
  if errorlevel 1 (
    echo [WARN] Existing venv is broken. Recreating ...
    rmdir /s /q "%VENV_DIR%"
  )
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [INFO] Creating venv ...
  %PY_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] Failed to create venv.
    goto :FAIL
  )
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
  echo [ERROR] Failed to activate venv.
  goto :FAIL
)

echo [INFO] Installing / updating dependencies ...
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
  echo [ERROR] pip install failed.
  goto :FAIL
)
echo [OK] Dependencies ready.

REM ========== 8) 刷新数据 (Pipeline) ==========
echo.
echo [INFO] Running data pipeline ...
python .\scripts\matres_pipeline.py
if errorlevel 1 (
  echo [WARN] Pipeline had errors (see above). Dashboard will start anyway.
  echo.
)
echo [OK] Pipeline finished.

REM ========== 9) 启动 Dashboard ==========
echo.
echo ========================================
echo   Starting Dashboard ...
echo   URL: http://localhost:8050
echo   Press Ctrl+C to stop
echo ========================================
echo.

title MatRes Dashboard Server
python .\dashboards\matres_app.py

echo.
echo ========================================
echo   Dashboard has stopped.
echo ========================================
goto :FAIL

:FAIL
echo.
echo Press any key to close this window ...
pause >nul
endlocal