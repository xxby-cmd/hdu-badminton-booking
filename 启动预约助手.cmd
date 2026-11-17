@echo off
setlocal
cd /d "%~dp0"
set "HDU_BOOTSTRAP_ROOT=%~dp0"
set "HDU_BOOTSTRAP_ARGS="
if "%~1"=="--self-check" set "HDU_BOOTSTRAP_ARGS=-SelfCheck"
powershell.exe -NoProfile -NonInteractive -Command "& ([scriptblock]::Create([IO.File]::ReadAllText((Join-Path $env:HDU_BOOTSTRAP_ROOT 'bootstrap.ps1')))) %HDU_BOOTSTRAP_ARGS%"
if errorlevel 1 (
 pause
 exit /b 1
)
exit /b 0
