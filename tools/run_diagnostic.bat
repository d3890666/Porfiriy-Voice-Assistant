@echo off
chcp 65001 >nul
title ESP32 Audio Diagnostic Tool

set PY_EXE=%USERPROFILE%\.platformio\penv\Scripts\python.exe
if not exist "%PY_EXE%" (
    set PY_EXE=python
)

echo ========================================================
echo   Запуск утилиты диагностики звука ESP32-S3 (INMP441)
echo ========================================================
echo.

"%PY_EXE%" "%~dp0mic_diagnostic.py" %*

pause
