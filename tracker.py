#!/usr/bin/env python3
"""거래 기록, 일별 P&L, 추천 적중률 추적."""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR          = Path(r"C:\Stock\data")
TRADES_PATH       = DATA_DIR / "trades.json"
DAILY_PNL_PATH    = DATA_DIR / "daily_pnl.json"
RECO_PATH         = DATA_DIR / "recommendations.json"
POSITIONS_PATH    = Path(r"C:\Stock\stock-bot\positions.json")


# ── 파일 I/O 헬퍼 ─────────────────────────────────────────────────

def _load(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _today() -> str:
    return date.today().isoformat()


# ── 매수 기록 ─────────────────────────────────────────────────────

def log_buy(code: str, name: str, buy_price: int, qty: int,
            source: str = "manual", buy_date: str | None = None) -> None:
    """매수 체결 기록."""
    d = buy_date or _today()
    data = _load(TRADES_PATH)
    trades = data.get("trades", [])

    trade_id = f"{d.replace('-', '')}-BUY-{code}"
    # 동일 날짜 중복 방지 (suffix 추가)
    existing = {t["id"] for t in trades}
    suffix = ""
    idx = 2
    while f"{trade_id}{suffix}" in existing:
        suffix = f"-{idx}"
        idx += 1
    trade_id = f"{trade_id}{suffix}"

    trades.append({
        "id":     trade_id,
        "date":   d,
        "type":   "BUY",
        "code":   code,
        "name":   name,
        "price":  buy_price,
        "qty":    qty,
        "amount": buy_price * qty,
        "source": source,
    })
    data["trades"] = trades
    _save(TRADES_PATH, data)
    print(f"  [tracker] BUY 기록: {name}({code}) {buy_price:,}×{qty}주")


# ── 매도 기록 ─────────────────────────────────────────────────────

def log_sell(code: str, sell_price: int, reason: str, name: str = "") -> None:
    """매도 체결 기록. 가장 최근 BUY에서 매수가/수량/보유일 계산.

    reason: "stop_loss" | "trailing" | "take_profit"
    name:   종목명 (BUY 기록 없을 때 fallback으로 사용)
    """
    d = _today()
    data = _load(TRADES_PATH)
    trades = data.get("trades", [])

    # 같은 종목의 가장 최근 BUY 탐색
    buy_record = None
    for t in reversed(trades):
        if t["code"] == code and t["type"] == "BUY":
            buy_record = t
            break

    buy_price = buy_record["price"] if buy_record else sell_price
    qty       = buy_record["qty"]   if buy_record else 0
    buy_date  = buy_record["date"]  if buy_record else d

    try:
        hold_days = (date.fromisoformat(d) - date.fromisoformat(buy_date)).days
    except ValueError:
        hold_days = 0

    profit_amt = (sell_price - buy_price) * qty
    profit_pct = round((sell_price - buy_price) / buy_price * 100, 2) if buy_price else 0.0

    trade_id = f"{d.replace('-', '')}-SELL-{code}"
    existing = {t["id"] for t in trades}
    suffix = ""
    idx = 2
    while f"{trade_id}{suffix}" in existing:
        suffix = f"-{idx}"
        idx += 1
    trade_id = f"{trade_id}{suffix}"

    trades.append({
        "id":         trade_id,
        "date":       d,
        "type":       "SELL",
        "code":       code,
        "name":       buy_record["name"] if buy_record else (name or code),
        "price":      sell_price,
        "qty":        qty,
        "amount":     sell_price * qty,
        "buy_price":  buy_price,
        "hold_days":  hold_days,
        "profit_amt": profit_amt,
        "profit_pct": profit_pct,
        "reason":     reason,
    })
    data["trades"] = trades
    _save(TRADES_PATH, data)
    print(f"  [tracker] SELL 기록: {buy_record['name'] if buy_record else (name or code)}({code}) "
          f"{sell_price:,}원 ({profit_pct:+.2f}%) [{reason}]")


# ── 일별 P&L 기록 ────────────────────────────────────────────────

def log_daily_pnl(seed_money: int, price_map: dict[str, int] | None = None) -> None:
    """당일 평가 P&L 기록. price_map = {code: cur_price}."""
    d = _today()
    pos_data = _load(POSITIONS_PATH)
    positions = pos_data.get("positions", [])

    total_invested = 0
    total_value    = 0
    open_positions = []

    for pos in positions:
        if pos.get("status") != "OPEN":
            continue
        code      = pos["code"]
        buy_price = pos["buy_price"]
        qty       = pos["qty"]
        invested  = buy_price * qty
        cur_price = (price_map or {}).get(code, buy_price)
        cur_value = cur_price * qty

        total_invested += invested
        total_value    += cur_value
        open_positions.append({
            "code":      code,
            "name":      pos.get("name", code),
            "qty":       qty,
            "buy_price": buy_price,
            "cur_price": cur_price,
            "unrealized": (cur_price - buy_price) * qty,
        })

    # 오늘 실현 손익
    trade_data = _load(TRADES_PATH)
    realized = sum(
        t.get("profit_amt", 0)
        for t in trade_data.get("trades", [])
        if t["date"] == d and t["type"] == "SELL"
    )

    unrealized   = total_value - total_invested
    total_pnl    = realized + unrealized
    total_assets = seed_money + total_pnl

    record = {
        "date":          d,
        "seed_money":    seed_money,
        "total_assets":  total_assets,
        "total_pnl":     total_pnl,
        "realized":      realized,
        "unrealized":    unrealized,
        "open_count":    len(open_positions),
        "positions":     open_positions,
    }

    pnl_data = _load(DAILY_PNL_PATH)
    records  = pnl_data.get("records", [])
    records  = [r for r in records if r.get("date") != d]  # 오늘 기록 덮어쓰기
    records.append(record)
    pnl_data["records"] = records
    _save(DAILY_PNL_PATH, pnl_data)
    print(f"  [tracker] 일별 P&L 기록: {d} 실현 {realized:+,} / 미실현 {unrealized:+,} / 총 {total_pnl:+,}원")


# ── 추천 기록 ────────────────────────────────────────────────────

def log_recommendation(code: str, name: str, recommended_price: int,
                       target_price: int | None = None) -> None:
    """consensus 추천 종목 기록 (히트율 추적용)."""
    d = _today()
    data = _load(RECO_PATH)
    records = data.get("records", [])

    # 같은 날 같은 종목 중복 방지
    for r in records:
        if r["date"] == d and r["code"] == code:
            return

    records.append({
        "date":              d,
        "code":              code,
        "name":              name,
        "recommended_price": recommended_price,
        "target_price":      target_price,
        "bought":            False,
        "bought_date":       None,
        "result":            None,   # "profit" | "loss" | "holding" | "missed"
    })
    data["records"] = records
    _save(RECO_PATH, data)
    print(f"  [tracker] 추천 기록: {name}({code}) {recommended_price:,}원")


# ── 주간 리포트 ──────────────────────────────────────────────────

def generate_weekly_report() -> str:
    """최근 7일 거래 요약 리포트 문자열 반환."""
    today     = date.today()
    week_ago  = today - timedelta(days=7)
    w_str     = week_ago.isoformat()

    trade_data = _load(TRADES_PATH)
    sells = [
        t for t in trade_data.get("trades", [])
        if t["type"] == "SELL" and t.get("date", "") >= w_str
    ]

    total_trades  = len(sells)
    win_trades    = [t for t in sells if t.get("profit_pct", 0) > 0]
    loss_trades   = [t for t in sells if t.get("profit_pct", 0) <= 0]
    total_profit  = sum(t.get("profit_amt", 0) for t in sells)
    win_rate      = len(win_trades) / total_trades * 100 if total_trades else 0.0

    best  = max(sells, key=lambda t: t.get("profit_pct", 0), default=None)
    worst = min(sells, key=lambda t: t.get("profit_pct", 0), default=None)

    pnl_data = _load(DAILY_PNL_PATH)
    recent_pnl = [
        r for r in pnl_data.get("records", [])
        if r.get("date", "") >= w_str
    ]
    open_count = recent_pnl[-1]["open_count"] if recent_pnl else 0

    lines = [
        f"📊 주간 트레이딩 리포트 ({w_str} ~ {today.isoformat()})",
        f"",
        f"■ 매도 체결: {total_trades}건 (익절 {len(win_trades)} / 손절 {len(loss_trades)})",
        f"■ 승률: {win_rate:.1f}%",
        f"■ 실현 손익: {total_profit:+,}원",
        f"■ 보유 종목: {open_count}개",
    ]

    if best:
        lines.append(f"■ 최고 수익: {best.get('name', best['code'])} {best.get('profit_pct', 0):+.1f}%")
    if worst:
        lines.append(f"■ 최대 손실: {worst.get('name', worst['code'])} {worst.get('profit_pct', 0):+.1f}%")

    reco_data = _load(RECO_PATH)
    recos = [r for r in reco_data.get("records", []) if r.get("date", "") >= w_str]
    if recos:
        bought = [r for r in recos if r.get("bought")]
        lines.append(f"■ 추천 종목: {len(recos)}개 → 매수 {len(bought)}개")

    detail_lines = []
    for t in sells:
        emoji = "✅" if t.get("profit_pct", 0) > 0 else "❌"
        reason_map = {"stop_loss": "손절", "trailing": "트레일링", "take_profit": "익절"}
        reason = reason_map.get(t.get("reason", ""), t.get("reason", ""))
        detail_lines.append(
            f"  {emoji} {t.get('name', t['code'])} {t.get('profit_pct', 0):+.1f}% "
            f"({t.get('profit_amt', 0):+,}원, {t.get('hold_days', 0)}일, {reason})"
        )

    if detail_lines:
        lines.append("")
        lines.append("─ 거래 내역 ─")
        lines.extend(detail_lines)

    return "\n".join(lines)


# ── 일간 텔레그램 리포트 (선택) ──────────────────────────────────

def send_daily_report(seed_money: int, price_map: dict[str, int] | None = None) -> None:
    """일별 P&L 기록 후 주간 리포트를 텔레그램으로 전송."""
    from telegram_alert import send_message

    log_daily_pnl(seed_money, price_map)
    report = generate_weekly_report()
    send_message(report)
    print("[tracker] 일간 리포트 전송 완료")


# ── 추천 이력 분석 ───────────────────────────────────────────────

def _calc_streak(dates: list[str]) -> int:
    """날짜 목록에서 마지막 날짜까지 연속 추천 일수.
    주말(최대 3일 간격) 허용: 금→월 = 3일 gap."""
    sorted_dates = sorted(set(dates))
    if not sorted_dates:
        return 0
    streak = 1
    for i in range(len(sorted_dates) - 1, 0, -1):
        gap = (date.fromisoformat(sorted_dates[i]) - date.fromisoformat(sorted_dates[i - 1])).days
        if gap <= 3:
            streak += 1
        else:
            break
    return streak


def analyze_recommendations() -> dict[str, dict]:
    """recommendations.json 분석 → 종목별 추천 이력 통계 반환.

    반환 예시:
    {
      "071050": {
        "name": "한국금융지주",
        "first": "2026-05-13",
        "last":  "2026-05-15",
        "count": 3,
        "dates": ["2026-05-13", "2026-05-14", "2026-05-15"],
        "scores": [70, 70, 75],
        "score_trend": "rising",   # "rising" | "falling" | "stable"
        "prices": [274500, 270000, 266000],
        "streak": 3,
      }
    }
    """
    data    = _load(RECO_PATH)
    records = sorted(data.get("records", []), key=lambda r: r.get("date", ""))

    result: dict[str, dict] = {}
    for rec in records:
        code  = rec.get("code", "")
        name  = rec.get("name", code)
        d     = rec.get("date", "")
        price = rec.get("recommended_price", 0)
        score = rec.get("score")          # 선택 필드 (없을 수 있음)

        if not code:
            continue

        if code not in result:
            result[code] = {
                "name":        name,
                "first":       d,
                "last":        d,
                "count":       0,
                "dates":       [],
                "scores":      [],
                "prices":      [],
                "score_trend": "stable",
                "streak":      1,
            }

        entry = result[code]
        entry["count"] += 1
        entry["last"]   = d
        entry["dates"].append(d)
        entry["prices"].append(price)
        if score is not None:
            entry["scores"].append(int(score))

    for entry in result.values():
        scores = entry["scores"]
        if len(scores) >= 2:
            if scores[-1] > scores[0]:
                entry["score_trend"] = "rising"
            elif scores[-1] < scores[0]:
                entry["score_trend"] = "falling"

        entry["streak"] = _calc_streak(entry["dates"])

    return result


# ── CLI 테스트 ────────────────────────────────────────────────────

if __name__ == "__main__":
    print(generate_weekly_report())
