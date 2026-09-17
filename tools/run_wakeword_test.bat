@echo off
chcp 65001 > nul
cd /d "%~dp0\.."
echo [INFO] Запуск локального теста вейкворда «Порфирий»...
python tools\test_local_wakeword.py
pause
