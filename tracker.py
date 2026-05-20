#!/usr/bin/env python3
"""거래 기록, 일별 P&L, 추천 적중률 추적."""

import json
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR       = Path(r"C:\Stock\data")
TRADES_PATH    = DATA_DIR / "trades.json"
DAILY_PNL_PATH = DATA_DIR / "daily_pnl.json"
RECO_PATH      = DATA_DIR / "recommendations.json"
POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")

# VALIDATE_TRADES=1 환경변수로 log_buy/log_sell 후 자동 일관성 검증 활성화
_AUTO_VALIDATE = os.getenv("VALIDATE_TRADES", "0") in ("1", "true", "yes")


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


def _unique_id(base: str, existing: set[str]) -> str:
    """base가 already 존재하면 -2, -3... suffix를 붙여 유니크 ID 반환."""
    if base not in existing:
        return base
    idx = 2
    while True:
        candidate = f"{base}-{idx}"
        if candidate not in existing:
            return candidate
        idx += 1


# ── 파일 잠금 ─────────────────────────────────────────────────────

@contextmanager
def _file_lock(path: Path, timeout: float = 5.0):
    """trades.json 동시 쓰기 방지. O_CREAT|O_EXCL 원자적 생성으로 잠금."""
    lock_path = path.with_suffix(".lock")
    deadline  = time.monotonic() + timeout
    acquired  = False
    try:
        while time.monotonic() < deadline:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.05)
        if not acquired:
            raise TimeoutError(
                f"trades.json 잠금 타임아웃 ({timeout:.0f}초) — 다른 프로세스 또는 stale lock 확인"
            )
        yield
    finally:
        if acquired:
            lock_path.unlink(missing_ok=True)


# ── 매수 기록 ─────────────────────────────────────────────────────

def log_buy(code: str, name: str, buy_price: int, qty: int,
            source: str = "manual", buy_date: str | None = None,
            entry_signal: str | None = None) -> None:
    """매수 체결 기록.

    BUY 레코드에 status/remaining_qty/cumulative_qty/avg_buy_price 추가.
    같은 종목 누적 수량·평단가를 스냅샷으로 기록.
    """
    d = buy_date or _today()

    with _file_lock(TRADES_PATH):
        data   = _load(TRADES_PATH)
        trades = data.get("trades", [])

        trade_id = _unique_id(
            f"{d.replace('-', '')}-BUY-{code}",
            {t["id"] for t in trades},
        )

        # 현재 OPEN 보유분 기준 누적 정보
        open_buys = [
            t for t in trades
            if t["code"] == code and t["type"] == "BUY"
            and t.get("status", "OPEN") in ("OPEN", "PARTIAL_CLOSED")
        ]
        prev_qty  = sum(t.get("remaining_qty", t["qty"]) for t in open_buys)
        prev_cost = sum(t["price"] * t.get("remaining_qty", t["qty"]) for t in open_buys)
        cum_qty   = prev_qty + qty
        avg_price = round((prev_cost + buy_price * qty) / cum_qty)

        trades.append({
            "id":             trade_id,
            "date":           d,
            "time":           datetime.now().isoformat(timespec="seconds"),
            "type":           "BUY",
            "code":           code,
            "name":           name,
            "price":          buy_price,
            "qty":            qty,
            "amount":         buy_price * qty,
            "source":         source,
            "entry_method":   source,
            "entry_signal":   entry_signal,
            "status":         "OPEN",
            "remaining_qty":  qty,
            "cumulative_qty": cum_qty,
            "avg_buy_price":  avg_price,
        })
        data["trades"] = trades
        _save(TRADES_PATH, data)

    print(f"  [BUY] {name}({code}) {buy_price:,}×{qty}주 | 누적: {cum_qty}주 @ 평단 {avg_price:,}원")

    if _AUTO_VALIDATE:
        validate_trades_consistency(notify=False)


def get_open_buy_qty(code: str) -> int:
    """trades.json에서 해당 종목의 OPEN/PARTIAL_CLOSED BUY 잔여 수량 합산.

    monitor가 잔고 증가를 감지할 때, 이미 기록된 수량과 비교해 중복 log_buy 방지용.
    """
    data = _load(TRADES_PATH)
    return sum(
        t.get("remaining_qty", t["qty"])
        for t in data.get("trades", [])
        if t["code"] == code
        and t["type"] == "BUY"
        and t.get("status", "OPEN") in ("OPEN", "PARTIAL_CLOSED")
    )


def has_recent_buy(code: str, minutes: int = 10) -> bool:
    """trades.json에서 최근 N분 내 동일 종목 BUY 기록이 있으면 True."""
    data   = _load(TRADES_PATH)
    cutoff = datetime.now() - timedelta(minutes=minutes)
    for t in data.get("trades", []):
        if t["code"] != code or t["type"] != "BUY":
            continue
        try:
            t_time = datetime.fromisoformat(t.get("time", t.get("date", "")))
            if t_time >= cutoff:
                return True
        except ValueError:
            if t.get("date") == date.today().isoformat():
                return True
    return False


# ── 매도 기록 ─────────────────────────────────────────────────────

def log_sell(code: str, sell_price: int, reason: str, name: str = "",
             sell_qty: int | None = None) -> None:
    """매도 체결 기록.

    같은 종목의 모든 OPEN BUY를 FIFO 합산 → 가중평균 매수가 계산 → profit 기록.
    BUY 레코드 remaining_qty/status를 CLOSED/PARTIAL_CLOSED로 갱신.

    매칭 BUY 없음 → ERROR 레코드 기록 + 텔레그램 알림.

    reason:   "stop_loss" | "trailing" | "take_profit" | "manual"
    sell_qty: 지정 시 해당 수량. None이면 전체 OPEN 수량.
    """
    d = _today()

    with _file_lock(TRADES_PATH):
        data   = _load(TRADES_PATH)
        trades = data.get("trades", [])

        # OPEN/PARTIAL_CLOSED BUY 레코드들 (FIFO — 오래된 매수부터)
        open_buys = [
            t for t in trades
            if t["code"] == code and t["type"] == "BUY"
            and t.get("status", "OPEN") in ("OPEN", "PARTIAL_CLOSED")
        ]
        total_available = sum(t.get("remaining_qty", t["qty"]) for t in open_buys)

        # ── 매칭 실패 ──────────────────────────────────────────────
        if total_available == 0:
            err_id = _unique_id(
                f"{d.replace('-', '')}-SELL-{code}-ERROR",
                {t["id"] for t in trades},
            )
            trades.append({
                "id":         err_id,
                "date":       d,
                "type":       "SELL",
                "code":       code,
                "name":       name or code,
                "price":      sell_price,
                "qty":        sell_qty or 0,
                "amount":     sell_price * (sell_qty or 0),
                "buy_price":  sell_price,
                "hold_days":  0,
                "profit_amt": 0,
                "profit_pct": 0.0,
                "reason":     reason,
                "status":     "ERROR",
                "error":      "매칭 BUY 없음",
            })
            data["trades"] = trades
            _save(TRADES_PATH, data)

            err_msg = (
                f"[ERROR] log_sell 매칭 실패: {name or code}({code}), "
                f"보유 수량 없음 → ERROR 레코드 기록 + 텔레그램 알림"
            )
            print(err_msg)
            try:
                from telegram_alert import send_message
                send_message(f"⚠️ {err_msg}")
            except Exception:
                pass
            return

        # ── sell_qty 결정 ──────────────────────────────────────────
        if sell_qty is None:
            sell_qty = total_available
        elif sell_qty > total_available:
            print(
                f"[경고] {name or code}({code}) 매도 수량({sell_qty}) > "
                f"보유({total_available}) → 보유 수량으로 조정"
            )
            sell_qty = total_available

        # ── FIFO 매칭 & 가중평균 매수가 ───────────────────────────
        qty_left      = sell_qty
        total_cost    = 0
        oldest_date   = None
        matched_name  = name or code

        for buy in open_buys:
            if qty_left <= 0:
                break
            avail = buy.get("remaining_qty", buy["qty"])
            take  = min(avail, qty_left)
            total_cost += buy["price"] * take
            qty_left   -= take

            if oldest_date is None:
                oldest_date  = buy.get("date", d)
                matched_name = buy.get("name", matched_name)

            new_remaining        = avail - take
            buy["remaining_qty"] = new_remaining
            buy["status"]        = "CLOSED" if new_remaining == 0 else "PARTIAL_CLOSED"

        avg_buy_price = round(total_cost / sell_qty)

        try:
            hold_days = (date.fromisoformat(d) - date.fromisoformat(oldest_date or d)).days
        except ValueError:
            hold_days = 0

        profit_amt = (sell_price - avg_buy_price) * sell_qty
        profit_pct = round((sell_price - avg_buy_price) / avg_buy_price * 100, 2) if avg_buy_price else 0.0

        trade_id = _unique_id(
            f"{d.replace('-', '')}-SELL-{code}",
            {t["id"] for t in trades},
        )

        trades.append({
            "id":         trade_id,
            "date":       d,
            "type":       "SELL",
            "code":       code,
            "name":       matched_name,
            "price":      sell_price,
            "qty":        sell_qty,
            "amount":     sell_price * sell_qty,
            "buy_price":  avg_buy_price,
            "hold_days":  hold_days,
            "profit_amt": profit_amt,
            "profit_pct": profit_pct,
            "reason":     reason,
        })
        data["trades"] = trades
        _save(TRADES_PATH, data)

    print(
        f"  [SELL] {matched_name}({code}) {sell_price:,}×{sell_qty}주 @ {reason} | "
        f"평단 {avg_buy_price:,}원, 손익 {profit_amt:+,}원 ({profit_pct:+.2f}%)"
    )

    if _AUTO_VALIDATE:
        validate_trades_consistency(notify=True)


# ── 일관성 검증 ───────────────────────────────────────────────────

def validate_trades_consistency(notify: bool = True) -> bool:
    """positions.json CLOSED_* 종목 qty ↔ trades.json SELL 합산 수량 검증.

    불일치 시 False 반환 + 에러 출력 + 텔레그램 알림 (notify=True).
    """
    pos_data   = _load(POSITIONS_PATH)
    trade_data = _load(TRADES_PATH)

    # SELL 합산 (ERROR 레코드 제외)
    sell_totals: dict[str, int] = {}
    for t in trade_data.get("trades", []):
        if t.get("type") == "SELL" and t.get("status") != "ERROR":
            sell_totals[t["code"]] = sell_totals.get(t["code"], 0) + t.get("qty", 0)

    errors: list[str] = []
    for pos in pos_data.get("positions", []):
        if not pos.get("status", "").startswith("CLOSED"):
            continue
        code     = pos["code"]
        expected = pos["qty"]
        actual   = sell_totals.get(code, 0)
        if actual != expected:
            errors.append(
                f"{pos.get('name', code)}({code}): "
                f"SELL합산={actual} ≠ position.qty={expected} (차이: {actual - expected:+d}주)"
            )

    if errors:
        for e in errors:
            print(f"[검증 FAIL] {e}")
        if notify:
            try:
                from telegram_alert import send_message
                send_message("⚠️ trades 일관성 오류:\n" + "\n".join(errors))
            except Exception:
                pass
        return False

    return True


# ── 마이그레이션 (1회 실행용) ─────────────────────────────────────

def migrate_trades_json(dry_run: bool = True) -> None:
    """기존 BUY 레코드에 status/remaining_qty/cumulative_qty/avg_buy_price 추가.

    BUY-SELL을 FIFO 매칭하여 remaining_qty/status 계산.
    dry_run=True(기본)이면 결과 출력만, False면 실제 저장.
    """
    import copy

    data   = _load(TRADES_PATH)
    trades = copy.deepcopy(data.get("trades", []))

    needs = any(t.get("type") == "BUY" and "status" not in t for t in trades)
    if not needs:
        print("[migrate] 이미 마이그레이션 완료됨 (모든 BUY에 status 필드 존재).")
        return

    # ── 1단계: 모든 BUY 초기화 ────────────────────────────────────
    for t in trades:
        if t.get("type") == "BUY":
            t["status"]        = "OPEN"
            t["remaining_qty"] = t.get("qty", 0)
            t.pop("cumulative_qty", None)
            t.pop("avg_buy_price", None)

    # ── 2단계: 시간순 처리 → cumulative 계산 + SELL 매칭 ─────────
    # open_refs: code → OPEN/PARTIAL BUY 레코드 참조 목록 (FIFO)
    open_refs: dict[str, list] = {}

    for t in trades:
        if t.get("type") == "BUY":
            code     = t["code"]
            qty      = t["qty"]
            prev     = open_refs.get(code, [])
            prev_qty  = sum(b["remaining_qty"] for b in prev)
            prev_cost = sum(b["price"] * b["remaining_qty"] for b in prev)
            cum_qty   = prev_qty + qty
            avg_price = round((prev_cost + t["price"] * qty) / cum_qty) if cum_qty else t["price"]

            t["cumulative_qty"] = cum_qty
            t["avg_buy_price"]  = avg_price

            if code not in open_refs:
                open_refs[code] = []
            open_refs[code].append(t)

        elif t.get("type") == "SELL":
            code     = t["code"]
            qty_left = t.get("qty", 0)

            for buy in list(open_refs.get(code, [])):
                if qty_left <= 0:
                    break
                avail = buy["remaining_qty"]
                take  = min(avail, qty_left)
                buy["remaining_qty"] = avail - take
                buy["status"] = "CLOSED" if buy["remaining_qty"] == 0 else "PARTIAL_CLOSED"
                qty_left -= take

            # 완전히 닫힌 레코드 제거
            if code in open_refs:
                open_refs[code] = [b for b in open_refs[code] if b["remaining_qty"] > 0]

    # ── 결과 출력 ─────────────────────────────────────────────────
    updated = [t for t in trades if t.get("type") == "BUY"]
    print(f"\n[migrate] BUY 레코드 {len(updated)}개 처리:")
    for t in updated:
        print(
            f"  {t['id']}: status={t['status']}, "
            f"remaining={t['remaining_qty']}/{t['qty']}, "
            f"cum_qty={t['cumulative_qty']}, avg={t['avg_buy_price']:,}원"
        )

    if dry_run:
        print("\n[migrate] dry-run - 저장 안 함. migrate_trades_json(dry_run=False)으로 적용.")
        return

    data["trades"] = trades
    _save(TRADES_PATH, data)
    print("\n[migrate] trades.json 마이그레이션 완료.")


# ── 일별 P&L 기록 ────────────────────────────────────────────────

def log_daily_pnl(seed_money: int, price_map: dict[str, int] | None = None) -> None:
    """당일 평가 P&L 기록. price_map = {code: cur_price}."""
    d        = _today()
    pos_data = _load(POSITIONS_PATH)
    positions = pos_data.get("positions", [])

    total_invested = 0
    total_value    = 0
    open_positions = []

    for pos in positions:
        if pos.get("status") not in ("OPEN", "HOLDING"):
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
            "code":       code,
            "name":       pos.get("name", code),
            "qty":        qty,
            "buy_price":  buy_price,
            "cur_price":  cur_price,
            "unrealized": (cur_price - buy_price) * qty,
        })

    trade_data = _load(TRADES_PATH)
    realized = sum(
        t.get("profit_amt", 0)
        for t in trade_data.get("trades", [])
        if t["date"] == d and t["type"] == "SELL" and t.get("status") != "ERROR"
    )

    unrealized   = total_value - total_invested
    total_pnl    = realized + unrealized
    total_assets = seed_money + total_pnl

    record = {
        "date":         d,
        "seed_money":   seed_money,
        "total_assets": total_assets,
        "total_pnl":    total_pnl,
        "realized":     realized,
        "unrealized":   unrealized,
        "open_count":   len(open_positions),
        "positions":    open_positions,
    }

    pnl_data = _load(DAILY_PNL_PATH)
    records  = pnl_data.get("records", [])
    records  = [r for r in records if r.get("date") != d]
    records.append(record)
    pnl_data["records"] = records
    _save(DAILY_PNL_PATH, pnl_data)
    print(f"  [tracker] 일별 P&L 기록: {d} 실현 {realized:+,} / 미실현 {unrealized:+,} / 총 {total_pnl:+,}원")


# ── 추천 기록 ────────────────────────────────────────────────────

def log_recommendation(code: str, name: str, recommended_price: int,
                       target_price: int | None = None) -> None:
    """consensus 추천 종목 기록 (히트율 추적용)."""
    d    = _today()
    data = _load(RECO_PATH)
    records = data.get("records", [])

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
        "result":            None,
    })
    data["records"] = records
    _save(RECO_PATH, data)
    print(f"  [tracker] 추천 기록: {name}({code}) {recommended_price:,}원")


# ── 주간 리포트 ──────────────────────────────────────────────────

def generate_weekly_report() -> str:
    """최근 7일 거래 요약 리포트 문자열 반환."""
    today    = date.today()
    week_ago = today - timedelta(days=7)
    w_str    = week_ago.isoformat()

    trade_data = _load(TRADES_PATH)
    sells = [
        t for t in trade_data.get("trades", [])
        if t["type"] == "SELL"
        and t.get("date", "") >= w_str
        and t.get("status") != "ERROR"
    ]

    total_trades = len(sells)
    win_trades   = [t for t in sells if t.get("profit_pct", 0) > 0]
    loss_trades  = [t for t in sells if t.get("profit_pct", 0) <= 0]
    total_profit = sum(t.get("profit_amt", 0) for t in sells)
    win_rate     = len(win_trades) / total_trades * 100 if total_trades else 0.0

    best  = max(sells, key=lambda t: t.get("profit_pct", 0), default=None)
    worst = min(sells, key=lambda t: t.get("profit_pct", 0), default=None)

    pnl_data   = _load(DAILY_PNL_PATH)
    recent_pnl = [r for r in pnl_data.get("records", []) if r.get("date", "") >= w_str]
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
        emoji      = "✅" if t.get("profit_pct", 0) > 0 else "❌"
        reason_map = {"stop_loss": "손절", "trailing": "트레일링", "take_profit": "익절"}
        reason     = reason_map.get(t.get("reason", ""), t.get("reason", ""))
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
    """recommendations.json 분석 → 종목별 추천 이력 통계 반환."""
    data    = _load(RECO_PATH)
    records = sorted(data.get("records", []), key=lambda r: r.get("date", ""))

    result: dict[str, dict] = {}
    for rec in records:
        code  = rec.get("code", "")
        name  = rec.get("name", code)
        d     = rec.get("date", "")
        price = rec.get("recommended_price", 0)
        score = rec.get("score")

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


# ── CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="tracker CLI")
    sub    = parser.add_subparsers(dest="cmd")

    sub.add_parser("report", help="주간 리포트 출력")

    p_migrate = sub.add_parser("migrate", help="trades.json BUY 레코드 마이그레이션")
    p_migrate.add_argument("--apply", action="store_true", help="실제 저장 (기본: dry-run)")

    p_validate = sub.add_parser("validate", help="trades 일관성 검증")

    args = parser.parse_args()

    if args.cmd == "report":
        print(generate_weekly_report())
    elif args.cmd == "migrate":
        migrate_trades_json(dry_run=not args.apply)
    elif args.cmd == "validate":
        ok = validate_trades_consistency(notify=False)
        print("[검증 OK] 일관성 이상 없음." if ok else "[검증 FAIL] 위 항목 확인.")
    else:
        print(generate_weekly_report())
