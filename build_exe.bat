@echo off
REM Rebuild bin\get_cookie.exe from get_cookie.py.
REM Usage: double-click this file, or run "build_exe.bat" in cmd.
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :failed

python -m PyInstaller --noconfirm --clean --onefile --console ^
  --name get_cookie --runtime-tmpdir . ^
  --distpath bin --workpath build\pyinstaller --specpath build ^
  get_cookie.py
if errorlevel 1 goto :failed

echo.
echo Build OK: %~dp0bin\get_cookie.exe
goto :eof

:failed
echo.
echo Build FAILED. Make sure Python 3.8+ and pip are available.
exit /b 1