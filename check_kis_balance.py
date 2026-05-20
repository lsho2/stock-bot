import os
import requests
from dotenv import load_dotenv
import sys
sys.path.insert(0, r"C:\Stock\stock-bot")
sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(r"C:\Stock\stock-bot\.env")
APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT = os.getenv("KIS_ACCOUNT", "")
BASE_URL = "https://openapivts.koreainvestment.com:29443"

from stock_info import get_token
token = get_token()

# 계좌 분리 (예: 50187747-01)
if "-" in ACCOUNT:
    cano, prdt_cd = ACCOUNT.split("-")
else:
    cano, prdt_cd = ACCOUNT[:8], ACCOUNT[8:] if len(ACCOUNT) > 8 else "01"

resp = requests.get(
    f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
    headers={
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "VTTC8434R",
        "custtype": "P",
    },
    params={
        "CANO": cano,
        "ACNT_PRDT_CD": prdt_cd,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    },
    timeout=10,
)
data = resp.json()

print("=== KIS 모의투자 실제 잔고 ===\n")
for stock in data.get("output1", []):
    name = stock.get("prdt_name", "")
    code = stock.get("pdno", "")
    qty = int(stock.get("hldg_qty", "0") or "0")
    avg = float(stock.get("pchs_avg_pric", "0") or "0")
    if qty > 0:
        print(f"  {name}({code}): {qty}주 @ {avg:,.0f}원")
