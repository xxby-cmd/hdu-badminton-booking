@echo off
cd /d "%~dp0"
start "" pythonw.exe -X utf8 "%~dp0app.pyw" --plan formal
