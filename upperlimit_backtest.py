#!/usr/bin/env python3
"""상한가 예측기 점수 검증 백테스트.

predictor.py 스코어링 → 실제 D+1~D+5 수익률 상관관계 분석.
점수 구간별 승률·도달율·손익비를 테이블로 출력.

실행:
    python upperlimit_backtest.py                # 전체 ~2600개, 60일
    python upperlimit_backtest.py --quick        # KOSPI200+KOSDAQ150, 60일
    python upperlimit_backtest.py --days 120     # 검증 기간 120일
    python upperlimit_backtest.py --score-min 60 # 60점 이상만 분석
"""

import sys
import json
import argparse
import time
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

try:
    import FinanceDataReader as fdr
    import pandas as pd
except ImportError:
    sys.exit("pip install finance-datareader pandas 후 재실행")

from stock_info import is_spac

# predictor에서 스코어링 함수 import → 가중치 로직 자동 동기화
try:
    from upperlimit_predictor import _score_stock
except ImportError as exc:
    sys.exit(
        f"upperlimit_predictor.py 로드 실패: {exc}\n"
        "같은 디렉토리에 upperlimit_predictor.py가 있어야 합니다."
    )

# ── 설정 ──────────────────────────────────────────────────────────

DATA_DIR      = Path(r"C:\Stock\stock-bot\data")
PATTERNS_PATH = DATA_DIR / "upperlimit_patterns.json"
TODAY         = date.today().isoformat()

MIN_CAP_B      = 100
MIN_AVG_VOLUME = 10_000
KOSPI_N        = 200
KOSDAQ_N       = 150
API_DELAY      = 0.1   # 종목 간 딜레이 (초)

_DEFAULT_WEIGHTS = {
    "vol_ratio_above_2":     25,
    "vol_ratio_above_1.5":   15,
    "vol_trend_increasing":  10,
    "d1_yang_bong":          10,
    "d2_yang_bong":           5,
    "market_cap_under_500b": 15,
    "rsi_40_to_75":          10,
    "above_ma5":             10,
    "above_ma20":             5,
}

# 점수 구간: (표시명, lo, hi_exclusive)
SCORE_BUCKETS = [
    ("0~40",  0,  40),
    ("40~60", 40, 60),
    ("60~70", 60, 70),
    ("70~80", 70, 80),
    ("80+",   80, 101),
]


# ── 점수 구간 헬퍼 ────────────────────────────────────────────────

def _bucket_label(score: int) -> str:
    for label, lo, hi in SCORE_BUCKETS:
        if lo <= score < hi:
            return label
    return "80+"


# ── 종목 목록 ─────────────────────────────────────────────────────

def _get_stock_list(kospi_n: int = KOSPI_N, kosdaq_n: int = KOSDAQ_N) -> list[dict]:
    """시총 상위 N개 (--quick 모드)."""
    results: list[dict] = []
    for market, n in (("KOSPI", kospi_n), ("KOSDAQ", kosdaq_n)):
        try:
            df = fdr.StockListing(market)
            cap_col  = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
            df       = df.nlargest(n, cap_col) if cap_col else df.head(n)
            code_col = next((c for c in df.columns if c.lower() == "code"), df.columns[0])
            name_col = next((c for c in df.columns if c.lower() == "name"), df.columns[1])
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                if not code or code == "000000":
                    continue
                cap_raw = row[cap_col] if cap_col else None
                cap_b   = round(float(cap_raw) / 1e8, 1) if cap_raw is not None and pd.notna(cap_raw) else None
                results.append({
                    "code":         code,
                    "name":         str(row.get(name_col, code)).strip(),
                    "market":       market,
                    "market_cap_b": cap_b,
                })
        except Exception as exc:
            print(f"  [{market}] 목록 오류: {exc}")
    return results


def _get_all_stocks(min_cap_b: float = MIN_CAP_B) -> list[dict]:
    """전체 KOSPI + KOSDAQ (시총 min_cap_b억 미만 제외)."""
    results: list[dict] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
            cap_col  = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
            code_col = next((c for c in df.columns if c.lower() == "code"), df.columns[0])
            name_col = next((c for c in df.columns if c.lower() == "name"), df.columns[1])
            before = len(results)
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                if not code or code == "000000":
                    continue
                cap_raw = row[cap_col] if cap_col else None
                cap_b   = round(float(cap_raw) / 1e8, 1) if cap_raw is not None and pd.notna(cap_raw) else None
                if cap_b is not None and cap_b < min_cap_b:
                    continue
                results.append({
                    "code":         code,
                    "name":         str(row.get(name_col, code)).strip(),
                    "market":       market,
                    "market_cap_b": cap_b,
                })
            print(f"  [{market}] {len(results) - before}종목")
        except Exception as exc:
            print(f"  [{market}] 목록 오류: {exc}")
    return results


# ── 수익률 지표 계산 ──────────────────────────────────────────────

def _calc_signal_metrics(base_close: int, future_df: pd.DataFrame) -> dict | None:
    """D 종가(base_close) 대비 D+1..D+5 수익률 지표.

    base_close: D 종가 (기준 가격)
    future_df : D+1부터 최대 5 거래일 OHLCV (과거→최신 정렬)
    """
    if base_close <= 0 or len(future_df) == 0:
        return None
    n = min(5, len(future_df))
    highs  = [int(float(future_df["High"].iloc[i]))  for i in range(n)]
    lows   = [int(float(future_df["Low"].iloc[i]))   for i in range(n)]
    closes = [int(float(future_df["Close"].iloc[i])) for i in range(n)]

    d1_return  = (closes[0] - base_close) / base_close * 100
    max_return = (max(highs) - base_close) / base_close * 100
    min_return = (min(lows)  - base_close) / base_close * 100

    return {
        "d1_return":          round(d1_return,  2),
        "d5_max_return":      round(max_return, 2),
        "d5_min_return":      round(min_return, 2),
        "reached_5pct":       any(h >= base_close * 1.05  for h in highs),
        "reached_10pct":      any(h >= base_close * 1.10  for h in highs),
        "reached_upperlimit": any(h >= base_close * 1.295 for h in highs),
        "hit_stoploss_3pct":  any(l <= base_close * 0.97  for l in lows),
        "days_tracked":       n,
    }


# ── 통계 ─────────────────────────────────────────────────────────

def _calc_stats(signals: list[dict]) -> dict:
    """신호 리스트 → 집계 통계 dict."""
    n = len(signals)
    if n == 0:
        return {"n": 0}

    d1_vals  = [s["d1_return"]     for s in signals if s.get("d1_return")     is not None]
    max_vals = [s["d5_max_return"] for s in signals if s.get("d5_max_return") is not None]
    min_vals = [s["d5_min_return"] for s in signals if s.get("d5_min_return") is not None]

    wins   = [v for v in d1_vals if v > 0]
    losses = [v for v in d1_vals if v <= 0]
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    return {
        "n":                  n,
        "d1_avg":             round(sum(d1_vals)  / len(d1_vals),  2) if d1_vals  else None,
        "d1_win_rate":        round(len(wins) / len(d1_vals) * 100, 1) if d1_vals else None,
        "d5_avg_max":         round(sum(max_vals) / len(max_vals), 2) if max_vals else None,
        "d5_avg_min":         round(sum(min_vals) / len(min_vals), 2) if min_vals else None,
        "reached_5pct_rate":  round(sum(1 for s in signals if s.get("reached_5pct"))       / n * 100, 1),
        "reached_10pct_rate": round(sum(1 for s in signals if s.get("reached_10pct"))      / n * 100, 1),
        "reached_ul_rate":    round(sum(1 for s in signals if s.get("reached_upperlimit"))  / n * 100, 1),
        "hit_sl3_rate":       round(sum(1 for s in signals if s.get("hit_stoploss_3pct"))   / n * 100, 1),
        "profit_factor":      round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 999.0,
    }


def _print_bucket_table(bucket_stats: dict[str, dict]) -> None:
    """점수 구간별 통계 테이블 출력."""
    hdr = (
        f"  {'구간':<8}  {'N':>5}  {'D+1평균':>8}  {'D+1승률':>7}  "
        f"{'5%도달':>6}  {'10%도달':>7}  {'상한가':>6}  {'손절-3%':>7}  {'손익비':>6}"
    )
    div = (
        f"  {'─'*8}  {'─'*5}  {'─'*8}  {'─'*7}  "
        f"{'─'*6}  {'─'*7}  {'─'*6}  {'─'*7}  {'─'*6}"
    )
    print(hdr)
    print(div)
    for label, _, _ in SCORE_BUCKETS:
        s = bucket_stats.get(label, {"n": 0})
        if s.get("n", 0) == 0:
            dash = "─"
            print(
                f"  {label:<8}  {dash:>5}  {dash:>8}  {dash:>7}  "
                f"{dash:>6}  {dash:>7}  {dash:>6}  {dash:>7}  {dash:>6}"
            )
            continue
        print(
            f"  {label:<8}  {s['n']:>5d}"
            f"  {s['d1_avg']:>+7.2f}%  {s['d1_win_rate']:>6.1f}%"
            f"  {s['reached_5pct_rate']:>5.1f}%  {s['reached_10pct_rate']:>6.1f}%"
            f"  {s['reached_ul_rate']:>5.1f}%  {s['hit_sl3_rate']:>6.1f}%"
            f"  {s['profit_factor']:>6.2f}"
        )


# ── 백테스트 ─────────────────────────────────────────────────────

def run_backtest(
    stocks: list[dict],
    weights: dict,
    days: int,
    score_min: int,
) -> list[dict]:
    """각 종목·날짜별 점수 계산 → 다음 5 거래일 수익률 추적."""
    total   = len(stocks)
    failed  = 0
    skipped = 0
    all_signals: list[dict] = []
    width   = len(str(total))

    fetch_from = (date.today() - timedelta(days=days + 80)).strftime("%Y-%m-%d")
    bt_start   = pd.Timestamp(date.today() - timedelta(days=days))
    # D+1 데이터 확보 위해 최소 2일 여유
    bt_end     = pd.Timestamp(date.today() - timedelta(days=2))

    for idx, s in enumerate(stocks, 1):
        code         = s["code"]
        name         = s.get("name", code)
        market_cap_b = s.get("market_cap_b")

        try:
            df = fdr.DataReader(code, fetch_from, TODAY)
        except Exception as exc:
            print(f"  [{idx:{width}d}/{total}] {code} {name:<16s}  다운로드 실패: {exc}")
            failed += 1
            time.sleep(API_DELAY)
            continue

        if df is None or len(df) < 30:
            print(f"  [{idx:{width}d}/{total}] {code} {name:<16s}  데이터 부족")
            failed += 1
            time.sleep(API_DELAY)
            continue

        df = df.sort_index()

        avg_vol = df["Volume"].mean()
        if avg_vol < MIN_AVG_VOLUME:
            print(f"  [{idx:{width}d}/{total}] {code} {name:<16s}  거래량 부족 ({avg_vol:.0f}주)")
            skipped += 1
            time.sleep(API_DELAY)
            continue

        sig_count  = 0
        sig_d1_sum = 0.0
        best_score = 0

        for i, (ts, _) in enumerate(df.iterrows()):
            if ts < bt_start or ts > bt_end:
                continue
            if i < 25:          # RSI14 + MA20 계산 버퍼
                continue
            if i + 1 >= len(df):  # D+1 데이터 없음
                continue

            # D일까지의 데이터로 점수 계산 (predictor와 동일 로직)
            score, sigs = _score_stock(df.iloc[: i + 1], market_cap_b, weights)

            if score < score_min:
                continue

            # D+1 ~ D+5 수익률 추적
            base_close = int(float(df["Close"].iloc[i]))
            future_df  = df.iloc[i + 1: i + 6]
            metrics    = _calc_signal_metrics(base_close, future_df)
            if metrics is None:
                continue

            all_signals.append({
                "code":         code,
                "name":         name,
                "date":         ts.strftime("%Y-%m-%d"),
                "score":        score,
                "bucket":       _bucket_label(score),
                "signals":      sigs,
                "base_close":   base_close,
                "market_cap_b": market_cap_b,
                **metrics,
            })
            sig_count  += 1
            sig_d1_sum += metrics["d1_return"]
            best_score  = max(best_score, score)

        # 종목별 요약
        if sig_count > 0:
            d1_avg = sig_d1_sum / sig_count
            print(
                f"  [{idx:{width}d}/{total}] {code} {name:<16s}"
                f"  신호 {sig_count}건 / 최고 {best_score}점 / D+1 평균 {d1_avg:+.1f}%"
            )
        else:
            print(f"  [{idx:{width}d}/{total}] {code} {name:<16s}  신호 없음")

        # 100종목마다 중간 저장
        if idx % 100 == 0:
            _save_partial(all_signals, idx, total)

        time.sleep(API_DELAY)

    if failed:
        print(f"\n  데이터 조회 실패: {failed}종목")
    if skipped:
        print(f"  거래량 필터 제외: {skipped}종목")
    return all_signals


def _save_partial(signals: list[dict], done: int, total: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"backtest_upperlimit_{TODAY}_partial.json"
    path.write_text(
        json.dumps(
            {"saved_at": done, "total": total, "count": len(signals), "signals": signals},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  ── [{done}/{total}] 중간 저장: {len(signals)}건 → {path.name} ──\n")


# ── 진입점 ────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="상한가 예측기 점수 검증 백테스트")
    parser.add_argument(
        "--quick",     action="store_true",
        help=f"KOSPI{KOSPI_N}+KOSDAQ{KOSDAQ_N} 350개 (빠른 검증)",
    )
    parser.add_argument(
        "--days",      type=int, default=60, metavar="N",
        help="검증 기간 일수 (기본 60, 최대 250)",
    )
    parser.add_argument(
        "--score-min", type=int, default=0,  metavar="N",
        help="최소 점수 필터 (기본 0 = 전체 포함)",
    )
    args = parser.parse_args()
    args.days = min(max(args.days, 5), 250)

    sep = "=" * 52
    print(f"\n{sep}")
    print(f"  상한가 예측기 점수 검증 백테스트")
    print(f"  검증 기간: 최근 {args.days}일  /  최소 점수: {args.score_min}점+")
    print(sep)

    # ── 가중치 로드 ────────────────────────────────────────────
    weights = _DEFAULT_WEIGHTS.copy()
    if PATTERNS_PATH.exists():
        try:
            pdata   = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
            weights = pdata.get("patterns", {}).get("scoring_weights", weights)
            print(f"  패턴 로드: {PATTERNS_PATH.name} ({pdata.get('analyzed_at', '?')})")
        except Exception as exc:
            print(f"  패턴 로드 실패 ({exc}) → 기본 가중치")
    else:
        print("  patterns.json 없음 → 기본 가중치")

    # ── 종목 목록 ──────────────────────────────────────────────
    if args.quick:
        stocks = _get_stock_list(KOSPI_N, KOSDAQ_N)
        print(f"  종목: {len(stocks)}개 (--quick)")
    else:
        print(f"  전체 종목 목록 조회 중 (시총 {MIN_CAP_B}억+)...")
        stocks = _get_all_stocks(MIN_CAP_B)
        print(f"  종목: {len(stocks)}개 (전체)")

    # 스팩 제외
    spac_n = sum(1 for s in stocks if is_spac(s.get("name", "")))
    stocks = [s for s in stocks if not is_spac(s.get("name", ""))]
    if spac_n:
        print(f"  스팩 제외: {spac_n}개 → 유효: {len(stocks)}종목")
    print()

    # ── 백테스트 실행 ──────────────────────────────────────────
    print("[신호 스캔 및 수익률 추적]")
    all_signals = run_backtest(stocks, weights, args.days, args.score_min)

    # ── 결과 집계 ──────────────────────────────────────────────
    bucket_signals: dict[str, list[dict]] = defaultdict(list)
    for sig in all_signals:
        bucket_signals[sig["bucket"]].append(sig)

    bucket_stats = {
        label: _calc_stats(bucket_signals[label])
        for label, _, _ in SCORE_BUCKETS
    }
    overall = _calc_stats(all_signals)

    # ── 결과 출력 ──────────────────────────────────────────────
    print(f"\n{sep}")
    print(f"  === 상한가 예측기 점수 검증 결과 ===")
    print(f"  검증 기간 {args.days}일 / 총 신호 {len(all_signals)}건 / 최소점수 {args.score_min}+")
    if spac_n:
        print(f"  스팩 제외 {spac_n}개")
    print()
    _print_bucket_table(bucket_stats)
    print()

    if overall.get("n", 0) > 0:
        print(
            f"  [전체] N={overall['n']}"
            f" / D+1 평균 {overall['d1_avg']:+.2f}%"
            f" / D+1 승률 {overall['d1_win_rate']:.1f}%"
            f" / 5% 도달 {overall['reached_5pct_rate']:.1f}%"
            f" / 상한가 도달 {overall['reached_ul_rate']:.1f}%"
            f" / 손익비 {overall['profit_factor']:.2f}"
        )
    else:
        print(f"  [전체] 분석 대상 신호 없음 (--score-min {args.score_min} 이상 없음)")
    print(sep)

    # ── JSON 저장 ──────────────────────────────────────────────
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mode     = "quick" if args.quick else "full"
    out_path = DATA_DIR / f"backtest_upperlimit_{TODAY}_{mode}.json"
    payload  = {
        "run_date":       TODAY,
        "mode":           mode,
        "days":           args.days,
        "score_min":      args.score_min,
        "total_stocks":   len(stocks) + spac_n,
        "spac_excluded":  spac_n,
        "scanned_stocks": len(stocks),
        "total_signals":  len(all_signals),
        "bucket_stats":   bucket_stats,
        "overall_stats":  overall,
        "signals":        all_signals,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  결과 저장: {out_path}")

    # 중간 저장 파일 정리
    partial = DATA_DIR / f"backtest_upperlimit_{TODAY}_partial.json"
    if partial.exists():
        partial.unlink()
        print(f"  중간 저장 파일 삭제: {partial.name}")


if __name__ == "__main__":
    main()
