@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MatRes Server Deployment

REM ================================================================
REM   MatRes Server Deployment Script
REM ================================================================
REM   This script sets up the server (big computer) to:
REM   1. Clone code to a LOCAL folder (C:\MatRes) — NOT in OneDrive
REM   2. Read data files from the shared OneDrive folder
REM   3. Auto-update code from GitHub every 5 minutes
REM   4. Start the dashboard
REM
REM   Run this script ONCE on the server machine.
REM   After that, use C:\MatRes\server_start.bat to start daily.
REM ================================================================

set "DEPLOY_DIR=C:\MatRes"
set "REPO_URL=https://github.com/HuarongXu/HairCare-Supply-Protection-Command-Center.git"
set "BRANCH=main"

echo ============================================
echo   MatRes Server Deployment
echo ============================================
echo.
echo   Deploy to:  %DEPLOY_DIR%
echo   Repo:       %REPO_URL%
echo.

REM ── Step 0: Check prerequisites ──────────────────────────────
where git >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Git is not installed. Please install Git for Windows first.
  pause
  exit /b 1
)

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
  echo [ERROR] Python not found. Please install Python 3.10+.
  pause
  exit /b 1
)
echo [OK] Python: %PY_CMD%

REM ── Step 1: Find OneDrive data folder ────────────────────────
echo.
echo [Step 1] Locating OneDrive data folder...
set "ONEDRIVE_DATA="

REM Search for the project folder under common OneDrive locations
for /d %%D in ("%USERPROFILE%\OneDrive*") do (
  if not defined ONEDRIVE_DATA (
    for /f "delims=" %%F in ('dir /s /b /ad "%%D" 2^>nul ^| findstr /i /c:"MR Upload Tool link with VM"') do (
      if not defined ONEDRIVE_DATA (
        if exist "%%F\0.Data Base" (
          set "ONEDRIVE_DATA=%%F"
        )
      )
    )
  )
)

if defined ONEDRIVE_DATA (
  echo [OK] Found OneDrive data: !ONEDRIVE_DATA!
) else (
  echo [WARN] Could not auto-detect OneDrive data folder.
  echo.
  echo   Please enter the full path to the OneDrive project folder.
  echo   Example: C:\Users\username\OneDrive - Procter and Gamble\...\12.MR Upload Tool link with VM
  echo.
  set /p "ONEDRIVE_DATA=Path: "
)

if not exist "!ONEDRIVE_DATA!\0.Data Base" (
  echo [ERROR] "0.Data Base" folder not found at: !ONEDRIVE_DATA!
  pause
  exit /b 1
)
echo [OK] Data source: !ONEDRIVE_DATA!\0.Data Base

REM ── Step 2: Clone or update repository ───────────────────────
echo.
echo [Step 2] Setting up code at %DEPLOY_DIR%...

if exist "%DEPLOY_DIR%\.git" (
  echo [INFO] Repository exists, pulling latest...
  pushd "%DEPLOY_DIR%"
  git fetch origin --prune
  git reset --hard origin/%BRANCH%
  popd
) else (
  if exist "%DEPLOY_DIR%" (
    echo [INFO] Directory exists but no git repo. Cleaning and cloning...
    rmdir /s /q "%DEPLOY_DIR%" 2>nul
  )
  echo [INFO] Cloning repository...
  git clone "%REPO_URL%" "%DEPLOY_DIR%"
  if errorlevel 1 (
    echo [ERROR] git clone failed.
    pause
    exit /b 1
  )
)

pushd "%DEPLOY_DIR%"
for /f "delims=" %%C in ('git log -1 --oneline') do set "LAST_COMMIT=%%C"
echo [OK] Current commit: !LAST_COMMIT!

REM ── Step 3: Create server-specific config ────────────────────
echo.
echo [Step 3] Creating server config...

if not exist "config" mkdir "config"

REM Escape backslashes for JSON — replace \ with \\
set "ESCAPED_DATA=!ONEDRIVE_DATA:\=\\!"

(
  echo {
  echo   "workbook_path": "!ESCAPED_DATA!\\0.Data Base\\MR Upload Request Form_VM Version.xlsm",
  echo   "sheet_name": "MatRes Record",
  echo   "level1_workbook_path": "!ESCAPED_DATA!\\0.Data Base\\HairCare Code List By Seg_Update Version.xlsx",
  echo   "level1_sheet_name": "Seg summary by code_New Version",
  echo   "level1_material_column": "Material",
  echo   "level1_first_level_column": "First Level",
  echo   "data_base_dir": "!ESCAPED_DATA!\\0.Data Base",
  echo   "production_data_dir": "!ESCAPED_DATA!\\0.Data Base\\Production Volume",
  echo   "history_path": "data/history/matres_history.csv",
  echo   "processed_dir": "data/processed",
  echo   "requester_roles_path": "config/requester_roles.json",
  echo   "time_zone": "Asia/Shanghai",
  echo   "admin_password": "HR",
  echo   "refresh": {
  echo     "append_history": true,
  echo     "history_keys": ["Material Number", "Reservation No", "Availability Date"]
  echo   }
  echo }
) > "config\config.json"

echo [OK] Config written with OneDrive data paths.

REM ── Step 4: Create virtual environment ───────────────────────
echo.
echo [Step 4] Setting up Python virtual environment...

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" --version >nul 2>nul
  if errorlevel 1 (
    echo [WARN] Existing venv is invalid. Recreating...
    rmdir /s /q ".venv"
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating venv...
  %PY_CMD% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
  echo [ERROR] Failed to install dependencies.
  pause
  exit /b 1
)
echo [OK] Dependencies installed.

REM ── Step 5: Create helper scripts ───────────────────────────
echo.
echo [Step 5] Creating helper scripts...

REM --- server_start.bat ---
(
  echo @echo off
  echo setlocal enabledelayedexpansion
  echo chcp 65001 ^>nul
  echo title MatRes Dashboard Server
  echo.
  echo cd /d "%DEPLOY_DIR%"
  echo.
  echo echo [INFO] Pulling latest code...
  echo git fetch origin --prune
  echo git reset --hard origin/%BRANCH%
  echo for /f "delims=" %%%%C in ^('git log -1 --oneline'^) do set "COMMIT=%%%%C"
  echo echo [OK] Commit: ^^!COMMIT^^!
  echo.
  echo call ".venv\Scripts\activate.bat"
  echo pip install -r requirements.txt --quiet
  echo.
  echo echo [INFO] Running pipeline...
  echo python scripts\matres_pipeline.py
  echo.
  echo echo [INFO] Starting dashboard...
  echo python dashboards\matres_app.py
) > "%DEPLOY_DIR%\server_start.bat"

echo [OK] server_start.bat created.

REM --- server_auto_update.vbs (silent background updater) ---
(
  echo Set oShell = CreateObject^("WScript.Shell"^)
  echo sDir = "%DEPLOY_DIR%"
  echo oShell.CurrentDirectory = sDir
  echo.
  echo ' Check for new commits
  echo oShell.Run "git fetch origin --prune", 0, True
  echo.
  echo ' Compare local vs remote
  echo Set oExec = oShell.Exec^("git rev-parse HEAD"^)
  echo sLocal = Trim^(oExec.StdOut.ReadLine^(^)^)
  echo Set oExec = oShell.Exec^("git rev-parse origin/main"^)
  echo sRemote = Trim^(oExec.StdOut.ReadLine^(^)^)
  echo.
  echo If sLocal ^<^> sRemote Then
  echo   ' New code available — write flag and let dashboard handle restart
  echo   oShell.Run "git reset --hard origin/main", 0, True
  echo   Set oFS = CreateObject^("Scripting.FileSystemObject"^)
  echo   Set oFile = oFS.CreateTextFile^(sDir ^& "\data\processed\.run_pipeline_on_start", True^)
  echo   oFile.Write "auto-update"
  echo   oFile.Close
  echo   ' Kill existing dashboard and restart
  echo   oShell.Run "taskkill /F /IM python.exe", 0, True
  echo   WScript.Sleep 2000
  echo   oShell.Run "cmd /c """ ^& sDir ^& "\server_start.bat""", 1, False
  echo End If
) > "%DEPLOY_DIR%\server_auto_update.vbs"

echo [OK] server_auto_update.vbs created.

REM ── Step 6: Create Windows Scheduled Task ───────────────────
echo.
echo [Step 6] Setting up auto-update scheduled task...

schtasks /query /tn "MatRes_AutoUpdate" >nul 2>nul
if not errorlevel 1 (
  echo [INFO] Removing existing scheduled task...
  schtasks /delete /tn "MatRes_AutoUpdate" /f >nul 2>nul
)

schtasks /create /tn "MatRes_AutoUpdate" /tr "wscript.exe \"%DEPLOY_DIR%\server_auto_update.vbs\"" /sc minute /mo 5 /f >nul 2>nul
if errorlevel 1 (
  echo [WARN] Could not create scheduled task (may need admin rights).
  echo [HINT] You can manually create a task that runs server_auto_update.vbs every 5 minutes.
) else (
  echo [OK] Scheduled task "MatRes_AutoUpdate" created (runs every 5 minutes).
)

REM ── Step 7: Run pipeline + start dashboard ──────────────────
echo.
echo ============================================
echo   Deployment Complete!
echo ============================================
echo.
echo   Code:       %DEPLOY_DIR%
echo   Data:       !ONEDRIVE_DATA!\0.Data Base
echo   Commit:     !LAST_COMMIT!
echo   Auto-update: Every 5 minutes via MatRes_AutoUpdate task
echo.
echo   To start daily:  %DEPLOY_DIR%\server_start.bat
echo   To update now:    wscript %DEPLOY_DIR%\server_auto_update.vbs
echo.
echo ============================================
echo.
echo [INFO] Running pipeline...
python scripts\matres_pipeline.py
if errorlevel 1 (
  echo [WARN] Pipeline failed. Dashboard will start without fresh data.
)

echo [INFO] Starting dashboard...
python dashboards\matres_app.py

popd
endlocal
