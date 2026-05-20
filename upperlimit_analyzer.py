#!/usr/bin/env python3
"""상한가 패턴 분석기.

upperlimit_cases.json → 통계 분석 → upperlimit_patterns.json

실행:
    python upperlimit_analyzer.py
"""

import sys
import json
import statistics
from collections import Counter
from pathlib import Path

DATA_DIR      = Path(r"C:\Stock\stock-bot\data")
CASES_PATH    = DATA_DIR / "upperlimit_cases.json"
PATTERNS_PATH = DATA_DIR / "upperlimit_patterns.json"


# ── 통계 헬퍼 ─────────────────────────────────────────────────────

def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def _quartiles(vals: list[float]) -> dict:
    if not vals:
        return {}
    s = sorted(vals)
    n = len(s)
    return {
        "min":    round(s[0], 2),
        "p25":    round(s[n // 4], 2),
        "median": round(statistics.median(s), 2),
        "p75":    round(s[3 * n // 4], 2),
        "max":    round(s[-1], 2),
        "mean":   round(statistics.mean(s), 2),
    }


def _pct_above(vals: list[float], threshold: float) -> float:
    if not vals:
        return 0.0
    return _pct(sum(1 for v in vals if v >= threshold), len(vals))


def _pct_below(vals: list[float], threshold: float) -> float:
    if not vals:
        return 0.0
    return _pct(sum(1 for v in vals if v <= threshold), len(vals))


# ── 분석 ─────────────────────────────────────────────────────────

def analyze(cases: list[dict]) -> dict:
    n = len(cases)
    if n == 0:
        return {}

    # ── 수집 헬퍼 ────────────────────────────────────────────────
    def _d1(key: str)    -> list[float]: return [c["days"]["D-1"][key] for c in cases if c["days"].get("D-1", {}).get(key) is not None]
    def _d2(key: str)    -> list[float]: return [c["days"]["D-2"][key] for c in cases if c["days"].get("D-2", {}).get(key) is not None]
    def _d3(key: str)    -> list[float]: return [c["days"]["D-3"][key] for c in cases if c["days"].get("D-3", {}).get(key) is not None]
    def _field(key: str) -> list[float]: return [c[key] for c in cases if c.get(key) is not None]

    # ── 1. 시가총액 분포 ──────────────────────────────────────────
    caps = _field("market_cap_b")
    cap_buckets = {
        "micro_under100":  _pct(sum(1 for v in caps if v < 100),  len(caps)),
        "small_100_500":   _pct(sum(1 for v in caps if 100 <= v < 500),  len(caps)),
        "mid_500_3000":    _pct(sum(1 for v in caps if 500 <= v < 3000), len(caps)),
        "large_over3000":  _pct(sum(1 for v in caps if v >= 3000), len(caps)),
    }

    # ── 2. D-1 거래량 비율 ────────────────────────────────────────
    vol_ratios = _d1("vol_ratio")
    vol_stats = _quartiles(vol_ratios)
    vol_stats["pct_above_1.5"] = _pct_above(vol_ratios, 1.5)
    vol_stats["pct_above_2"]   = _pct_above(vol_ratios, 2.0)
    vol_stats["pct_above_3"]   = _pct_above(vol_ratios, 3.0)
    vol_stats["pct_above_5"]   = _pct_above(vol_ratios, 5.0)

    # ── 3. 등락률 패턴 (양봉/음봉) ───────────────────────────────
    def _chg_pct_from_ohlc(c: dict, day: str) -> float | None:
        d = c["days"].get(day, {})
        o, cl = d.get("open", 0), d.get("close", 0)
        return round((cl - o) / o * 100, 2) if o > 0 else None

    d1_body = [v for c in cases if (v := _chg_pct_from_ohlc(c, "D-1")) is not None]
    d2_body = [v for c in cases if (v := _chg_pct_from_ohlc(c, "D-2")) is not None]
    d3_body = [v for c in cases if (v := _chg_pct_from_ohlc(c, "D-3")) is not None]

    price_pattern = {
        "d1_yang_pct":  _pct_above(d1_body, 0),
        "d2_yang_pct":  _pct_above(d2_body, 0),
        "d3_yang_pct":  _pct_above(d3_body, 0),
        "d1_body_q":    _quartiles(d1_body),
        "d2_body_q":    _quartiles(d2_body),
        "d3_body_q":    _quartiles(d3_body),
    }

    # ── 4. RSI 분포 ───────────────────────────────────────────────
    rsi_vals = _d1("rsi14")
    rsi_stats = _quartiles(rsi_vals)
    rsi_stats["pct_below30"]  = _pct_below(rsi_vals, 30)
    rsi_stats["pct_30_50"]    = _pct(sum(1 for v in rsi_vals if 30 <= v < 50), len(rsi_vals))
    rsi_stats["pct_50_70"]    = _pct(sum(1 for v in rsi_vals if 50 <= v < 70), len(rsi_vals))
    rsi_stats["pct_above70"]  = _pct_above(rsi_vals, 70)

    # ── 5. MA 위치 ────────────────────────────────────────────────
    ma5_above  = sum(1 for c in cases
                     if c["days"].get("D-1", {}).get("close", 0) >
                        (c["days"]["D-1"].get("ma5")  or 0))
    ma20_above = sum(1 for c in cases
                     if c["days"].get("D-1", {}).get("close", 0) >
                        (c["days"]["D-1"].get("ma20") or 0))
    ma_position = {
        "pct_close_above_ma5":  _pct(ma5_above,  n),
        "pct_close_above_ma20": _pct(ma20_above, n),
    }

    # ── 6. 거래량 증가 추세 (D-3 < D-2 < D-1) ────────────────────
    vol_trend_up = sum(
        1 for c in cases
        if (c["days"].get("D-3", {}).get("volume", 0) <
            c["days"].get("D-2", {}).get("volume", 0) <
            c["days"].get("D-1", {}).get("volume", 0))
    )

    # ── 7. 섹터 분포 ─────────────────────────────────────────────
    sector_cnt: Counter = Counter(c.get("sector", "미분류") or "미분류" for c in cases)
    top_sectors = dict(sector_cnt.most_common(10))

    # ── 8. 시장별 분포 ───────────────────────────────────────────
    market_dist = {
        "KOSPI":  _pct(sum(1 for c in cases if c.get("market") == "KOSPI"),  n),
        "KOSDAQ": _pct(sum(1 for c in cases if c.get("market") == "KOSDAQ"), n),
    }

    # ── 9. 스코어링 가중치 (예측기에서 사용) ─────────────────────
    # 각 조건의 "상한가 종목 중 해당 비율" 기반 가중치 설계
    scoring_weights = {
        # 거래량 급증 (가장 강한 신호)
        "vol_ratio_above_2":      25 if vol_stats.get("pct_above_2", 0) >= 50 else 20,
        "vol_ratio_above_1.5":    15,
        # 거래량 증가 추세
        "vol_trend_increasing":   _pct(vol_trend_up, n) // 5,   # 비율 → 점수 변환
        # 양봉
        "d1_yang_bong":           10 if price_pattern["d1_yang_pct"] >= 60 else 5,
        "d2_yang_bong":           5,
        # 시가총액 (소형주 유리)
        "market_cap_under_500b":  15 if cap_buckets.get("small_100_500", 0) + cap_buckets.get("micro_under100", 0) >= 40 else 10,
        # RSI 범위
        "rsi_40_to_75":           15 if rsi_stats.get("pct_50_70", 0) >= 30 else 10,
        # MA 위치
        "above_ma5":              10,
        "above_ma20":             10,
    }
    # 합계를 100점 만점으로 정규화
    weight_sum = sum(scoring_weights.values())
    if weight_sum > 0:
        scoring_weights = {k: round(v / weight_sum * 100) for k, v in scoring_weights.items()}

    return {
        "sample_size":    n,
        "market_cap":     {"distribution": cap_buckets, "stats_b": _quartiles(caps)},
        "volume":         {"d1_ratio": vol_stats},
        "price_pattern":  price_pattern,
        "rsi":            rsi_stats,
        "ma_position":    ma_position,
        "vol_trend_up_pct": _pct(vol_trend_up, n),
        "sector_top10":   top_sectors,
        "market_dist":    market_dist,
        "scoring_weights": scoring_weights,
    }


# ── 출력 ─────────────────────────────────────────────────────────

def _print_report(p: dict) -> None:
    n = p["sample_size"]
    sep = "─" * 44
    print(f"\n{'='*44}")
    print(f"  상한가 패턴 분석 결과  (N={n}건)")
    print(f"{'='*44}")

    print(f"\n[시가총액 분포]")
    for k, v in p["market_cap"]["distribution"].items():
        print(f"  {k:<22s}: {v}%")

    print(f"\n[D-1 거래량 비율]")
    vd = p["volume"]["d1_ratio"]
    print(f"  중앙값:    {vd.get('median', '-')}배")
    print(f"  1.5배 이상: {vd.get('pct_above_1.5', 0)}%")
    print(f"  2배 이상:  {vd.get('pct_above_2', 0)}%")
    print(f"  3배 이상:  {vd.get('pct_above_3', 0)}%")

    print(f"\n[캔들 패턴]")
    pp = p["price_pattern"]
    print(f"  D-1 양봉: {pp['d1_yang_pct']}%  D-2: {pp['d2_yang_pct']}%  D-3: {pp['d3_yang_pct']}%")

    print(f"\n[RSI 분포]")
    r = p["rsi"]
    print(f"  중앙값: {r.get('median', '-')}  (30↓: {r.get('pct_below30',0)}% / 30-50: {r.get('pct_30_50',0)}% / 50-70: {r.get('pct_50_70',0)}% / 70↑: {r.get('pct_above70',0)}%)")

    print(f"\n[MA 위치]")
    ma = p["ma_position"]
    print(f"  MA5 위: {ma['pct_close_above_ma5']}%  MA20 위: {ma['pct_close_above_ma20']}%")

    print(f"\n[기타]")
    print(f"  거래량 증가 추세 (D3<D2<D1): {p['vol_trend_up_pct']}%")
    print(f"  KOSPI {p['market_dist']['KOSPI']}% / KOSDAQ {p['market_dist']['KOSDAQ']}%")

    print(f"\n[섹터 상위 10]")
    for sector, cnt in list(p["sector_top10"].items())[:10]:
        print(f"  {sector:<20s}: {cnt}건")

    print(f"\n[스코어링 가중치 (합계 ≈100)]")
    for k, v in sorted(p["scoring_weights"].items(), key=lambda x: -x[1]):
        print(f"  {k:<28s}: {v}점")
    print(sep)


# ── 진입점 ────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    if not CASES_PATH.exists():
        sys.exit(f"cases 파일 없음: {CASES_PATH}\n먼저 upperlimit_collector.py 실행")

    data   = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases  = data.get("cases", [])
    print(f"  상한가 사례 로드: {len(cases)}건 (수집일: {data.get('collected_at', '?')})")

    if not cases:
        sys.exit("  분석할 사례 없음")

    patterns = analyze(cases)
    _print_report(patterns)

    from datetime import date
    out = {
        "analyzed_at":  date.today().isoformat(),
        "source":       str(CASES_PATH),
        "patterns":     patterns,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PATTERNS_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  패턴 저장: {PATTERNS_PATH}")


if __name__ == "__main__":
    main()
