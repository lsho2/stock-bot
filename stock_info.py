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


def _get_token_from(base_url: str, token_file: str) -> str:
    if os.path.exists(token_file):
        with open(token_file, "r") as f:
            data = json.load(f)
        if time.time() - data["time"] < 82800:
            return data["token"]

    url = f"{base_url}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }
    res = requests.post(url, json=body, timeout=10)
    token = res.json()["access_token"]

    with open(token_file, "w") as f:
        json.dump({"token": token, "time": time.time()}, f)
    return token


def get_token() -> str:
    return _get_token_from(KIS_BASE_URL, TOKEN_FILE)


def get_real_token() -> str:
    return _get_token_from(KIS_REAL_URL, "token_real.json")


def _fetch_kis(stock_code: str) -> dict:
    """KIS API로 현재가, 52주 최고/최저, PER, 외국인보유율 조회.

    KIS 모의투자 서버가 연속 호출 시 빈 output을 반환하는 경우가 있어
    rt_cd != '0' 또는 stck_prpr 이 비면 최대 2회 재시도.
    """
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
    out: dict = {}
    for attempt in range(3):
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        if DEBUG:
            print(f"  [DEBUG] KIS inquire-price 응답:\n{json.dumps(data, ensure_ascii=False, indent=2)}")
        out = data.get("output", {})
        # rt_cd='0' + stck_prpr 있으면 정상
        if data.get("rt_cd") == "0" and out.get("stck_prpr") and out["stck_prpr"] != "0":
            break
        if attempt < 2:
            time.sleep(0.3)
            if DEBUG:
                print(f"  [DEBUG _fetch_kis] {stock_code} 재시도 {attempt+1}/2 (rt_cd={data.get('rt_cd')!r})")

    def fmt_price(v: str) -> str:
        return f"{int(v):,}원" if v and v.strip("0") else ""

    if DEBUG:
        print(f"  [DEBUG] hts_kor_isnm={out.get('hts_kor_isnm')!r}  bstp_kor_isnm={out.get('bstp_kor_isnm')!r}  stck_shrn_iscd={out.get('stck_shrn_iscd')!r}")

    chg_rt = out.get("prdy_ctrt", "")
    try:
        chg_str = f"{float(chg_rt):+.2f}%" if chg_rt else ""
    except ValueError:
        chg_str = ""

    return {
        "종목명":   out.get("hts_kor_isnm", ""),
        "현재가":   fmt_price(out.get("stck_prpr", "")),
        "시가":     fmt_price(out.get("stck_oprc", "")),
        "고가":     fmt_price(out.get("stck_hgpr", "")),
        "저가":     fmt_price(out.get("stck_lwpr", "")),
        "등락률":   chg_str,
        "52주최고": fmt_price(out.get("w52_hgpr", "")),
        "52주최저": fmt_price(out.get("w52_lwpr", "")),
        "PER":      f"{out['per']}배" if out.get("per") and out["per"] != "0.00" else "",
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


def get_chart_data(stock_code: str, limit: int = 20) -> list[dict]:
    """KIS API로 일봉 데이터 조회.

    limit: 반환할 최대 행 수 (기본 20). MA60 계산 시 90 이상 지정.
    반환: [{"날짜": "20260514", "종가": ..., "거래량": ...}, ...] (최신→과거 순)
    """
    from datetime import datetime, timedelta
    token = get_token()
    url = f"{KIS_REAL_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
    today = datetime.now().strftime("%Y%m%d")
    cal_days = max(40, limit * 2)  # 거래일 확보를 위해 달력일 2배 조회
    start = (datetime.now() - timedelta(days=cal_days)).strftime("%Y%m%d")
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_code,
        "fid_period_div_code": "D",
        "fid_org_adj_prc": "1",
        "fid_input_date_1": start,
        "fid_input_date_2": today,
    }
    try:
        resp = requests.get(
            url,
            headers=_kis_headers(token, "FHKST01010400"),
            params=params,
            timeout=10,
        )
        data = resp.json()
        output = data.get("output") or []
    except Exception as exc:
        print(f"  일봉 조회 오류 ({stock_code}): {exc}")
        return []

    result = []
    for row in output[:limit]:
        date = row.get("stck_bsop_date", "")
        close = int(row["stck_clpr"]) if row.get("stck_clpr") else 0
        vol = int(row["acml_vol"]) if row.get("acml_vol") else 0
        result.append({
            "날짜": date,
            "종가": close,
            "거래량": vol,
        })
    return result


def _ema(values: list[float], period: int) -> list[float]:
    """지수이동평균(EMA). values는 오래된 순서(oldest-first)."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _calc_indicators(closes: list[float], investor: list[dict]) -> dict:
    """종가 리스트(최신→과거 순)와 외국인 데이터로 기술적 지표 계산."""
    result: dict = {}
    if not closes:
        return result

    rev = list(reversed(closes))  # 오래된→최신 순으로 변환
    n = len(rev)

    def _ma(period: int) -> float | None:
        if n < period:
            return None
        return round(sum(rev[-period:]) / period)

    ma5 = _ma(5)
    ma20 = _ma(20)
    ma60 = _ma(60)

    if ma5 is not None:
        result["MA5"] = ma5
    if ma20 is not None:
        result["MA20"] = ma20
    if ma60 is not None:
        result["MA60"] = ma60

    if ma20 is not None:
        result["MA20_위"] = closes[0] >= ma20

    if ma5 is not None and ma20 is not None:
        result["골든크로스"] = ma5 > ma20

    # ── RSI(14) ────────────────────────────────────────────────────────────
    if n >= 15:
        gains, losses = [], []
        for i in range(1, n):
            diff = rev[i] - rev[i - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))
        avg_gain = sum(gains[:14]) / 14
        avg_loss = sum(losses[:14]) / 14
        for i in range(14, len(gains)):
            avg_gain = (avg_gain * 13 + gains[i]) / 14
            avg_loss = (avg_loss * 13 + losses[i]) / 14
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        result["RSI14"] = round(rsi, 1)

    # ── MACD(12,26,9) ──────────────────────────────────────────────────────
    if n >= 34:
        ema12 = _ema(rev, 12)
        ema26 = _ema(rev, 26)
        # ema12[i+14]과 ema26[i]는 같은 시점에 대응 (26-12=14)
        macd_line = [ema12[i + 14] - ema26[i] for i in range(len(ema26))]
        if len(macd_line) >= 9:
            signal_line = _ema(macd_line, 9)
            macd_val = macd_line[-1]
            signal_val = signal_line[-1]
            result["MACD"] = round(macd_val, 2)
            result["MACD_signal"] = round(signal_val, 2)
            result["MACD_histogram"] = round(macd_val - signal_val, 2)

    # ── 외국인 연속 순매수 일수 ───────────────────────────────────────────
    if investor:
        streak = 0
        for row in investor:  # 최신→과거 순
            if row["외국인순매수"] > 0:
                streak += 1
            else:
                break
        result["외국인연속순매수"] = streak

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


def get_intraday_info(stock_code: str) -> dict:
    """KIS FHKST01010100으로 장중 실시간 정보 조회.

    _fetch_kis 를 재사용하므로 별도 HTTP 요청 없음.
    반환: {시가, 고가, 저가, 현재가, 등락률}
    """
    try:
        kis = _fetch_kis(stock_code)
    except Exception as exc:
        print(f"  장중 정보 조회 오류 ({stock_code}): {exc}")
        return {}
    return {
        "시가":   kis.get("시가", ""),
        "고가":   kis.get("고가", ""),
        "저가":   kis.get("저가", ""),
        "현재가": kis.get("현재가", ""),
        "등락률": kis.get("등락률", ""),
    }


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

    try:
        chart = get_chart_data(stock_code, limit=90)
        investor = get_investor_data(stock_code)
        closes = [r["종가"] for r in chart]
        info.update(_calc_indicators(closes, investor))
    except Exception as exc:
        print(f"  지표 계산 오류 ({stock_code}): {exc}")

    return info


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "005930"

    print("=== 기본 정보 + 기술적 지표 ===")
    for k, v in get_stock_info(code).items():
        print(f"  {k}: {v}")

    print("\n=== 일봉 (최근 20일) ===")
    for row in get_chart_data(code, limit=20):
        print(f"  {row['날짜']}  종가:{row['종가']:,}  거래량:{row['거래량']:,}")

    print("\n=== 외국인/기관 순매수 (최근 20일) ===")
    for row in get_investor_data(code):
        print(f"  {row['날짜']}  외국인:{row['외국인순매수']:+,}  기관:{row['기관순매수']:+,}")
