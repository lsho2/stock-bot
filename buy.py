#!/usr/bin/env python3
"""반자동 매수 + 체결가 기록.

사용법:
    python buy.py [종목코드] [수량]
    python buy.py 035900 30
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from stock_info import get_token
from tracker import log_buy

load_dotenv()

KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.getenv("KIS_ACCOUNT", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"
CHECK_INTERVAL = os.getenv("CHECK_INTERVAL", "3")

POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")


# ── KIS 공통 ──────────────────────────────────────────────────────

def _account_parts() -> tuple[str, str]:
    acct = KIS_ACCOUNT.replace("-", "").strip()
    return acct[:8], acct[8:] or "01"


def _kis_headers(tr_id: str, token: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
        "Content-Type":  "application/json",
    }


# ── 시세 조회 ─────────────────────────────────────────────────────

def _fetch_quote(token: str, code: str) -> dict:
    """현재가·종목명·등락률 조회."""
    resp = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
        headers={
            "authorization": f"Bearer {token}",
            "appkey":        KIS_APP_KEY,
            "appsecret":     KIS_APP_SECRET,
            "tr_id":         "FHKST01010100",
            "custtype":      "P",
        },
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        timeout=10,
    )
    resp.raise_for_status()
    out = resp.json().get("output", {})
    return {
        "name":    out.get("hts_kor_isnm", code),
        "price":   int(out.get("stck_prpr", "0") or "0"),
        "chg_pct": out.get("prdy_ctrt", ""),
    }


# ── 매수 주문 ─────────────────────────────────────────────────────

def _kis_buy(token: str, code: str, qty: int) -> str:
    """KIS 모의 시장가 매수. 주문번호 반환."""
    cano, acnt_prdt = _account_parts()
    resp = requests.post(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_kis_headers("VTTC0802U", token),
        json={
            "CANO":         cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "PDNO":         code,
            "ORD_DVSN":     "01",   # 시장가
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     "0",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1", "매수 주문 실패"))
    return data.get("output", {}).get("ODNO", "")


# ── 체결가 확인 ───────────────────────────────────────────────────

def _get_filled_price(token: str, code: str) -> int | None:
    """잔고 조회 → 해당 종목 평균 매수가 반환. 없으면 None."""
    cano, acnt_prdt = _account_parts()
    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        KIS_APP_KEY,
                "appsecret":     KIS_APP_SECRET,
                "tr_id":         "VTTC8434R",
                "custtype":      "P",
            },
            params={
                "CANO":                  cano,
                "ACNT_PRDT_CD":          acnt_prdt,
                "AFHR_FLPR_YN":          "N",
                "OFL_YN":                "",
                "INQR_DVSN":             "02",
                "UNPR_DVSN":             "01",
                "FUND_STTL_ICLD_YN":     "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN":             "01",
                "CTX_AREA_FK100":        "",
                "CTX_AREA_NK100":        "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        for item in resp.json().get("output1", []):
            if item.get("pdno") == code:
                return int(float(item.get("pchs_avg_pric", "0")))
    except Exception as exc:
        print(f"  잔고 조회 실패: {exc}")
    return None


# ── positions.json 업데이트 ────────────────────────────────────────

def _add_to_positions(
    code: str, name: str, buy_price: int, qty: int, order_no: str
) -> None:
    """positions.json 에 새 포지션 추가.
    이미 OPEN 이면 수량 합산 후 평단·손절·목표가 재계산."""
    if not POSITIONS_PATH.exists():
        print("  [경고] positions.json 없음 → 포지션 미등록")
        return

    data      = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    positions = data.get("positions", [])

    existing = next(
        (p for p in positions if p["code"] == code and p["status"] == "OPEN"),
        None,
    )
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if existing:
        old_amt  = existing["buy_price"] * existing["qty"]
        new_amt  = buy_price * qty
        new_qty  = existing["qty"] + qty
        new_avg  = (old_amt + new_amt) // new_qty
        existing["buy_price"]    = new_avg
        existing["qty"]          = new_qty
        existing["stop_loss"]    = int(new_avg * 0.95)
        existing["target_price"] = int(new_avg * 1.10)
        existing["last_seen_price"] = new_avg
        print(f"  [포지션] 추가 매수 반영: 평단 {new_avg:,}원 × {new_qty}주")
    else:
        positions.append({
            "code":            code,
            "name":            name,
            "buy_price":       buy_price,
            "qty":             qty,
            "stop_loss":       int(buy_price * 0.95),
            "target_price":    int(buy_price * 1.10),
            "status":          "OPEN",
            "trailing_active": False,
            "peak_price":      0,
            "last_seen_price": buy_price,
            "buy_order_no":    order_no,
            "buy_time":        now_str,
        })
        print(f"  [포지션] 신규 추가: {name}({code})")

    data["positions"] = positions
    POSITIONS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 진입점 ────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    if len(sys.argv) != 3:
        print("사용법: python buy.py [종목코드] [수량]")
        print("예:     python buy.py 035900 30")
        sys.exit(1)

    code = sys.argv[1].strip()
    try:
        qty = int(sys.argv[2])
        if qty <= 0:
            raise ValueError
    except ValueError:
        sys.exit("수량은 1 이상의 정수로 입력하세요.")

    token = get_token()

    # ── 1) 현재가 조회 ───────────────────────────────────────────────
    print(f"\n[{code}] 시세 조회 중...")
    try:
        quote = _fetch_quote(token, code)
    except Exception as exc:
        sys.exit(f"시세 조회 실패: {exc}")

    name      = quote["name"]
    cur_price = quote["price"]
    chg_pct   = quote["chg_pct"]

    if cur_price <= 0:
        sys.exit("현재가 0원 — 장 시간 또는 종목코드 확인")

    try:
        chg_str = f"{float(chg_pct):+.2f}%" if chg_pct else ""
    except ValueError:
        chg_str = ""

    sep = "─" * 32
    print(f"\n{sep}")
    print(f"  종목명 : {name} ({code})")
    print(f"  현재가 : {cur_price:,}원  {chg_str}")
    print(f"  수량   : {qty}주")
    print(f"  예상액 : {cur_price * qty:,}원")
    print(f"  예상 손절: {int(cur_price * 0.95):,}원 (-5%)")
    print(f"  예상 목표: {int(cur_price * 1.10):,}원 (+10%)")
    print(sep)

    # ── 2) 확인 ──────────────────────────────────────────────────────
    ans = input("정말 매수? (y/n): ").strip().lower()
    if ans != "y":
        print("매수 취소.")
        sys.exit(0)

    # ── 3) KIS 매수 주문 ─────────────────────────────────────────────
    print("주문 전송 중...")
    try:
        order_no = _kis_buy(token, code, qty)
        print(f"  주문 완료 (주문번호: {order_no or '-'})")
    except Exception as exc:
        sys.exit(f"주문 실패: {exc}")

    # ── 4) 체결 확인 (3초 대기 → 잔고 평단가) ───────────────────────
    print("체결 확인 중 (3초 대기)...")
    time.sleep(3)
    filled_price = _get_filled_price(token, code)
    if filled_price and filled_price > 0:
        print(f"  체결가 (잔고 평단): {filled_price:,}원")
    else:
        filled_price = cur_price
        print(f"  체결가 조회 실패 → 현재가 {filled_price:,}원으로 대체")

    filled_amount = filled_price * qty

    # ── 5) tracker 기록 ──────────────────────────────────────────────
    log_buy(code, name, filled_price, qty, source="buy.py")

    # ── 6) positions.json 업데이트 ───────────────────────────────────
    _add_to_positions(code, name, filled_price, qty, order_no)

    # ── 7) 결과 출력 ─────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"✅ 매수 완료")
    print(f"  종목  : {name} ({code})")
    print(f"  체결가: {filled_price:,}원 × {qty}주")
    print(f"  체결액: {filled_amount:,}원")
    print(f"  손절가: {int(filled_price * 0.95):,}원 (-5%)")
    print(f"  목표가: {int(filled_price * 1.10):,}원 (+10%)")
    print(f"  trades.json 자동 기록 완료")
    print(f"  monitor.py 가 {CHECK_INTERVAL}분 안에 손절/익절 자동 적용")
    print(sep)


if __name__ == "__main__":
    main()
