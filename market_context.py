#!/usr/bin/env python3
"""시장 컨텍스트 조회 - 네이버 증권 API로 코스피/코스닥 실시간 정보."""

import requests

_NAVER_INDEX_URL = "https://m.stock.naver.com/api/index/{code}/basic"

_TREND_THRESHOLDS = [
    (-5.0, "panic"),
    (-3.0, "correction"),
    (-1.0, "weak"),
    ( 1.0, "stable"),
    ( 3.0, "strong"),
]

_TREND_MSG = {
    "panic":      "패닉 국면 - 신규 매수 전면 보류",
    "correction": "조정 국면 - 매수 신중",
    "weak":       "약세 - 선별 매수",
    "stable":     "안정 - 정상 매매",
    "strong":     "강세 - 적극 매수",
    "bullish":    "급등 국면 - FOMO 주의",
}


def _classify_trend(change_pct: float) -> str:
    for threshold, label in _TREND_THRESHOLDS:
        if change_pct < threshold:
            return label
    return "bullish"


def _fetch_index(code: str) -> dict:
    """네이버 증권 모바일 API로 지수 조회. code: 'KOSPI' | 'KOSDAQ'"""
    try:
        resp = requests.get(
            _NAVER_INDEX_URL.format(code=code),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
            timeout=8,
        )
        resp.raise_for_status()
        data   = resp.json()
        price  = float(str(data.get("closePrice", "0")).replace(",", ""))
        change = float(str(data.get("fluctuationsRatio", "0")).replace(",", "").replace("%", ""))
        return {"price": round(price, 2), "change": round(change, 2)}
    except Exception as exc:
        print(f"  [{code}] 지수 조회 실패: {exc}")
        return {"price": 0.0, "change": 0.0}


def get_market_context() -> dict:
    """코스피/코스닥 실시간 시장 컨텍스트 반환.

    Returns:
        {
          "kospi":  {"price": 2649.78, "change": -0.57, "trend": "weak"},
          "kosdaq": {"price": 850.23,  "change": -0.32, "trend": "stable"},
          "trend_summary": "코스피 -0.57% 약세 - 선별 매수",
        }
    """
    kospi  = _fetch_index("KOSPI")
    kosdaq = _fetch_index("KOSDAQ")

    kospi["trend"]  = _classify_trend(kospi["change"])
    kosdaq["trend"] = _classify_trend(kosdaq["change"])

    trend   = kospi["trend"]
    summary = f"코스피 {kospi['change']:+.1f}% {_TREND_MSG.get(trend, trend)}"

    return {
        "kospi":         kospi,
        "kosdaq":        kosdaq,
        "trend_summary": summary,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(get_market_context(), ensure_ascii=False, indent=2))
