@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MatRes One-Click Update + Start

REM ========== Machine-specific config (4CV823WZKZ-W10) ==========
set "MATRES_DASHBOARD_URL=http://143.35.13.175:8050/"
set "MATRES_ADMIN_PASSWORD=HR"

REM ========== 0) 项目目录 ==========
set "PROJECT_ROOT=%~dp0"
pushd "%PROJECT_ROOT%"

echo [INFO] Project Root: %PROJECT_ROOT%

REM ========== 0.5) 杀掉旧的 Dashboard 进程 ==========
echo [INFO] Stopping old dashboard (if any) ...
taskkill /f /fi "WINDOWTITLE eq MatRes Dashboard Server" >nul 2>nul
REM Also kill any python using port 8050
for /f "tokens=5" %%P in ('netstat -aon ^| findstr ":8050 " ^| findstr "LISTENING"') do (
  taskkill /f /pid %%P >nul 2>nul
)
timeout /t 2 /nobreak >nul
echo [OK] Port 8050 cleared.

REM ========== 1) 检查 Git ==========
where git >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Git 未安装或未加入 PATH。
  echo [HINT] 先安装 Git for Windows 后重试。
  pause
  exit /b 1
)

REM ========== 2) 初始化/修复仓库 ==========
if not exist ".git" (
  echo [WARN] 当前目录不是 Git 仓库，正在初始化...
  git init
)

REM ========== 3) 绑定远程 ==========
REM Repository URL — change here if the repo moves
set "EXPECTED_REPO=https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git"

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo [INFO] 添加远程 origin...
  git remote add origin !EXPECTED_REPO!
) else (
  for /f "delims=" %%R in ('git remote get-url origin') do set "REMOTE_URL=%%R"
  if /I not "!REMOTE_URL!"=="!EXPECTED_REPO!" (
    echo [WARN] origin 地址不是目标仓库，正在修正...
    git remote set-url origin !EXPECTED_REPO!
  )
)

REM ========== 4) 拉取并切到 main ==========
echo [INFO] Fetch latest...
git fetch origin --prune
if errorlevel 1 (
  echo [ERROR] git fetch 失败。
  pause
  exit /b 1
)

echo [INFO] Checkout main...
git checkout -B main origin/main >nul 2>nul
if errorlevel 1 (
  echo [WARN] checkout 被未跟踪文件阻塞，先做备份+清理...
  if exist "config" robocopy "config" "_backup_config_auto" /E >nul
  git clean -fd -e .venv -e .venv_* -e data -e _backup_config_auto
  git checkout -B main origin/main
  if errorlevel 1 (
    echo [ERROR] 仍无法切换到 main，请人工检查冲突文件。
    pause
    exit /b 1
  )
)

git pull origin main
if errorlevel 1 (
  echo [ERROR] git reset 失败。
  pause
  exit /b 1
)

for /f "delims=" %%C in ('git log -1 --oneline') do set "LAST_COMMIT=%%C"
echo [OK] Current commit: !LAST_COMMIT!

REM ========== 5) Python / 虚拟环境 ==========
REM Prefer "python" over "py" launcher to avoid stale registry entries
set "PY_CMD="

REM 5a) Try "python" first — most reliable when Python is in PATH
where python >nul 2>nul
if not errorlevel 1 (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY_CMD=python"
)

REM 5b) Try "py -3" launcher as fallback
if not defined PY_CMD (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 --version >nul 2>nul
    if not errorlevel 1 set "PY_CMD=py -3"
  )
)

REM 5c) Scan common install locations as last resort
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
  echo [ERROR] 未找到 Python。请安装 Python 3.10+ 并加入 PATH。
  echo [HINT] 下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)

echo [INFO] Using Python: %PY_CMD%
%PY_CMD% --version

REM Each machine gets its own venv so two computers sharing
REM the same OneDrive folder won't conflict with each other.
set "VENV_DIR=.venv_%COMPUTERNAME%"
echo [INFO] Machine-specific venv: %VENV_DIR%

REM Validate existing venv (may be broken after cross-machine copy)
if exist "%VENV_DIR%\Scripts\python.exe" (
  "%VENV_DIR%\Scripts\python.exe" --version >nul 2>nul
  if errorlevel 1 (
    echo [WARN] Existing %VENV_DIR% is invalid. Recreating...
    rmdir /s /q "%VENV_DIR%"
  )
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [INFO] Creating venv: %VENV_DIR%
  %PY_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo [ERROR] 创建虚拟环境失败。
    pause
    exit /b 1
  )
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
  echo [ERROR] 激活虚拟环境失败。
  pause
  exit /b 1
)

python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] 安装依赖失败。
  pause
  exit /b 1
)

REM ========== 6) 重跑数据 ==========
echo [INFO] Running pipeline...
python .\scripts\matres_pipeline.py
if errorlevel 1 (
  echo [WARN] Pipeline had errors, continuing to start dashboard anyway...
)

REM ========== 7) 获取运行信息 ==========
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
for /f "delims=" %%C in ('git rev-parse --short HEAD') do set "COMMIT=%%C"

echo.
echo ========================================
echo   MatRes Dashboard (Production Mode)
echo ========================================
echo [INFO] Machine:    %COMPUTERNAME%
echo [INFO] Branch:     !BRANCH!
echo [INFO] Commit:     !COMMIT!
echo [INFO] Python:     %PY_CMD%
echo [INFO] Venv:       %VENV_DIR%
echo [INFO] Startup:    Waitress (production server)
echo ========================================
echo.
echo   Local URL:  http://localhost:8050
set "_LANIP="
for /f "tokens=2 delims=:" %%I in ('ipconfig ^| findstr /i "IPv4"') do (
  if not defined _LANIP (
    for /f "tokens=* delims= " %%A in ("%%I") do set "_LANIP=%%A"
  )
)
if defined _LANIP echo   LAN URL:    http://!_LANIP!:8050
echo.
echo   Press Ctrl+C to stop
echo ========================================
echo.

REM ========== 8) 启动看板（生产模式 Waitress）==========
python -m waitress --listen=0.0.0.0:8050 dashboards.matres_app:app.server

popd
endlocal