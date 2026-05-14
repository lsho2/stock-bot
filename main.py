import requests
import os
import time
import json
from dotenv import load_dotenv

load_dotenv()

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
ACCOUNT = os.getenv("KIS_ACCOUNT")

BASE_URL = "https://openapivts.koreainvestment.com:29443"
TOKEN_FILE = "token.json"

def get_token():
    # 저장된 토큰 있으면 재사용
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        if time.time() - data["time"] < 82800:
            print("저장된 토큰 재사용")
            return data["token"]

    # 새 토큰 발급
    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    res = requests.post(url, json=body)
    token = res.json()["access_token"]

    # 파일에 저장
    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": token, "time": time.time()}, f)

    print("새 토큰 발급 완료!")
    return token

def get_stock_price(token, stock_code):
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100"
    }
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_code
    }
    res = requests.get(url, headers=headers, params=params)
    output = res.json()["output"]
    name = output["bstp_kor_isnm"]
    price = int(output["stck_prpr"])
    change = int(output["prdy_vrss"])
    change_rate = float(output["prdy_ctrt"])
    sign = "▲" if output["prdy_vrss_sign"] == "2" else "▼"
    print(f"{stock_code} | {name} | 현재가: {price:,}원 | {sign} {change:,}원 ({change_rate}%)")
    return price

if __name__ == "__main__":
    token = get_token()
    get_stock_price(token, "005930")  # 삼성전자
    get_stock_price(token, "000660")  # SK하이닉스