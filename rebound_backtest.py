#!/usr/bin/env python3
"""V자 반등 신호 백테스트.

기간: 2025-11-19 ~ 2026-05-18 (6개월)

실행:
    python rebound_backtest.py                       # 기본: KOSPI200+KOSDAQ150 (350종목)
    python rebound_backtest.py --quick               # 상위 50종목 빠른 검증
    python rebound_backtest.py --full                # 전체 KOSPI+KOSDAQ (~2600종목, 1~2시간)
    python rebound_backtest.py --codes 005930 000660 # 특정 종목만
"""

import sys
import json
import argparse
from datetime import date
from pathlib import Path

try:
    import FinanceDataReader as fdr
    import pandas as pd
except ImportError:
    sys.exit("FinanceDataReader 미설치\npip install FinanceDataReader 후 재실행")

from rebound_detector import _calc_rsi, _is_hammer, _volume_ratio, _five_day_decline
from stock_info import is_spac

# ── 설정 ──────────────────────────────────────────────────────────

BACKTEST_START  = "2025-11-19"
BACKTEST_END    = "2026-05-18"
FETCH_FROM      = "2025-10-01"   # RSI(14일)·거래량(20일) 계산용 버퍼

TAKE_PROFIT_PCT = 0.10   # 목표가 +10%
STOP_LOSS_PCT   = 0.05   # 손절 -5%
TRAILING_DROP   = 0.03   # 트레일링 스탑 -3%
MAX_HOLD_DAYS   = 20     # 최대 보유 20 영업일

KOSPI_N         = 200
KOSDAQ_N        = 150
MIN_CAP_B       = 100     # --full 모드 시총 하한 (억원)
MIN_AVG_VOLUME  = 10_000  # 일평균 거래량 하한 (주): 이 미만 종목 스킵

DATA_DIR = Path(r"C:\Stock\stock-bot\data")
TODAY    = date.today().isoformat()

# ── 섹터 그룹 매핑 ────────────────────────────────────────────────

_SECTOR_KEYWORDS: dict[str, list[str]] = {
    "IT":   ["소프트웨어", "반도체", "전자", "통신", "정보", "디스플레이", "it", "tech"],
    "바이오": ["의약품", "제약", "바이오", "의료", "헬스", "건강"],
    "제조": ["자동차", "화학", "소재", "철강", "기계", "섬유", "조선", "건설", "에너지", "식품"],
    "금융": ["금융", "은행", "증권", "보험", "자산"],
}

def _sector_group(sector: str) -> str:
    s = (sector or "").lower()
    for group, keywords in _SECTOR_KEYWORDS.items():
        if any(kw in s for kw in keywords):
            return group
    return "기타"


# ── 종목 목록 ─────────────────────────────────────────────────────

def _listing_to_dicts(df: pd.DataFrame, market: str) -> list[dict]:
    """StockListing DataFrame → 종목 dict 리스트."""
    cap_col    = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
    code_col   = next((c for c in df.columns if c.lower() == "code"),   df.columns[0])
    name_col   = next((c for c in df.columns if c.lower() == "name"),   df.columns[1])
    sector_col = next((c for c in df.columns if "sector" in c.lower() or "업종" in c.lower()), None)
    results = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip().zfill(6)
        if not code or code == "000000":
            continue
        cap_raw = row[cap_col] if cap_col else None
        cap_b   = round(float(cap_raw) / 1e8, 1) if cap_raw is not None and pd.notna(cap_raw) else None
        sector  = str(row[sector_col]).strip() if sector_col and pd.notna(row.get(sector_col)) else ""
        results.append({
            "code":         code,
            "name":         str(row.get(name_col, code)).strip(),
            "market":       market,
            "market_cap_b": cap_b,
            "sector":       sector,
            "sector_group": _sector_group(sector),
        })
    return results


def _get_target_codes(kospi_n: int = KOSPI_N, kosdaq_n: int = KOSDAQ_N) -> list[dict]:
    """시총 상위 KOSPI + KOSDAQ 종목 반환."""
    results: list[dict] = []
    for market, n in (("KOSPI", kospi_n), ("KOSDAQ", kosdaq_n)):
        try:
            df = fdr.StockListing(market)
            cap_col = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
            df = df.nlargest(n, cap_col) if cap_col else df.head(n)
            results.extend(_listing_to_dicts(df, market))
        except Exception as exc:
            print(f"  [{market}] 종목 목록 조회 실패: {exc}")
    return results


def _get_all_stocks(min_cap_b: float = MIN_CAP_B) -> list[dict]:
    """전체 KOSPI + KOSDAQ 종목 (시총 min_cap_b억 미만 제외)."""
    results: list[dict] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df  = fdr.StockListing(market)
            cap_col = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
            all_rows = _listing_to_dicts(df, market)
            for s in all_rows:
                if s["market_cap_b"] is not None and s["market_cap_b"] < min_cap_b:
                    continue
                results.append(s)
            print(f"  [{market}] {len([s for s in results if s['market']== market])}종목")
        except Exception as exc:
            print(f"  [{market}] 목록 조회 실패: {exc}")
    return results


# ── 데이터 변환 ───────────────────────────────────────────────────

def _make_candle(ts: pd.Timestamp, row: pd.Series) -> dict:
    def _int(v) -> int:
        try:
            return int(float(v) if v is not None else 0)
        except (ValueError, TypeError):
            return 0
    return {
        "date":   ts.strftime("%Y%m%d"),
        "open":   _int(row.get("Open")),
        "high":   _int(row.get("High")),
        "low":    _int(row.get("Low")),
        "close":  _int(row.get("Close")),
        "volume": _int(row.get("Volume")),
    }


def _to_detect_candles(df_slice: pd.DataFrame) -> list[dict]:
    """DataFrame(과거→최신) → 최신→과거 캔들 (detect_rebound 입력 형식)."""
    candles = [_make_candle(ts, row) for ts, row in df_slice.iterrows()]
    candles.reverse()
    return candles


def _to_sim_candles(df_slice: pd.DataFrame) -> list[dict]:
    """DataFrame(과거→최신) → 과거→최신 캔들 (시뮬레이션 입력 형식)."""
    return [_make_candle(ts, row) for ts, row in df_slice.iterrows()]


# ── 신호 감지 (로컬, API 호출 없음) ──────────────────────────────

def _detect_local(candles: list[dict]) -> dict:
    """과거 데이터로 반등 신호 감지. rebound_detector 로직 그대로."""
    if len(candles) < 6:
        return {"is_rebound": False, "confidence": "none", "signals": []}

    signals: list[str] = []

    rsi = _calc_rsi(candles, 14)
    if rsi is not None and rsi <= 30:
        signals.append(f"RSI {rsi:.0f}")

    if _is_hammer(candles[0]):
        signals.append("망치형 캔들")

    vol_ratio = _volume_ratio(candles, 20)
    if vol_ratio >= 1.5:
        signals.append(f"거래량 {vol_ratio:.1f}배")

    decline = _five_day_decline(candles)
    if decline <= -5.0:
        signals.append(f"5일 낙폭 {decline:.1f}%")

    count = len(signals)
    if count >= 4:
        return {"is_rebound": True,  "confidence": "high",   "signals": signals}
    if count == 3:
        return {"is_rebound": True,  "confidence": "medium", "signals": signals}
    return     {"is_rebound": False, "confidence": "low" if count == 2 else "none", "signals": signals}


# ── 거래 시뮬레이션 ───────────────────────────────────────────────

def _simulate(entry_price: int, sim_candles: list[dict]) -> dict:
    """진입 후 최대 MAX_HOLD_DAYS 영업일 동안 매도 조건 시뮬레이션.

    sim_candles: 진입 다음날부터 과거→최신 순 (entry_price는 이미 반영).
    우선순위: 손절 > 트레일링 스탑 > 타임아웃.
    """
    trailing_active = False
    peak            = float(entry_price)

    for i, c in enumerate(sim_candles[:MAX_HOLD_DAYS]):
        h, l = c["high"], c["low"]

        if not trailing_active:
            # 손절 우선 체크 (low가 손절선 이하)
            if l <= entry_price * (1 - STOP_LOSS_PCT):
                return {
                    "outcome":    "stop_loss",
                    "profit_pct": round(-STOP_LOSS_PCT * 100, 2),
                    "hold_days":  i + 1,
                }
            # 고가가 목표가 이상 → 트레일링 활성화
            if h >= entry_price * (1 + TAKE_PROFIT_PCT):
                trailing_active = True
                peak = float(h)
        else:
            peak = max(peak, h)
            if l <= peak * (1 - TRAILING_DROP):
                exit_price = peak * (1 - TRAILING_DROP)
                return {
                    "outcome":    "trailing",
                    "profit_pct": round((exit_price - entry_price) / entry_price * 100, 2),
                    "hold_days":  i + 1,
                }

    # 타임아웃: 마지막 종가 기준 청산
    last = sim_candles[min(MAX_HOLD_DAYS - 1, len(sim_candles) - 1)]
    return {
        "outcome":    "timeout",
        "profit_pct": round((last["close"] - entry_price) / entry_price * 100, 2),
        "hold_days":  min(MAX_HOLD_DAYS, len(sim_candles)),
    }


# ── 통계 ─────────────────────────────────────────────────────────

def _calc_stats(trades: list[dict]) -> dict:
    total = len(trades)
    if total == 0:
        return {"total": 0}
    wins   = [t for t in trades if t["profit_pct"] > 0]
    losses = [t for t in trades if t["profit_pct"] <= 0]
    pcts   = [t["profit_pct"] for t in trades]
    avg_win  = sum(t["profit_pct"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t["profit_pct"] for t in losses) / len(losses) if losses else 0.0
    return {
        "total":         total,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / total * 100, 1),
        "avg_pct":       round(sum(pcts) / total, 2),
        "avg_profit":    round(avg_win,  2),
        "avg_loss":      round(avg_loss, 2),
        "profit_factor": round(abs(avg_win / avg_loss), 2) if avg_loss else 999.0,
        "mdd":           round(min(pcts), 2),
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / total, 1),
        "by_outcome": {
            "stop_loss": sum(1 for t in trades if t["outcome"] == "stop_loss"),
            "trailing":  sum(1 for t in trades if t["outcome"] == "trailing"),
            "timeout":   sum(1 for t in trades if t["outcome"] == "timeout"),
        },
    }


def _print_stats(label: str, trades: list[dict]) -> None:
    s = _calc_stats(trades)
    if s.get("total", 0) == 0:
        print(f"\n[{label}] 신호 없음")
        return
    sep = "─" * 38
    print(f"\n[{label}]")
    print(sep)
    print(f"  신호 발생    : {s['total']}건")
    print(f"  승률         : {s['win_rate']}%  ({s['wins']}승 {s['losses']}패)")
    print(f"  평균 손익    : {s['avg_pct']:+.2f}%")
    print(f"  평균 수익(익): {s['avg_profit']:+.2f}%")
    print(f"  평균 손실(손): {s['avg_loss']:+.2f}%")
    print(f"  손익비       : {s['profit_factor']:.2f}")
    print(f"  MDD          : {s['mdd']:+.2f}%")
    print(f"  평균 보유일  : {s['avg_hold_days']}일")
    o = s["by_outcome"]
    print(f"  청산 원인    : 손절 {o['stop_loss']} / 트레일링 {o['trailing']} / 타임아웃 {o['timeout']}")
    print(sep)


# ── 분류 헬퍼 ─────────────────────────────────────────────────────

def _calc_vol60(df: pd.DataFrame) -> float | None:
    """60일 연환산 변동성 (%)."""
    closes = df["Close"].tolist()
    if len(closes) < 30:
        return None
    window  = closes[-61:]
    returns = [(window[i] / window[i - 1] - 1) for i in range(1, len(window))]
    if len(returns) < 10:
        return None
    mean = sum(returns) / len(returns)
    var  = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round((var ** 0.5) * (252 ** 0.5) * 100, 1)


def _cap_bucket(cap_b: float | None) -> str:
    if cap_b is None:
        return "미분류"
    if cap_b < 1_000:
        return "100~1000억"
    if cap_b < 5_000:
        return "1000~5000억"
    if cap_b < 30_000:
        return "5000억~3조"
    return "3조+"


def _vol_bucket(vol_60d: float | None) -> str:
    if vol_60d is None:
        return "미분류"
    if vol_60d < 30:
        return "저변동(<30%)"
    if vol_60d < 60:
        return "중변동(30~60%)"
    return "고변동(60%+)"


def _print_breakdown_table(label: str, buckets: dict[str, list[dict]]) -> None:
    """분류별 통계 테이블 출력."""
    non_empty = {k: v for k, v in buckets.items() if v}
    if not non_empty:
        return
    print(f"\n[분류별 분석: {label}]")
    print(f"  {'분류':<16s}  {'건수':>4s}  {'승률':>6s}  {'평균손익':>8s}  {'손익비':>6s}  {'MDD':>7s}")
    print(f"  {'─'*16}  {'─'*4}  {'─'*6}  {'─'*8}  {'─'*6}  {'─'*7}")
    for bucket_name in sorted(non_empty):
        s = _calc_stats(non_empty[bucket_name])
        if s.get("total", 0) == 0:
            continue
        print(
            f"  {bucket_name:<16s}  {s['total']:>4d}  {s['win_rate']:>5.1f}%"
            f"  {s['avg_pct']:>+8.2f}%  {s['profit_factor']:>6.2f}  {s['mdd']:>+6.2f}%"
        )


# ── 백테스트 메인 ─────────────────────────────────────────────────

def run_backtest(stocks: list[dict]) -> list[dict]:
    total    = len(stocks)
    failed   = 0
    skipped  = 0
    all_trades: list[dict] = []

    bt_start = pd.Timestamp(BACKTEST_START)
    bt_end   = pd.Timestamp(BACKTEST_END)
    width    = len(str(total))

    for idx, s in enumerate(stocks, 1):
        code         = s["code"]
        name         = s.get("name", code)
        market_cap_b = s.get("market_cap_b")
        sector_group = s.get("sector_group", "기타")
        print(f"  [{idx:{width}d}/{total}] {code} {name:<16s}", end="", flush=True)

        try:
            df = fdr.DataReader(code, FETCH_FROM, BACKTEST_END)
        except Exception as exc:
            print(f"  다운로드 실패: {exc}")
            failed += 1
            continue

        if df is None or len(df) < 30:
            print("  데이터 부족")
            failed += 1
            continue

        df = df.sort_index()

        # 거래량 필터: 일평균 거래량 부족 종목 스킵
        avg_vol = df["Volume"].mean()
        if avg_vol < MIN_AVG_VOLUME:
            print(f"  거래량 부족 ({avg_vol:.0f}주 평균)")
            skipped += 1
            continue

        vol_60d      = _calc_vol60(df)
        signal_count = 0

        for i, (ts, _) in enumerate(df.iterrows()):
            if ts < bt_start or ts > bt_end:
                continue

            # 신호 감지: 당일까지 최대 25 거래일 데이터 (최신→과거)
            start_i   = max(0, i - 24)
            candles   = _to_detect_candles(df.iloc[start_i: i + 1])
            detection = _detect_local(candles)

            if not detection["is_rebound"]:
                continue

            # 진입: 다음 거래일 시초가
            if i + 1 >= len(df):
                continue
            next_ts, next_row = df.index[i + 1], df.iloc[i + 1]
            entry_price = int(float(next_row.get("Open") or 0))
            if entry_price <= 0:
                continue

            # 시뮬레이션: 진입일부터 최대 MAX_HOLD_DAYS+1 거래일 (과거→최신)
            sim_end_i   = min(i + MAX_HOLD_DAYS + 2, len(df))
            sim_candles = _to_sim_candles(df.iloc[i + 1: sim_end_i])
            if not sim_candles:
                continue

            result = _simulate(entry_price, sim_candles)
            result.update({
                "code":         code,
                "name":         name,
                "signal_date":  ts.strftime("%Y-%m-%d"),
                "entry_date":   next_ts.strftime("%Y-%m-%d"),
                "entry_price":  entry_price,
                "confidence":   detection["confidence"],
                "signals":      detection["signals"],
                "market_cap_b": market_cap_b,
                "sector_group": sector_group,
                "vol_60d":      vol_60d,
            })
            all_trades.append(result)
            signal_count += 1

        print(f"  신호 {signal_count}건")

    if failed:
        print(f"\n  데이터 조회 실패: {failed}종목")
    if skipped:
        print(f"  거래량 필터 제외: {skipped}종목")
    return all_trades


# ── 진입점 ────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="V자 반등 신호 백테스트")
    parser.add_argument("--quick", action="store_true", help="상위 50종목만 빠른 검증")
    parser.add_argument("--full",  action="store_true", help="전체 KOSPI+KOSDAQ (~2600종목, 1~2시간)")
    parser.add_argument("--codes", nargs="+", metavar="CODE", help="특정 종목코드 지정")
    args = parser.parse_args()

    sep = "=" * 44
    print(f"\n{sep}")
    print(f"  V자 반등 신호 백테스트")
    print(f"  기간: {BACKTEST_START} ~ {BACKTEST_END}")
    print(f"  진입: 다음날 시초가  /  손절: -{STOP_LOSS_PCT*100:.0f}%")
    print(f"  목표: +{TAKE_PROFIT_PCT*100:.0f}% → 트레일링 -{TRAILING_DROP*100:.0f}%")
    print(f"  최대 보유: {MAX_HOLD_DAYS}일")
    print(sep)

    # 종목 목록 결정
    if args.codes:
        stocks = [{"code": c.zfill(6), "name": c, "market_cap_b": None, "sector_group": "기타"} for c in args.codes]
        print(f"  대상: 지정 종목 {len(stocks)}개")
    elif args.quick:
        stocks = _get_target_codes(kospi_n=35, kosdaq_n=15)
        print(f"  대상: 상위 {len(stocks)}종목 (quick 모드)")
    elif args.full:
        print(f"  전체 종목 목록 조회 중 (시총 {MIN_CAP_B}억+ 필터)...")
        stocks = _get_all_stocks(MIN_CAP_B)
        print(f"  대상: 전체 KOSPI+KOSDAQ {len(stocks)}종목 (--full)")
    else:
        stocks = _get_target_codes(KOSPI_N, KOSDAQ_N)
        print(f"  대상: KOSPI{KOSPI_N} + KOSDAQ{KOSDAQ_N} = {len(stocks)}종목")

    # 스팩 제외
    spac_n  = sum(1 for s in stocks if is_spac(s.get("name", "")))
    stocks  = [s for s in stocks if not is_spac(s.get("name", ""))]
    if spac_n:
        print(f"  스팩 제외: {spac_n}개 → 유효 대상: {len(stocks)}종목")

    print(f"\n[데이터 수집 및 신호 스캔]")
    all_trades = run_backtest(stocks)

    # ── 통계 출력 ────────────────────────────────────────────────
    high_trades   = [t for t in all_trades if t["confidence"] == "high"]
    medium_trades = [t for t in all_trades if t["confidence"] == "medium"]

    print(f"\n{sep}")
    print(f"  === V자 반등 백테스트 결과 ===")
    print(f"  기간: {BACKTEST_START} ~ {BACKTEST_END}")
    spac_tag = f" (스팩 {spac_n}개 제외)" if spac_n else ""
    print(f"  대상: {len(stocks)}종목{spac_tag}")
    _print_stats("전체 (high + medium)", all_trades)
    _print_stats("confidence=high 만", high_trades)
    _print_stats("confidence=medium 만", medium_trades)

    # 신호별 빈도 분석
    if all_trades:
        from collections import Counter
        sig_counter: Counter = Counter()
        for t in all_trades:
            for s in t["signals"]:
                key = s.split()[0]  # 숫자 제거 ("RSI 28" → "RSI")
                sig_counter[key] += 1
        print("\n[신호 발생 빈도]")
        for sig, cnt in sig_counter.most_common():
            print(f"  {sig:<14s}: {cnt}건")

    # ── 분류별 분석 ──────────────────────────────────────────────
    breakdown_json: dict = {}
    if all_trades:
        # 시가총액별
        cap_buckets: dict[str, list[dict]] = {}
        for t in all_trades:
            b = _cap_bucket(t.get("market_cap_b"))
            cap_buckets.setdefault(b, []).append(t)
        _print_breakdown_table("시가총액별", cap_buckets)
        breakdown_json["by_cap"] = {k: _calc_stats(v) for k, v in cap_buckets.items()}

        # 섹터별
        sec_buckets: dict[str, list[dict]] = {}
        for t in all_trades:
            b = t.get("sector_group") or "기타"
            sec_buckets.setdefault(b, []).append(t)
        _print_breakdown_table("섹터별", sec_buckets)
        breakdown_json["by_sector"] = {k: _calc_stats(v) for k, v in sec_buckets.items()}

        # 변동성별
        vol_buckets: dict[str, list[dict]] = {}
        for t in all_trades:
            b = _vol_bucket(t.get("vol_60d"))
            vol_buckets.setdefault(b, []).append(t)
        _print_breakdown_table("변동성별", vol_buckets)
        breakdown_json["by_vol"] = {k: _calc_stats(v) for k, v in vol_buckets.items()}

    # ── JSON 저장 ────────────────────────────────────────────────
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mode_tag = "full" if args.full else ("quick" if args.quick else "default")
    out_path = DATA_DIR / f"backtest_rebound_{TODAY}_{mode_tag}.json"
    payload = {
        "run_date":  TODAY,
        "mode":      mode_tag,
        "period":    {"start": BACKTEST_START, "end": BACKTEST_END},
        "params": {
            "take_profit": TAKE_PROFIT_PCT,
            "stop_loss":   STOP_LOSS_PCT,
            "trailing":    TRAILING_DROP,
            "max_hold":    MAX_HOLD_DAYS,
        },
        "stats": {
            "all":    _calc_stats(all_trades),
            "high":   _calc_stats(high_trades),
            "medium": _calc_stats(medium_trades),
        },
        "breakdown": breakdown_json,
        "trades":    all_trades,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")

    # 텔레그램 요약 (선택)
    try:
        from telegram_alert import send_message
        s_all  = _calc_stats(all_trades)
        s_high = _calc_stats(high_trades)
        if s_all.get("total", 0) > 0:
            lines = [
                f"📊 V자 반등 백테스트 ({BACKTEST_START}~{BACKTEST_END})",
                "",
                f"[전체] {s_all['total']}건 / 승률 {s_all['win_rate']}% / 평균 {s_all['avg_pct']:+.1f}% / 손익비 {s_all['profit_factor']:.2f}",
            ]
            if s_high.get("total", 0):
                lines.append(
                    f"[high만] {s_high['total']}건 / 승률 {s_high['win_rate']}% / 평균 {s_high['avg_pct']:+.1f}%"
                )
            send_message("\n".join(lines))
    except Exception:
        pass  # 텔레그램 실패는 무시


if __name__ == "__main__":
    main()
