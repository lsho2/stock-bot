@echo off
cd /d "C:\Stock\stock-bot"
chcp 65001 > nul

set LOG_DIR=C:\Stock\data\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set LOG_FILE=%LOG_DIR%\morning_%date:~0,4%%date:~5,2%%date:~8,2%.log

set PYTHONIOENCODING=utf-8

echo [%date% %time%] 실행 시작 >> "%LOG_FILE%"
"C:\Users\jur08\AppData\Local\Programs\Python\Python313\python.exe" consensus.py >> "%LOG_FILE%" 2>&1
echo [%date% %time%] 실행 완료 >> "%LOG_FILE%"