#!/usr/bin/env python3
"""상한가 예측기.

오늘 장 마감 데이터 기준 내일 상한가 가능성 높은 종목 탐색 및 텔레그램 알림.

실행:
    python upperlimit_predictor.py
    python upperlimit_predictor.py --dry-run   # 텔레그램 발송 안 함

권장 스케줄: 매일 16:30 (장 마감 + 데이터 반영 여유)
"""

import sys
import json
import time
import argparse
from datetime import date, timedelta
from pathlib import Path

try:
    import FinanceDataReader as fdr
    import pandas as pd
except ImportError:
    sys.exit("pip install finance-datareader pandas 후 재실행")

from stock_info import is_spac

DATA_DIR      = Path(r"C:\Stock\stock-bot\data")
PATTERNS_PATH = DATA_DIR / "upperlimit_patterns.json"
ALERT_SCORE   = 60    # 텔레그램 알림 기준
TOP_N         = 10    # 상위 N개만 알림
KOSPI_N       = 200   # --quick 모드 전용
KOSDAQ_N      = 150   # --quick 모드 전용
MIN_CAP_B     = 100   # 시총 하한 (억원)
MIN_AVG_VOLUME = 10_000  # 60일 평균 거래량 하한 (주)
API_DELAY     = 0.15

TODAY = date.today().isoformat()

# ── 기본 스코어링 가중치 (patterns.json 없을 때 fallback) ──────────
_DEFAULT_WEIGHTS = {
    "vol_ratio_above_2":      25,
    "vol_ratio_above_1.5":    15,
    "vol_trend_increasing":   10,
    "d1_yang_bong":           10,
    "d2_yang_bong":            5,
    "market_cap_under_500b":  15,
    "rsi_40_to_75":           10,
    "above_ma5":              10,
    "above_ma20":              5,  # 패딩 (합계=105지만 실질 100점 만점)
}


# ── 지표 계산 (rebound_backtest.py와 동일) ─────────────────────────

def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
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
    return sum(vals[-n:]) / n


# ── 종목 목록 ─────────────────────────────────────────────────────

def _get_stock_list(kospi_n: int, kosdaq_n: int) -> list[dict]:
    """시총 상위 N개 (--quick 모드)."""
    results: list[dict] = []
    for market, n in (("KOSPI", kospi_n), ("KOSDAQ", kosdaq_n)):
        try:
            df = fdr.StockListing(market)
            cap_col = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
            df = df.nlargest(n, cap_col) if cap_col else df.head(n)
            code_col = next((c for c in df.columns if c.lower() == "code"), df.columns[0])
            name_col = next((c for c in df.columns if c.lower() == "name"), df.columns[1])
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                if not code or code == "000000":
                    continue
                cap = float(row[cap_col]) if cap_col and pd.notna(row.get(cap_col)) else None
                results.append({
                    "code":         code,
                    "name":         str(row.get(name_col, code)).strip(),
                    "market":       market,
                    "market_cap_b": round(cap / 1e8, 1) if cap else None,
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


# ── 스코어 계산 ───────────────────────────────────────────────────

def _score_stock(
    df: pd.DataFrame,
    market_cap_b: float | None,
    weights: dict,
) -> tuple[int, list[str]]:
    """점수 및 매칭 신호 목록 반환."""
    df = df.sort_index()
    closes  = df["Close"].tolist()
    volumes = df["Volume"].tolist()

    if len(closes) < 6:
        return 0, []

    cur_close  = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else cur_close
    cur_open   = float(df["Open"].iloc[-1]) if "Open" in df.columns else cur_close
    cur_vol    = volumes[-1]

    # 지표 계산
    rsi     = _calc_rsi(closes[:-1])            # 오늘 제외 (전날까지)
    ma5     = _ma(closes[:-1], 5)
    ma20    = _ma(closes[:-1], 20)
    vol_ma20 = (sum(volumes[-21:-1]) / 20) if len(volumes) >= 21 else (sum(volumes[:-1]) / max(len(volumes) - 1, 1))
    vol_ratio = cur_vol / vol_ma20 if vol_ma20 > 0 else 0.0
    d1_yang  = cur_close > cur_open

    # D-2 양봉 체크
    d2_yang = (closes[-2] > float(df["Open"].iloc[-2])) if (len(df) >= 2 and "Open" in df.columns) else False

    # D-3<D-2<D-1 거래량 증가 추세
    vol_trend = (len(volumes) >= 3 and volumes[-3] < volumes[-2] < volumes[-1])

    # 점수 집계
    score    = 0
    signals: list[str] = []

    w = weights

    if vol_ratio >= 2.0:
        score += w.get("vol_ratio_above_2", 25)
        signals.append(f"거래량 {vol_ratio:.1f}배")
    elif vol_ratio >= 1.5:
        score += w.get("vol_ratio_above_1.5", 15)
        signals.append(f"거래량 {vol_ratio:.1f}배")

    if vol_trend:
        score += w.get("vol_trend_increasing", 10)
        signals.append("거래량 증가 추세")

    if d1_yang:
        score += w.get("d1_yang_bong", 10)
        signals.append("양봉")

    if d2_yang:
        score += w.get("d2_yang_bong", 5)
        signals.append("전일 양봉")

    if market_cap_b is not None and market_cap_b < 500:
        score += w.get("market_cap_under_500b", 15)
        signals.append(f"소형주 {market_cap_b:.0f}억")

    if rsi is not None and 40 <= rsi <= 75:
        score += w.get("rsi_40_to_75", 10)
        signals.append(f"RSI {rsi:.0f}")

    if ma5 is not None and cur_close > ma5:
        score += w.get("above_ma5", 10)
        signals.append("MA5 상회")

    if ma20 is not None and cur_close > ma20:
        score += w.get("above_ma20", 5)
        signals.append("MA20 상회")

    # 오늘 이미 상한가에 근접한 종목은 제외 (내일 후보가 아님)
    if prev_close > 0:
        today_chg = (cur_close - prev_close) / prev_close * 100
        if today_chg >= 25:
            return 0, []   # 오늘 이미 상한가 → 내일 후보 아님

    return min(score, 100), signals


# ── 메인 ──────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",   action="store_true", help="텔레그램 발송 안 함")
    parser.add_argument("--quick",     action="store_true", help=f"시총 상위 {KOSPI_N+KOSDAQ_N}개만 (기존 모드)")
    parser.add_argument("--threshold", type=int, default=ALERT_SCORE, help=f"알림 기준 점수 (기본 {ALERT_SCORE})")
    args = parser.parse_args()

    sep = "=" * 44
    print(f"\n{sep}")
    print(f"  상한가 예측기  {TODAY}")
    print(sep)

    # 패턴 로드 (없으면 기본값)
    weights = _DEFAULT_WEIGHTS.copy()
    if PATTERNS_PATH.exists():
        try:
            pdata   = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
            weights = pdata.get("patterns", {}).get("scoring_weights", weights)
            print(f"  패턴 로드: {PATTERNS_PATH.name} ({pdata.get('analyzed_at', '?')})")
        except Exception as exc:
            print(f"  패턴 로드 실패 ({exc}) → 기본 가중치 사용")
    else:
        print(f"  patterns.json 없음 → 기본 가중치 사용 (analyzer 먼저 실행 권장)")

    # 종목 목록
    if args.quick:
        stocks = _get_stock_list(KOSPI_N, KOSDAQ_N)
        print(f"  스캔 대상: {len(stocks)}종목 (--quick)")
    else:
        print(f"  전체 종목 목록 조회 중 (시총 {MIN_CAP_B}억+)...")
        stocks = _get_all_stocks(MIN_CAP_B)
        print(f"  스캔 대상: {len(stocks)}종목")

    # 스팩 제외
    spac_n  = sum(1 for s in stocks if is_spac(s.get("name", "")))
    stocks  = [s for s in stocks if not is_spac(s.get("name", ""))]
    if spac_n:
        print(f"  스팩 제외: {spac_n}개 → 유효 대상: {len(stocks)}종목")
    print()

    # 스캔 기간 (25 거래일 = 약 35 달력일)
    fetch_from = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
    fetch_to   = TODAY

    candidates: list[dict] = []
    failed  = 0
    skipped = 0
    width   = len(str(len(stocks)))

    for idx, s in enumerate(stocks, 1):
        code, name = s["code"], s["name"]
        print(f"  [{idx:{width}d}/{len(stocks)}] {code} {name:<18s}", end="", flush=True)

        try:
            df = fdr.DataReader(code, fetch_from, fetch_to)
            if df is None or len(df) < 6:
                print("  데이터 부족")
                failed += 1
                time.sleep(API_DELAY)
                continue
            df = df.sort_index()
        except Exception as exc:
            print(f"  오류: {exc}")
            failed += 1
            time.sleep(API_DELAY)
            continue

        # 거래량 필터 (전체 모드에서만)
        if not args.quick:
            avg_vol = df["Volume"].mean()
            if avg_vol < MIN_AVG_VOLUME:
                print(f"  거래량 부족 ({avg_vol:.0f}주)")
                skipped += 1
                time.sleep(API_DELAY)
                continue

        score, signals = _score_stock(df, s.get("market_cap_b"), weights)
        cur_close = int(float(df["Close"].iloc[-1]))

        print(f"  {score:3d}점  {', '.join(signals[:3]) if signals else '-'}")

        if score >= args.threshold:
            candidates.append({
                "code":       code,
                "name":       name,
                "market":     s["market"],
                "score":      score,
                "signals":    signals,
                "cur_price":  cur_close,
                "market_cap_b": s.get("market_cap_b"),
            })

        time.sleep(API_DELAY)

    # 점수 내림차순 정렬
    candidates.sort(key=lambda x: -x["score"])
    top = candidates[:TOP_N]

    print(f"\n{sep}")
    scanned = len(stocks) - failed - skipped
    spac_tag = f" / 스팩제외 {spac_n}" if spac_n else ""
    print(f"  스캔 완료: {scanned}종목 / 후보 {len(candidates)}개 / 실패 {failed} / 거래량필터 {skipped}{spac_tag}")
    print(f"  알림 기준: {args.threshold}점 이상")

    if top:
        print(f"\n  ──  상한가 후보 TOP{min(TOP_N, len(top))}  ──")
        for i, c in enumerate(top, 1):
            print(f"  {i:2d}. {c['name']} ({c['code']}) {c['score']}점 | {', '.join(c['signals'])}")
    else:
        print(f"\n  {args.threshold}점 이상 종목 없음")
    print(sep)

    # 결과 저장
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"upperlimit_candidates_{TODAY}.json"
    out_path.write_text(
        json.dumps({"date": TODAY, "threshold": args.threshold, "candidates": candidates},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  결과 저장: {out_path}")

    # 텔레그램 발송
    if args.dry_run:
        print("  [dry-run] 텔레그램 발송 스킵")
        return

    if not top:
        return

    try:
        from telegram_alert import send_message
        lines = [f"🚀 상한가 후보  {TODAY}  (기준: {args.threshold}점+)"]
        for i, c in enumerate(top, 1):
            cap_str = f"{c['market_cap_b']:.0f}억" if c.get("market_cap_b") else "-"
            lines.append(
                f"{i}. {c['name']}({c['code']}) {c['score']}점 | "
                f"{c['cur_price']:,}원 {cap_str} | "
                f"{', '.join(c['signals'][:3])}"
            )
        lines.append("※ 상한가 보장 아님. 참고용 알림.")
        send_message("\n".join(lines))
        print("  텔레그램 발송 완료")
    except Exception as exc:
        print(f"  텔레그램 발송 실패: {exc}")


if __name__ == "__main__":
    main()
