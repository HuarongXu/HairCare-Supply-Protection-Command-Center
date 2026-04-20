@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MatRes One-Click Update + Start

REM ========== 0) 项目目录 ==========
set "PROJECT_ROOT=%~dp0"
pushd "%PROJECT_ROOT%"

echo [INFO] Project Root: %PROJECT_ROOT%

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
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo [INFO] 添加远程 origin...
  git remote add origin https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git
) else (
  for /f "delims=" %%R in ('git remote get-url origin') do set "REMOTE_URL=%%R"
  if /I not "!REMOTE_URL!"=="https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git" (
    echo [WARN] origin 地址不是目标仓库，正在修正...
    git remote set-url origin https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git
  )
)

REM ========== 4) 拉取并切到 main ==========
echo [INFO] Fetch latest...
git fetch origin --prune
if errorlevel 1 (
  echo [WARN] git fetch 失败，尝试修复 .git/index ...
  if exist ".git\index" del /f /q ".git\index"
  git reset >nul 2>nul
  git fetch origin --prune
  if errorlevel 1 (
    echo [ERROR] git fetch 仍然失败，请关闭所有占用文件的程序后重试。
    pause
    exit /b 1
  )
  echo [OK] .git/index 已修复。
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

git reset --hard origin/main
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

REM ========== 6) 重跑数据 + 启动看板 ==========
echo [INFO] Running pipeline...
python .\scripts\matres_pipeline.py
if errorlevel 1 (
  echo [ERROR] pipeline 失败，请检查日志。
  pause
  exit /b 1
)

echo [INFO] Starting dashboard...
python .\dashboards\matres_app.py

popd
endlocal