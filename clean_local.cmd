@echo off
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0clean_local.py"
  exit /b %errorlevel%
)
python "%~dp0clean_local.py"
