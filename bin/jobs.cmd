@echo off
rem Launcher for Windows: put this folder on PATH, then run `jobs` from anywhere.
set "JOBS_ROOT=%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (py -3 "%JOBS_ROOT%\jobs.py" %*) else (python "%JOBS_ROOT%\jobs.py" %*)
