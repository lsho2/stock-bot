@echo off
:: ============================================================
:: send_report.bat  —  8:55 AM 작업 스케줄러 등록용
:: consensus.py가 저장한 last_report.txt 를 텔레그램으로 전송.
:: consensus.py가 정상 완료됐다면 이미 전송됨 → 분석 중 오류 발생 시 재전송용.
::
:: 등록 방법 (관리자 PowerShell):
::   schtasks /create /tn "StockBot_SendReport" /tr "\"C:\Stock\stock-bot\send_report.bat\"" /sc daily /st 08:55 /f
:: ============================================================

cd /d "C:\Stock\stock-bot"

set REPORT_FILE=C:\Stock\data\reports\last_report.txt

if not exist "%REPORT_FILE%" (
    echo [%date% %time%] last_report.txt 없음 - 분석이 아직 완료되지 않았습니다.
    exit /b 1
)

python -c ^
"from pathlib import Path; from telegram_alert import send_message; ^
 msg = Path(r'C:\Stock\data\reports\last_report.txt').read_text(encoding='utf-8'); ^
 ok = send_message(msg); ^
 print('전송 완료' if ok else '전송 실패')"
