@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" goto use_venv
where py >nul 2>nul
if not errorlevel 1 goto use_py
python -B -X utf8 "%~dp0watson.py" %*
exit /b %errorlevel%

:use_venv
"%~dp0.venv\Scripts\python.exe" -B -X utf8 "%~dp0watson.py" %*
exit /b %errorlevel%

:use_py
py -3 -B -X utf8 "%~dp0watson.py" %*
exit /b %errorlevel%
