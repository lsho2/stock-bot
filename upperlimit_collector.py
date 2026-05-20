#!/usr/bin/env python3
"""상한가 사례 수집기.

지난 6개월 동안 상한가(+30%)를 기록한 종목의 D-3/D-2/D-1 데이터 수집.

대상: KOSPI200 + KOSDAQ150 (시총 상위 350종목)
저장: data/upperlimit_cases.json

실행:
    python upperlimit_collector.py
    python upperlimit_collector.py --quick    # 상위 50종목 테스트
"""

import sys
import json
import argparse
import time
from datetime import date, timedelta
from pathlib import Path

try:
    import FinanceDataReader as fdr
    import pandas as pd
except ImportError:
    sys.exit("pip install finance-datareader pandas 후 재실행")

from stock_info import is_spac

UPPER_LIMIT_PCT = 29.9   # 상한가 기준 (전일 대비 %)
HISTORY_DAYS    = 45     # RSI14 + MA20 + D-3 여유 (calendar days)
API_DELAY       = 0.2    # 요청 간 딜레이 (초)
KOSPI_N         = 200
KOSDAQ_N        = 150
MIN_CAP_B       = 100    # --full 시총 하한 (억원)
MIN_AVG_VOLUME  = 10_000 # --full 일평균 거래량 하한 (주)

DATA_DIR    = Path(r"C:\Stock\stock-bot\data")
OUTPUT_PATH = DATA_DIR / "upperlimit_cases.json"

PERIOD_START = "2025-11-19"
PERIOD_END   = "2026-05-18"
FETCH_FROM   = "2025-10-01"   # RSI/MA 계산용 버퍼


# ── 지표 계산 ─────────────────────────────────────────────────────

def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI. closes는 과거→최신 순."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return round(100.0 - 100.0 / (1 + ag / al), 1)


def _ma(vals: list[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return round(sum(vals[-n:]) / n, 1)


# ── 종목 목록 ─────────────────────────────────────────────────────

def _get_stock_list(kospi_n: int, kosdaq_n: int) -> list[dict]:
    """시총 상위 KOSPI + KOSDAQ 종목 목록."""
    results: list[dict] = []
    for market, n in (("KOSPI", kospi_n), ("KOSDAQ", kosdaq_n)):
        try:
            df = fdr.StockListing(market)
            cap_col = next(
                (c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()),
                None,
            )
            df = df.nlargest(n, cap_col) if cap_col else df.head(n)
            code_col   = next((c for c in df.columns if c.lower() == "code"),   df.columns[0])
            name_col   = next((c for c in df.columns if c.lower() == "name"),   df.columns[1])
            sector_col = next((c for c in df.columns if "sector" in c.lower() or "업종" in c.lower()), None)
            cap_col2   = cap_col
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                if not code or code == "000000":
                    continue
                results.append({
                    "code":       code,
                    "name":       str(row.get(name_col, code)).strip(),
                    "market":     market,
                    "sector":     str(row[sector_col]).strip() if sector_col and pd.notna(row.get(sector_col)) else "",
                    "market_cap_b": round(float(row[cap_col2]) / 1e8, 1) if cap_col2 and pd.notna(row.get(cap_col2)) else None,
                })
        except Exception as exc:
            print(f"  [{market}] 종목 목록 오류: {exc}")
    return results


# ── 전체 종목 목록 (--full 모드) ─────────────────────────────────

def _get_all_stocks(min_cap_b: float = MIN_CAP_B) -> list[dict]:
    """전체 KOSPI + KOSDAQ 종목 (시총 min_cap_b억 미만 제외)."""
    results: list[dict] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df = fdr.StockListing(market)
            cap_col    = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
            code_col   = next((c for c in df.columns if c.lower() == "code"),   df.columns[0])
            name_col   = next((c for c in df.columns if c.lower() == "name"),   df.columns[1])
            sector_col = next((c for c in df.columns if "sector" in c.lower() or "업종" in c.lower()), None)
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
                    "sector":       str(row[sector_col]).strip() if sector_col and pd.notna(row.get(sector_col)) else "",
                    "market_cap_b": cap_b,
                })
            added = len(results) - before
            print(f"  [{market}] {added}종목")
        except Exception as exc:
            print(f"  [{market}] 목록 조회 실패: {exc}")
    return results


# ── 단일 종목 OHLCV 조회 ──────────────────────────────────────────

def _fetch_df(code: str) -> pd.DataFrame | None:
    """fdr로 FETCH_FROM ~ PERIOD_END 일봉 반환. 실패 시 None."""
    try:
        df = fdr.DataReader(code, FETCH_FROM, PERIOD_END)
        if df is None or len(df) < 10:
            return None
        df = df.sort_index()
        return df
    except Exception:
        return None


def _row_to_dict(ts: pd.Timestamp, row: pd.Series) -> dict:
    def _i(k): return int(float(row.get(k, 0) or 0))
    def _f(k): return round(float(row.get(k, 0.0) or 0.0), 4)
    return {
        "date":   ts.strftime("%Y%m%d"),
        "open":   _i("Open"),
        "high":   _i("High"),
        "low":    _i("Low"),
        "close":  _i("Close"),
        "volume": _i("Volume"),
    }


# ── 상한가 이벤트 추출 ────────────────────────────────────────────

def _find_upperlimit_events(
    df: pd.DataFrame, code: str, name: str, market: str, sector: str, market_cap_b: float | None
) -> list[dict]:
    """DataFrame에서 상한가 발생일 탐색 → 각 이벤트의 D-3/D-2/D-1 데이터 구성."""
    bt_start = pd.Timestamp(PERIOD_START)
    bt_end   = pd.Timestamp(PERIOD_END)

    df = df.sort_index()
    closes  = df["Close"].tolist()
    volumes = df["Volume"].tolist()
    dates   = list(df.index)

    events: list[dict] = []

    for i in range(1, len(df)):
        ts = dates[i]
        if ts < bt_start or ts > bt_end:
            continue

        prev_close = closes[i - 1]
        cur_close  = closes[i]
        if prev_close <= 0:
            continue

        chg_pct = (cur_close - prev_close) / prev_close * 100
        if chg_pct < UPPER_LIMIT_PCT:
            continue

        # D-3, D-2, D-1 인덱스 (상한가일 기준 직전 거래일)
        if i < 3:
            continue
        d_idx = {"D-3": i - 3, "D-2": i - 2, "D-1": i - 1}

        days_data: dict[str, dict] = {}
        for label, pos in d_idx.items():
            days_data[label] = _row_to_dict(dates[pos], df.iloc[pos])

        # D-1 기준 RSI, MA, 거래량 비율
        history_closes  = closes[max(0, i - 35): i]   # 최대 35일 (과거→최신)
        history_volumes = volumes[max(0, i - 21): i]

        rsi   = _calc_rsi(history_closes)
        ma5   = _ma(history_closes, 5)
        ma20  = _ma(history_closes, 20)
        vol_ma20 = round(sum(history_volumes[-20:]) / min(len(history_volumes), 20)) if history_volumes else 0
        d1_vol   = days_data["D-1"]["volume"]
        vol_ratio_d1 = round(d1_vol / vol_ma20, 2) if vol_ma20 > 0 else 0.0

        days_data["D-1"]["rsi14"]     = rsi
        days_data["D-1"]["ma5"]       = ma5
        days_data["D-1"]["ma20"]      = ma20
        days_data["D-1"]["vol_ma20"]  = vol_ma20
        days_data["D-1"]["vol_ratio"] = vol_ratio_d1

        events.append({
            "code":            code,
            "name":            name,
            "market":          market,
            "sector":          sector,
            "market_cap_b":    market_cap_b,
            "upperlimit_date": ts.strftime("%Y%m%d"),
            "ul_chg_pct":      round(chg_pct, 2),
            "days":            days_data,
        })

    return events


# ── 메인 ──────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="상위 50종목 테스트")
    parser.add_argument("--full",  action="store_true", help="전체 KOSPI+KOSDAQ (~2600종목)")
    args = parser.parse_args()

    sep = "=" * 44
    print(f"\n{sep}")
    print(f"  상한가 사례 수집기")
    print(f"  기간: {PERIOD_START} ~ {PERIOD_END}")
    print(sep)

    if args.full:
        print(f"  전체 종목 목록 조회 중 (시총 {MIN_CAP_B}억+, 거래량 {MIN_AVG_VOLUME:,}주+)...")
        stocks = _get_all_stocks(MIN_CAP_B)
        print(f"  대상: 전체 KOSPI+KOSDAQ {len(stocks)}종목 (--full)")
    elif args.quick:
        stocks = _get_stock_list(35, 15)
        print(f"  대상: {len(stocks)}종목 (quick 모드)")
    else:
        stocks = _get_stock_list(KOSPI_N, KOSDAQ_N)
        print(f"  대상: {len(stocks)}종목")

    # 스팩 제외
    spac_n  = sum(1 for s in stocks if is_spac(s.get("name", "")))
    stocks  = [s for s in stocks if not is_spac(s.get("name", ""))]
    if spac_n:
        print(f"  스팩 제외: {spac_n}개 → 유효 대상: {len(stocks)}종목")
    print()

    all_events: list[dict] = []
    failed   = 0
    skipped  = 0
    width    = len(str(len(stocks)))
    milestone = max(100, len(stocks) // 20)  # 100건마다 또는 5% 단위

    for idx, s in enumerate(stocks, 1):
        code, name = s["code"], s["name"]
        print(f"  [{idx:{width}d}/{len(stocks)}] {code} {name:<18s}", end="", flush=True)

        df = _fetch_df(code)
        if df is None:
            print("  데이터 없음")
            failed += 1
            time.sleep(API_DELAY)
            continue

        # --full 모드: 거래량 부족 종목 스킵
        if args.full:
            avg_vol = df["Volume"].mean()
            if avg_vol < MIN_AVG_VOLUME:
                print(f"  거래량 부족 ({avg_vol:.0f}주 평균)")
                skipped += 1
                time.sleep(API_DELAY)
                continue

        events = _find_upperlimit_events(
            df, code, name, s["market"], s["sector"], s["market_cap_b"]
        )
        all_events.extend(events)
        print(f"  상한가 {len(events)}건")

        # 진행 요약 (milestone 단위)
        if idx % milestone == 0:
            print(f"\n  ── [{idx}/{len(stocks)}] 중간 집계: 상한가 {len(all_events)}건 / 실패 {failed} / 스킵 {skipped} ──\n")

        time.sleep(API_DELAY)

    # 저장
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mode = "full" if args.full else ("quick" if args.quick else "default")
    payload = {
        "collected_at":  date.today().isoformat(),
        "mode":          mode,
        "period":        {"start": PERIOD_START, "end": PERIOD_END},
        "target_stocks": len(stocks),
        "failed_stocks": failed,
        "skipped_stocks": skipped,
        "total_events":  len(all_events),
        "cases": all_events,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{sep}")
    print(f"  수집 완료: 총 {len(all_events)}건 상한가 이벤트")
    if spac_n:
        print(f"  스팩 제외: {spac_n}개")
    if skipped:
        print(f"  거래량 필터 제외: {skipped}종목")
    if failed:
        print(f"  데이터 조회 실패: {failed}종목")
    print(f"  저장: {OUTPUT_PATH}")
    print(sep)


if __name__ == "__main__":
    main()
