#!/usr/bin/env python3
"""V자 반등 신호 감지 모듈.

손절선 도달 시 무조건 매도하지 않고, 반등 신호 다수 포착 시 매도 보류.

환경변수:
    REBOUND_DETECTION_ENABLED=false  감지 비활성화 (기본 true)
"""

import os
import logging
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("monitor")

KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"

ENABLED = os.getenv("REBOUND_DETECTION_ENABLED", "true").lower() not in ("false", "0", "no")

_DISABLED = {
    "is_rebound": False,
    "confidence": "none",
    "signals":    [],
    "reason":     "감지 비활성화",
}


# ── KIS API ───────────────────────────────────────────────────────

def _headers(token: str, tr_id: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
    }


def _get_daily_ohlcv(token: str, code: str, n: int = 25) -> list[dict]:
    """최근 n 거래일 일봉 OHLCV (최신→과거 순). 실패 시 빈 리스트."""
    today = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=60)).strftime("%Y%m%d")
    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            headers=_headers(token, "FHKST03010100"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD":         code,
                "FID_INPUT_DATE_1":       start,
                "FID_INPUT_DATE_2":       today,
                "FID_PERIOD_DIV_CODE":    "D",
                "FID_ORG_ADJ_PRC":        "0",
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = []
        for r in resp.json().get("output2", []):
            try:
                result.append({
                    "date":   r.get("stck_bsop_date", ""),
                    "open":   int(r.get("stck_oprc", "0") or "0"),
                    "high":   int(r.get("stck_hgpr", "0") or "0"),
                    "low":    int(r.get("stck_lwpr", "0") or "0"),
                    "close":  int(r.get("stck_clpr", "0") or "0"),
                    "volume": int(r.get("acml_vol",  "0") or "0"),
                })
            except (ValueError, TypeError):
                continue
        result.sort(key=lambda x: x["date"], reverse=True)  # 최신→과거 보장
        return result[:n]
    except Exception as exc:
        log.warning(f"  [{code}] 일봉 조회 실패: {exc}")
        return []


# ── 지표 계산 ─────────────────────────────────────────────────────

def _calc_rsi(candles: list[dict], period: int = 14) -> float | None:
    """RSI(period). candles는 최신→과거 순."""
    if len(candles) < period + 1:
        return None
    closes = [c["close"] for c in reversed(candles[:period + 1])]  # 과거→최신
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1 + avg_gain / avg_loss))


def _is_hammer(c: dict) -> bool:
    """망치형 캔들: 아래꼬리 ≥ 몸통×2, 위꼬리 ≤ 몸통×0.5."""
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body         = abs(cl - o) or 1  # 도지 방지
    lower_shadow = min(o, cl) - l
    upper_shadow = h - max(o, cl)
    return lower_shadow >= body * 2 and upper_shadow <= body * 0.5


def _volume_ratio(candles: list[dict], avg_n: int = 20) -> float:
    """오늘 거래량 / 직전 avg_n일 평균 거래량."""
    if len(candles) < 2:
        return 0.0
    today_vol = candles[0]["volume"]
    past = [c["volume"] for c in candles[1:avg_n + 1] if c["volume"] > 0]
    if not past:
        return 0.0
    return today_vol / (sum(past) / len(past))


def _five_day_decline(candles: list[dict]) -> float:
    """최근 5 거래일 등락률(%). candles[0]=최신."""
    if len(candles) < 6:
        return 0.0
    return (candles[0]["close"] - candles[5]["close"]) / candles[5]["close"] * 100


# ── 메인 함수 ─────────────────────────────────────────────────────

def detect_rebound(code: str, token: str) -> dict:
    """V자 반등 신호 감지.

    Returns:
        {
            "is_rebound": bool,
            "confidence": "high" | "medium" | "low" | "none",
            "signals":    list[str],   e.g. ["RSI 28", "망치형 캔들", "거래량 1.8배"]
            "reason":     str,
        }

    판정 기준:
        4개 충족 → high  (is_rebound=True)
        3개 충족 → medium (is_rebound=True)
        2개 이하 → is_rebound=False
    """
    if not ENABLED:
        return _DISABLED

    candles = _get_daily_ohlcv(token, code, n=25)
    if len(candles) < 6:
        return {"is_rebound": False, "confidence": "none", "signals": [], "reason": "데이터 부족"}

    signals: list[str] = []

    # 조건 1: RSI 14일 ≤ 30 (과매도)
    rsi = _calc_rsi(candles, period=14)
    if rsi is not None and rsi <= 30:
        signals.append(f"RSI {rsi:.0f}")

    # 조건 2: 망치형 캔들 (오늘 일봉)
    if _is_hammer(candles[0]):
        signals.append("망치형 캔들")

    # 조건 3: 거래량 ≥ 20일 평균 × 1.5
    vol_ratio = _volume_ratio(candles, avg_n=20)
    if vol_ratio >= 1.5:
        signals.append(f"거래량 {vol_ratio:.1f}배")

    # 조건 4: 최근 5일 누적 ≤ -5%
    decline = _five_day_decline(candles)
    if decline <= -5.0:
        signals.append(f"5일 낙폭 {decline:.1f}%")

    count = len(signals)
    if count >= 4:
        return {"is_rebound": True,  "confidence": "high",   "signals": signals, "reason": "다중 컨펌 통과 (4/4)"}
    if count == 3:
        return {"is_rebound": True,  "confidence": "medium", "signals": signals, "reason": "다중 컨펌 통과 (3/4)"}
    confidence = "low" if count == 2 else "none"
    return     {"is_rebound": False, "confidence": confidence, "signals": signals, "reason": f"신호 부족 ({count}/4)"}


# ── CLI 테스트 ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from stock_info import get_token

    if len(sys.argv) < 2:
        print("사용법: python rebound_detector.py [종목코드]")
        print("예:     python rebound_detector.py 111770")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    code  = sys.argv[1].strip()
    token = get_token()
    result = detect_rebound(code, token)
    print(f"\n[{code}] V자 반등 감지 결과")
    print(f"  반등 여부: {result['is_rebound']}")
    print(f"  신뢰도  : {result['confidence']}")
    print(f"  신호    : {result['signals']}")
    print(f"  판정    : {result['reason']}")
