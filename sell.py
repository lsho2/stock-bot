#!/usr/bin/env python3
"""반자동 지정가 매도.

사용법:
    python sell.py [종목코드] [수량] [지정가]
    python sell.py 111770 22 89186
    → 영원무역 22주를 89,186원 지정가 매도

체결 후 monitor.py가 잔고 변화를 감지하여 trades.json 및 positions.json 자동 업데이트.
"""

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from stock_info import get_token, get_stock_info

load_dotenv()

KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.getenv("KIS_ACCOUNT", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"

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
        "name":    (out.get("hts_kor_isnm") or "").strip() or code,
        "price":   int(out.get("stck_prpr", "0") or "0"),
        "chg_pct": out.get("prdy_ctrt", ""),
    }


# ── 보유 확인 ─────────────────────────────────────────────────────

def _get_holding(code: str) -> dict | None:
    """positions.json에서 OPEN/HOLDING 포지션 반환. 없으면 None."""
    if not POSITIONS_PATH.exists():
        return None
    data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    return next(
        (p for p in data.get("positions", [])
         if p["code"] == code and p["status"] in ("OPEN", "HOLDING")),
        None,
    )


# ── 매도 주문 ─────────────────────────────────────────────────────

def _kis_sell(token: str, code: str, qty: int, limit_price: int) -> str:
    """KIS 모의 지정가 매도. 주문번호 반환."""
    cano, acnt_prdt = _account_parts()
    resp = requests.post(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_kis_headers("VTTC0801U", token),
        json={
            "CANO":         cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "PDNO":         code,
            "ORD_DVSN":     "00",           # 지정가
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(limit_price),
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1", "매도 주문 실패"))
    return data.get("output", {}).get("ODNO", "")


# ── 진입점 ────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    if len(sys.argv) != 4:
        print("사용법: python sell.py [종목코드] [수량] [지정가]")
        print("예:     python sell.py 111770 22 89186")
        sys.exit(1)

    code = sys.argv[1].strip()
    try:
        qty = int(sys.argv[2])
        if qty <= 0:
            raise ValueError
    except ValueError:
        sys.exit("수량은 1 이상의 정수로 입력하세요.")
    try:
        limit_price = int(sys.argv[3])
        if limit_price <= 0:
            raise ValueError
    except ValueError:
        sys.exit("지정가는 1 이상의 정수로 입력하세요.")

    # ── 1) 보유 확인 ─────────────────────────────────────────────────
    holding = _get_holding(code)
    if holding is None:
        sys.exit(f"[{code}] 보유 포지션 없음 (positions.json 확인)")
    held_qty  = holding["qty"]
    buy_price = holding["buy_price"]
    if qty > held_qty:
        sys.exit(f"매도 수량({qty}주) > 보유 수량({held_qty}주)")

    token = get_token()

    # ── 2) 현재가 조회 ───────────────────────────────────────────────
    print(f"\n[{code}] 시세 조회 중...")
    try:
        quote = _fetch_quote(token, code)
    except Exception as exc:
        sys.exit(f"시세 조회 실패: {exc}")

    name = quote["name"]
    if not name or name == code:
        try:
            info = get_stock_info(code)
            name = info.get("종목명") or code
        except Exception:
            name = code
    cur_price = quote["price"]
    chg_pct   = quote["chg_pct"]

    if cur_price <= 0:
        sys.exit("현재가 0원 — 장 시간 또는 종목코드 확인")

    try:
        chg_str = f"{float(chg_pct):+.2f}%" if chg_pct else ""
    except ValueError:
        chg_str = ""

    profit_pct     = (limit_price - buy_price) / buy_price * 100 if buy_price else 0.0
    profit_amt     = (limit_price - buy_price) * qty
    cur_profit_pct = (cur_price   - buy_price) / buy_price * 100 if buy_price else 0.0

    sep = "─" * 36
    print(f"\n{sep}")
    print(f"  종목명   : {name} ({code})")
    print(f"  현재가   : {cur_price:,}원  {chg_str}  (매수가 대비 {cur_profit_pct:+.1f}%)")
    print(f"  매수가   : {buy_price:,}원  (보유 {held_qty}주)")
    print(f"  매도 수량: {qty}주")
    print(f"  지정가   : {limit_price:,}원  ({profit_pct:+.1f}%)")
    print(f"  예상 수익: {profit_amt:+,}원")
    print(sep)

    # ── 3) 확인 ──────────────────────────────────────────────────────
    ans = input("정말 매도? (y/n): ").strip().lower()
    if ans != "y":
        print("매도 취소.")
        sys.exit(0)

    # ── 4) KIS 지정가 매도 주문 ──────────────────────────────────────
    print("주문 전송 중...")
    try:
        order_no = _kis_sell(token, code, qty, limit_price)
        print(f"  주문 완료 (주문번호: {order_no or '-'})")
    except Exception as exc:
        sys.exit(f"주문 실패: {exc}")

    # ── 5) 결과 출력 ─────────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"✅ 매도 주문 접수")
    print(f"  종목    : {name} ({code})")
    print(f"  지정가  : {limit_price:,}원 × {qty}주")
    print(f"  예상 수익: {profit_amt:+,}원 ({profit_pct:+.1f}%)")
    print(f"  체결 시 monitor.py가 잔고 변화 감지 → trades.json 자동 기록")
    print(sep)


if __name__ == "__main__":
    main()
