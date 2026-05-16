#!/usr/bin/env python3
"""일간 손익 보고서 생성 및 텔레그램 전송."""

import json
import os
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from stock_info import get_token
from telegram_alert import send_message
from tracker import log_daily_pnl
from market_context import get_market_context

load_dotenv()

KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"

POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")
TRADES_PATH    = Path(r"C:\Stock\data\trades.json")
DAILY_PNL_PATH = Path(r"C:\Stock\data\daily_pnl.json")
RECO_PATH      = Path(r"C:\Stock\data\recommendations.json")

REASON_LABEL = {
    "stop_loss":   "손절",
    "trailing":    "트레일링",
    "take_profit": "익절",
    "manual":      "수동",
}


# ── KIS API 조회 ──────────────────────────────────────────────────

def _fetch_price(token: str, code: str) -> int:
    """KIS 현재가 조회. 실패 시 0 반환."""
    try:
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
        return int(resp.json()["output"]["stck_prpr"])
    except Exception as exc:
        print(f"  [{code}] 현재가 조회 실패: {exc}")
        return 0


def _fetch_kospi_rate(token: str) -> float | None:
    """코스피 지수 전일대비율(%) 조회. 실패 시 None (알파 섹션 생략)."""
    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-price",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        KIS_APP_KEY,
                "appsecret":     KIS_APP_SECRET,
                "tr_id":         "FHKUP03500100",
                "custtype":      "P",
            },
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001"},
            timeout=10,
        )
        resp.raise_for_status()
        out  = resp.json().get("output", {})
        rate = out.get("bstp_nmix_prdy_ctrt", "")
        return float(rate) if rate else None
    except Exception as exc:
        print(f"  [코스피] 조회 실패 (알파 생략): {exc}")
        return None


# ── 매도 레코드 보완 ──────────────────────────────────────────────

def _enrich_sell(sell: dict, pos_map: dict, buy_map: dict) -> dict:
    """buy_price == sell_price 또는 qty == 0 인 손상 레코드를
    positions.json 또는 BUY 거래 기록으로 보완."""
    bp  = sell.get("buy_price", 0)
    sp  = sell.get("price", 0)
    qty = sell.get("qty", 0)

    if bp == sp or qty == 0:
        code = sell.get("code", "")
        src  = pos_map.get(code) or buy_map.get(code)
        if src:
            bp   = src.get("buy_price") or src.get("price") or bp
            qty  = src.get("qty", qty)
            name = src.get("name", sell.get("name", code))
        else:
            name = sell.get("name", code)

        if bp and qty:
            profit_amt = (sp - bp) * qty
            profit_pct = round((sp - bp) / bp * 100, 2)
        else:
            profit_amt = sell.get("profit_amt", 0)
            profit_pct = sell.get("profit_pct", 0.0)

        return {**sell, "buy_price": bp, "qty": qty,
                "profit_amt": profit_amt, "profit_pct": profit_pct, "name": name}

    return sell


# ── 보고서 생성 ───────────────────────────────────────────────────

def _build_report(
    seed_money: int,
    today: str,
    price_map: dict[str, int],
    kospi_rate: float | None,
) -> str:
    """일간 보고서 문자열 생성."""

    # positions.json 전체 (OPEN + CLOSED 모두 enrich에 필요)
    pos_data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    pos_map  = {p["code"]: p for p in pos_data.get("positions", [])}
    open_pos = [p for p in pos_data["positions"] if p["status"] in ("OPEN", "HOLDING")]

    # trades.json
    trade_data = (
        json.loads(TRADES_PATH.read_text(encoding="utf-8"))
        if TRADES_PATH.exists() else {}
    )
    all_trades = trade_data.get("trades", [])

    # BUY 맵 (code → 가장 마지막 BUY)
    buy_map: dict[str, dict] = {}
    for t in all_trades:
        if t.get("type") == "BUY":
            buy_map[t["code"]] = t

    # 오늘 SELL (보완 적용)
    raw_sells = [t for t in all_trades if t.get("date") == today and t.get("type") == "SELL"]
    sells     = [_enrich_sell(t, pos_map, buy_map) for t in raw_sells]

    # reason 기준 분류
    profit_sells = [t for t in sells if t.get("reason") in ("trailing", "take_profit")]
    loss_sells   = [t for t in sells if t.get("reason") == "stop_loss"]
    manual_sells = [t for t in sells if t.get("reason") not in ("trailing", "take_profit", "stop_loss")]

    # 실현 손익 (보완된 profit_amt 합산)
    realized    = sum(t.get("profit_amt", 0) for t in sells)
    real_pct    = realized / seed_money * 100 if seed_money else 0.0

    # 평가 손익
    unrealized  = sum(
        (price_map.get(p["code"], p.get("last_seen_price") or p["buy_price"]) - p["buy_price"]) * p["qty"]
        for p in open_pos
    )
    unrl_pct    = unrealized / seed_money * 100 if seed_money else 0.0

    total_pnl   = realized + unrealized
    total_pct   = total_pnl / seed_money * 100 if seed_money else 0.0
    net_assets  = seed_money + total_pnl

    # 추천 기록 건수
    reco_cnt = 0
    if RECO_PATH.exists():
        reco_data = json.loads(RECO_PATH.read_text(encoding="utf-8"))
        reco_cnt  = sum(1 for r in reco_data.get("records", []) if r.get("date") == today)

    # ── 조립 ──────────────────────────────────────────────────────
    sep   = "─" * 24
    lines = [f"📊 [{today}] 일간 거래 요약", sep]

    # 익절 섹션
    profit_total = sum(t.get("profit_amt", 0) for t in profit_sells)
    lines.append(f"✅ 익절 {len(profit_sells)}건  {profit_total:+,}원")
    for t in profit_sells:
        label = REASON_LABEL.get(t.get("reason", ""), t.get("reason", ""))
        lines.append(
            f"  - {t['name']}: {t.get('buy_price', 0):,} → {t.get('price', 0):,}"
            f" ({t.get('profit_pct', 0.0):+.1f}%) [{label}]"
        )

    # 손절 섹션
    loss_total = sum(t.get("profit_amt", 0) for t in loss_sells)
    lines.append(f"🛡️ 손절 {len(loss_sells)}건  {loss_total:+,}원")
    for t in loss_sells:
        label = REASON_LABEL.get(t.get("reason", ""), t.get("reason", ""))
        lines.append(
            f"  - {t['name']}: {t.get('buy_price', 0):,} → {t.get('price', 0):,}"
            f" ({t.get('profit_pct', 0.0):+.1f}%) [{label}]"
        )

    # 보유 섹션
    lines.append(f"📦 보유 {len(open_pos)}건")
    for pos in open_pos:
        bp  = pos["buy_price"]
        cp  = price_map.get(pos["code"], pos.get("last_seen_price") or bp)
        unr = (cp - bp) * pos["qty"]
        pct = (cp - bp) / bp * 100 if bp else 0.0
        lines.append(
            f"  - {pos['name']}: {bp:,} → {cp:,}"
            f" ({pct:+.1f}%) 평가 {unr:+,}원"
        )

    lines.append(sep)

    # 손익 요약
    lines.append(f"💰 실현 손익: {realized:+,}원 ({real_pct:+.2f}%)")
    lines.append(f"📊 평가 손익: {unrealized:+,}원 ({unrl_pct:+.2f}%)")
    lines.append(f"🎯 총 손익:   {total_pnl:+,}원 ({total_pct:+.2f}%)")

    # 코스피 알파 (조회 성공 시만)
    if kospi_rate is not None:
        alpha = total_pct - kospi_rate
        lines.append(f"\n📈 시장: 코스피 {kospi_rate:+.2f}%")
        lines.append(f"🏆 알파: {alpha:+.2f}% (시장 대비)")

    lines.append(sep)

    # 매도 출처 요약
    auto_cnt   = len(profit_sells) + len(loss_sells)
    manual_cnt = len(manual_sells)
    lines.append(f"🤖 monitor.py 자동 매도: {auto_cnt}건")
    if manual_cnt:
        names = ", ".join(t.get("name", t["code"]) for t in manual_sells)
        lines.append(f"✋ 수동 매도: {manual_cnt}건 ({names})")
    if reco_cnt:
        lines.append(f"📝 추천 기록: {reco_cnt}건")

    return "\n".join(lines)


# ── 진입점 ────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    today = date.today().isoformat()

    pos_data   = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    seed_money = pos_data.get("seed_money", 0)
    open_pos   = [p for p in pos_data["positions"] if p["status"] in ("OPEN", "HOLDING")]

    # 현재가 조회 (실패 시 last_seen_price / buy_price 대체)
    token     = get_token()
    price_map: dict[str, int] = {}
    for pos in open_pos:
        price = _fetch_price(token, pos["code"])
        if price > 0:
            price_map[pos["code"]] = price
        else:
            fallback = pos.get("last_seen_price") or pos["buy_price"]
            price_map[pos["code"]] = fallback
            print(f"  [{pos['name']}] 현재가 대체: {fallback:,}원")

    # 코스피 등락률: KIS API 시도 → 실패 시 네이버 증권 fallback
    kospi_rate = _fetch_kospi_rate(token)
    if kospi_rate is None:
        try:
            kospi_rate = get_market_context()["kospi"]["change"]
            print(f"  [코스피] 네이버 증권 fallback: {kospi_rate:+.2f}%")
        except Exception:
            pass

    # daily_pnl.json 기록
    log_daily_pnl(seed_money, price_map)

    # 보고서 생성 및 전송
    report = _build_report(seed_money, today, price_map, kospi_rate)
    print(report)
    send_message(report)
    print("\n[daily_report] 텔레그램 전송 완료")


if __name__ == "__main__":
    main()
