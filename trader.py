#!/usr/bin/env python3
"""KIS 모의투자 자동 매수 - last_analysis.txt 기반."""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

from stock_info import get_token
from telegram_alert import send_message

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.getenv("KIS_ACCOUNT", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"
SEED_MONEY     = int(os.getenv("SEED_MONEY", "10000000"))
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

LOG_DIR        = Path(r"C:\Stock\stock-bot")
ANALYSIS_PATH  = LOG_DIR / "last_analysis.txt"
POSITIONS_PATH = LOG_DIR / "positions.json"

_RETRY_DELAYS  = [10, 20, 40, 80, 120]

_SYSTEM_WEIGHT = (
    "당신은 포트폴리오 매니저입니다. "
    "매수근거/기술적신호/리스크를 종합해 종목별 투자 비중(%)을 결정하세요. "
    "비중의 합은 반드시 100이어야 합니다. "
    "JSON만 출력하세요. 다른 텍스트는 절대 포함하지 마세요. "
    '예시: {"weights": {"005930": 25, "000660": 20, "035720": 20, "003230": 20, "036570": 15}}'
)


def _parse_stocks(text: str) -> list[dict]:
    """last_analysis.txt에서 추천 종목 5개 파싱."""
    stocks = []
    blocks = re.split(r"\*\*\d+\.", text)
    for block in blocks[1:6]:
        try:
            m_name = re.match(r"\s*(.+?)\((\d{6})\)", block)
            if not m_name:
                continue
            name = m_name.group(1).strip().rstrip("*").strip()
            code = m_name.group(2)

            m_cur  = re.search(r"현재가:\s*([\d,]+)원", block)
            m_tgt  = re.search(r"목표가:\s*([\d,]+)원", block)
            m_stop = re.search(r"손절가:\s*([\d,]+)원", block)
            m_up   = re.search(r"upside:\s*([+\-\d.]+%)", block)
            m_rsn  = re.search(r"매수 근거:\s*(.+)", block)
            m_tec  = re.search(r"기술적 신호:\s*(.+)", block)

            stocks.append({
                "code":         code,
                "name":         name,
                "buy_price":    int(m_cur.group(1).replace(",", ""))  if m_cur  else 0,
                "target_price": int(m_tgt.group(1).replace(",", ""))  if m_tgt  else 0,
                "stop_loss":    int(m_stop.group(1).replace(",", "")) if m_stop else 0,
                "upside":       m_up.group(1)  if m_up  else "",
                "reason":       m_rsn.group(1).strip() if m_rsn else "",
                "tech":         m_tec.group(1).strip() if m_tec else "",
            })
        except Exception as exc:
            print(f"  [파싱 스킵] {exc}")
    return stocks


def _get_weights(stocks: list[dict]) -> dict[str, int]:
    """Gemini Flash로 종목별 투자 비중(%) 결정."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = "아래 종목들에 대해 투자 비중(%)을 결정하세요. 합이 100이 되도록.\n\n" + "\n".join(
        f"{s['code']} {s['name']}: 매수근거={s['reason']}, 기술신호={s['tech']}, upside={s['upside']}"
        for s in stocks
    )

    def _equal_weights() -> dict[str, int]:
        n = len(stocks)
        base = 100 // n
        w = {s["code"]: base for s in stocks}
        w[stocks[0]["code"]] += 100 - base * n
        return w

    last_exc: Exception | None = None
    max_attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_WEIGHT,
                    max_output_tokens=1024,
                    temperature=0,
                ),
            )
            raw = resp.text or ""
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                print("  [비중] JSON 없음, 균등 배분 사용")
                return _equal_weights()
            data = json.loads(m.group())
            return {k: int(v) for k, v in data.get("weights", {}).items()}
        except json.JSONDecodeError as exc:
            print(f"  [비중] JSON 파싱 실패, 균등 배분 사용: {exc}")
            return _equal_weights()
        except Exception as exc:
            if "503" not in str(exc) and "ServerError" not in type(exc).__name__:
                raise
            last_exc = exc
            if attempt <= len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[attempt - 1]
                print(f"Gemini 503 ({attempt}/5), {delay}초 후 재시도...")
                time.sleep(delay)
    raise last_exc


def _kis_order(token: str, code: str, qty: int) -> str:
    """KIS 모의 시장가 매수 주문. 주문번호 반환."""
    account = KIS_ACCOUNT.replace("-", "").strip()
    if len(account) == 8:
        cano = account
        acnt_prdt_cd = "01"
    elif len(account) == 10:
        cano = account[:8]
        acnt_prdt_cd = account[8:]
    else:
        raise ValueError(f"KIS_ACCOUNT 길이 이상 ({len(account)}자리): {account}")

    headers = {
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         "VTTC0802U",
        "custtype":      "P",
        "Content-Type":  "application/json",
    }
    body = {
        "CANO":         cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "PDNO":         code,
        "ORD_DVSN":     "01",
        "ORD_QTY":      str(qty),
        "ORD_UNPR":     "0",
    }
    resp = requests.post(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=headers, json=body, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1", "주문 실패"))
    return data.get("output", {}).get("ODNO", "")


def main() -> None:
    if not ANALYSIS_PATH.exists():
        sys.exit(f"분석 파일 없음: {ANALYSIS_PATH}")

    text = ANALYSIS_PATH.read_text(encoding="utf-8")
    print(f"[1/5] 분석 파일 로드 완료 ({len(text):,}자)")

    stocks = _parse_stocks(text)
    if not stocks:
        sys.exit("종목 파싱 실패: last_analysis.txt 형식 확인")
    print(f"[2/5] 종목 파싱 완료: {', '.join(s['name'] for s in stocks)}")

    print("[3/5] Gemini 비중 결정 중...")
    weights = _get_weights(stocks)
    print(f"  비중: {weights}")

    print("[4/5] 매수 주문 처리 중...")
    token = get_token() if not DRY_RUN else ""

    buy_date = datetime.now().strftime("%Y-%m-%d")
    positions = []
    order_lines = []
    total_ordered = 0
    telegram_errors = []

    for s in stocks:
        code  = s["code"]
        ratio = weights.get(code, 0)

        if ratio <= 0 or s["buy_price"] <= 0:
            print(f"  [{s['name']}] 비중/현재가 0 → 스킵")
            continue

        qty           = (SEED_MONEY * ratio // 100) // s["buy_price"]
        actual_amount = qty * s["buy_price"]

        if qty <= 0:
            print(f"  [{s['name']}] 수량 0 → 스킵")
            continue

        order_no = ""
        status   = "PENDING"

        if DRY_RUN:
            print(f"  [DRY_RUN] {s['name']}({code}) {qty}주 @ {s['buy_price']:,}원 ({ratio}%)")
            order_no = "DRY_RUN"
        else:
            try:
                order_no = _kis_order(token, code, qty)
                print(f"  [{s['name']}] 주문 완료: {qty}주 @ {s['buy_price']:,}원 (주문번호: {order_no})")
            except Exception as exc:
                err_msg = str(exc)
                print(f"  [{s['name']}] 주문 실패: {err_msg}")
                telegram_errors.append(f"❌ {s['name']} 실패: {err_msg}")
                status = "FAILED"

        positions.append({
            "code":         code,
            "name":         s["name"],
            "buy_price":    s["buy_price"],
            "qty":          qty,
            "amount":       actual_amount,
            "target_price": s["target_price"],
            "stop_loss":    s["stop_loss"],
            "ratio_pct":    ratio,
            "order_no":     order_no,
            "status":       status,
            "buy_time":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        total_ordered += actual_amount
        order_lines.append(
            f"{len(order_lines)+1}. {s['name']} {qty}주 @ {s['buy_price']:,}원 ({ratio}%)"
        )

    print("[5/5] 결과 저장 중...")
    POSITIONS_PATH.write_text(
        json.dumps({"buy_date": buy_date, "seed_money": SEED_MONEY, "positions": positions},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  positions.json 저장: {POSITIONS_PATH}")

    unused = SEED_MONEY - total_ordered
    msg = "\n".join([
        "🚀 자동 매수 주문 완료",
        *order_lines,
        f"\n총 주문 금액: {total_ordered:,}원 / {SEED_MONEY:,}원",
        f"미사용 현금: {unused:,}원",
        *(([""] + telegram_errors) if telegram_errors else []),
    ])
    send_message(msg)
    print("완료.")


if __name__ == "__main__":
    main()
