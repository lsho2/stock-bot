#!/usr/bin/env python3
"""KIS API + 네이버 증권으로 종목 기본 정보 조회."""

import os
import json
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

DEBUG = os.getenv("DEBUG", "")

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
KIS_BASE_URL = "https://openapivts.koreainvestment.com:29443"
KIS_REAL_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_FILE = "token.json"

_naver = requests.Session()
_naver.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
    "Accept-Language": "ko-KR,ko;q=0.9",
})


def get_token() -> str:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        if time.time() - data["time"] < 82800:
            return data["token"]

    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }
    res = requests.post(url, json=body, timeout=10)
    token = res.json()["access_token"]

    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": token, "time": time.time()}, f)
    return token


def _fetch_kis(stock_code: str) -> dict:
    """KIS API로 현재가, 52주 최고/최저, PER, 외국인보유율 조회."""
    token = get_token()
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100",
    }
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_code,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    data = resp.json()
    if DEBUG:
        print(f"  [DEBUG] KIS inquire-price 응답:\n{json.dumps(data, ensure_ascii=False, indent=2)}")
    out = data.get("output", {})

    def fmt_price(v: str) -> str:
        return f"{int(v):,}원" if v and v.strip("0") else ""

    if DEBUG:
        print(f"  [DEBUG] hts_kor_isnm={out.get('hts_kor_isnm')!r}  bstp_kor_isnm={out.get('bstp_kor_isnm')!r}  stck_shrn_iscd={out.get('stck_shrn_iscd')!r}")
    return {
        "종목명": out.get("hts_kor_isnm", ""),
        "현재가": fmt_price(out.get("stck_prpr", "")),
        "52주최고": fmt_price(out.get("w52_hgpr", "")),
        "52주최저": fmt_price(out.get("w52_lwpr", "")),
        "PER": f"{out['per']}배" if out.get("per") and out["per"] != "0.00" else "",
        "외국인보유율": f"{out['hts_frgn_ehrt']}%" if out.get("hts_frgn_ehrt") else "",
    }


def _crawl_naver(stock_code: str) -> dict:
    """네이버 증권 크롤링으로 시가총액, PBR, 배당수익률, 목표주가 조회."""
    url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
    resp = _naver.get(url, timeout=10)
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    result = {}

    name_tag = soup.select_one("div.wrap_company h2 a")
    if name_tag:
        result["종목명"] = name_tag.get_text(strip=True)

    def _extract(key: str, val: str) -> None:
        val = val.strip()
        if not val or val in ("-", "N/A", ""):
            return
        if "시가총액" in key:
            result["시가총액"] = val
        elif "목표주가" in key:
            result["목표주가"] = val
        elif "PBR" in key and "PBR" not in result:
            result["PBR"] = val
        elif "ROE" in key:
            result["ROE"] = val
        elif "배당수익률" in key:
            result["배당수익률"] = val

    for table in soup.select("table.tb_type1, table.per_table"):
        for tr in table.select("tr"):
            cells = tr.select("th, td")
            i = 0
            while i + 1 < len(cells):
                if cells[i].name == "th" and cells[i + 1].name == "td":
                    _extract(
                        cells[i].get_text(strip=True),
                        cells[i + 1].get_text(strip=True),
                    )
                    i += 2
                else:
                    i += 1

    return result


def _kis_headers(token: str, tr_id: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
    }


def get_chart_data(stock_code: str) -> list[dict]:
    """KIS API로 최근 20일 일봉 데이터 조회.

    반환: [{"날짜": "20260514", "시가": 187100, "고가": ..., "저가": ..., "종가": ..., "거래량": ...}, ...]
    """
    token = get_token()
    url = f"{KIS_REAL_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_code,
        "fid_period_div_code": "D",
        "fid_org_adj_prc": "1",
    }
    try:
        resp = requests.get(
            url,
            headers=_kis_headers(token, "FHKST01010400"),
            params=params,
            timeout=10,
        )
        data = resp.json()
        if DEBUG:
            print(f"  [DEBUG] 일봉 응답 전체:\n{json.dumps(data, ensure_ascii=False, indent=2)}")
        else:
            print(f"  [일봉] rt_cd={data.get('rt_cd')}  msg1={data.get('msg1')}  output2 건수={len(data.get('output2') or [])}")
        output = data.get("output2") or []
    except Exception as exc:
        print(f"  일봉 조회 오류 ({stock_code}): {exc}")
        return []

    result = []
    for row in output[:20]:
        result.append({
            "날짜": row.get("stck_bsop_date", ""),
            "시가": int(row["stck_oprc"]) if row.get("stck_oprc") else 0,
            "고가": int(row["stck_hgpr"]) if row.get("stck_hgpr") else 0,
            "저가": int(row["stck_lwpr"]) if row.get("stck_lwpr") else 0,
            "종가": int(row["stck_clpr"]) if row.get("stck_clpr") else 0,
            "거래량": int(row["acml_vol"]) if row.get("acml_vol") else 0,
        })
    return result


def get_investor_data(stock_code: str) -> list[dict]:
    """KIS API로 최근 20일 외국인/기관 순매수 데이터 조회.

    반환: [{"날짜": "20260514", "외국인순매수": 12345, "기관순매수": -6789}, ...]
    """
    token = get_token()
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor"
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_code,
    }
    try:
        resp = requests.get(
            url,
            headers=_kis_headers(token, "FHKST01010900"),
            params=params,
            timeout=10,
        )
        output = resp.json().get("output", [])
    except Exception as exc:
        print(f"  투자자 조회 오류 ({stock_code}): {exc}")
        return []

    result = []
    for row in output[:20]:
        result.append({
            "날짜": row.get("stck_bsop_date", ""),
            "외국인순매수": int(row["frgn_ntby_qty"]) if row.get("frgn_ntby_qty") else 0,
            "기관순매수": int(row["orgn_ntby_qty"]) if row.get("orgn_ntby_qty") else 0,
        })
    return result


def get_stock_info(stock_code: str) -> dict:
    """종목코드로 KIS API + 네이버 증권 기업정보를 조회해 dict 반환."""
    info: dict = {"종목코드": stock_code}

    try:
        info.update(_fetch_kis(stock_code))
    except Exception as exc:
        print(f"  KIS 오류 ({stock_code}): {exc}")

    try:
        naver = _crawl_naver(stock_code)
        # 네이버 종목명이 있으면 KIS 필드(업종명이 섞일 수 있음)보다 우선
        if naver.get("종목명"):
            info["종목명"] = naver.pop("종목명")
        for k, v in naver.items():
            if v and not info.get(k):
                info[k] = v
    except Exception as exc:
        print(f"  Naver 오류 ({stock_code}): {exc}")

    return info


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"

    print("=== 기본 정보 ===")
    for k, v in get_stock_info(code).items():
        print(f"  {k}: {v}")

    print("\n=== 일봉 (최근 20일) ===")
    for row in get_chart_data(code):
        print(f"  {row['날짜']}  종가:{row['종가']:,}  거래량:{row['거래량']:,}")

    print("\n=== 외국인/기관 순매수 (최근 20일) ===")
    for row in get_investor_data(code):
        print(f"  {row['날짜']}  외국인:{row['외국인순매수']:+,}  기관:{row['기관순매수']:+,}")
