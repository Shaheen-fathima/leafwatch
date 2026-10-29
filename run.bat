@echo off
REM LeafWatch launcher (Windows). Run from a terminal or double-click.
cd /d "%~dp0"
if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat

if "%LEAFWATCH_ADMIN_KEY%"=="" (
  echo.
  echo  [ERROR] LEAFWATCH_ADMIN_KEY environment variable is not configured!
  echo.
  echo  LeafWatch requires a securely configured admin key before startup.
  echo  Please set LEAFWATCH_ADMIN_KEY in your environment and run again:
  echo.
  echo    Command Prompt:
  echo      set LEAFWATCH_ADMIN_KEY=your_private_admin_key
  echo      run.bat
  echo.
  echo    PowerShell:
  echo      $env:LEAFWATCH_ADMIN_KEY="your_private_admin_key"
  echo      .\run.bat
  echo.
  pause
  exit /b 1
)

REM Admin approval is ON by default. To switch it off for a demo only:  set LEAFWATCH_REQUIRE_ADMIN_APPROVAL=0
echo  Starting LeafWatch...
echo  Open http://127.0.0.1:8000  ^(this PC^)   or  http://YOUR-PC-IP:8000  ^(phones on the same Wi-Fi^)
echo  The site is online only while this window is open and this PC is on.
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
pause
