@echo off
:: ============================================================
:: send_report.bat  ?? 8:55 AM ?묒뾽 ?ㅼ?以꾨윭 ?깅줉??
:: consensus.py媛 ??ν븳 last_report.txt 瑜??붾젅洹몃옩?쇰줈 ?꾩넚.
:: consensus.py媛 ?뺤긽 ?꾨즺?먮떎硫??대? ?꾩넚????遺꾩꽍 以??ㅻ쪟 諛쒖깮 ???ъ쟾?≪슜.
::
:: ?깅줉 諛⑸쾿 (愿由ъ옄 PowerShell):
::   schtasks /create /tn "StockBot_SendReport" /tr "\"C:\Stock\stock-bot\send_report.bat\"" /sc daily /st 08:55 /f
:: ============================================================

cd /d "C:\Stock\stock-bot"

set REPORT_FILE=C:\Stock\stock-bot\last_report.txt

if not exist "%REPORT_FILE%" (
    echo [%date% %time%] last_report.txt ?놁쓬 - 遺꾩꽍???꾩쭅 ?꾨즺?섏? ?딆븯?듬땲??
    exit /b 1
)

python -c ^
"from pathlib import Path; from telegram_alert import send_message; ^
 msg = Path(r'C:\Stock\stock-bot\last_report.txt').read_text(encoding='utf-8'); ^
 ok = send_message(msg); ^
 print('?꾩넚 ?꾨즺' if ok else '?꾩넚 ?ㅽ뙣')"

