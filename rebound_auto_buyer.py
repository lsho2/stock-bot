#!/usr/bin/env python3
"""V자 반등 자동 매수 시스템.

실행:
    python rebound_auto_buyer.py                # 5분 루프 (데몬 모드)
    python rebound_auto_buyer.py --once         # 1회 실행 후 종료 (작업 스케줄러용)
    python rebound_auto_buyer.py --dry-run      # KIS API·텔레그램 스킵
    python rebound_auto_buyer.py --force-scan   # 캐시 무시 재스캔
    python rebound_auto_buyer.py --quick        # 상위 350종목만 (테스트)
    python rebound_auto_buyer.py --scan-only    # 스캔·캐시만 생성 후 종료 (08:00 스케줄러용)
    python rebound_auto_buyer.py --no-intraday  # 일중 갭다운 필터 비활성화 (캐시만으로 매수)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    import FinanceDataReader as fdr
    import pandas as pd
except ImportError:
    sys.exit("pip install finance-datareader pandas 후 재실행")

from market_context import get_market_context
from stock_info import get_token, is_spac
from telegram_alert import send_message
from tracker import log_buy
from rebound_detector import _calc_rsi, _is_hammer, _volume_ratio, _five_day_decline

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────
KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.getenv("KIS_ACCOUNT", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"   # 실전: 9443
IS_PAPER       = "vts" in KIS_BASE_URL.lower()

POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")
DATA_DIR       = Path(r"C:\Stock\stock-bot\data")
LOG_DIR        = Path(r"C:\Stock\data\logs")
LOCK_FILE      = DATA_DIR / "rebound_auto_buyer.lock"

TODAY      = date.today().isoformat()
SCAN_CACHE = DATA_DIR / f"rebound_scan_{TODAY}.json"
STATE_FILE = DATA_DIR / f"rebound_auto_state_{TODAY}.json"

MARKET_OPEN    = dtime(9, 5)    # 매수 시작 (장 초반 변동 회피)
MARKET_CLOSE   = dtime(15, 20)  # 매수 마감

MAX_BUYS_PER_DAY = 15
MAX_POSITIONS    = 20
SLOT_PCT         = 0.03         # 총자산 대비 슬롯 비율
SLOT_MIN         = 300_000      # 슬롯 최소값 (원)
SLOT_MAX         = 1_000_000    # 슬롯 최대값 (원, 한 종목 100만원 초과 방지)
SLOT_FALLBACK    = 500_000      # 잔고 조회 실패 시 fallback (원)
CAP_MIN_B        = 100          # 시총 하한 (억)
CAP_MAX_B        = 5_000        # 시총 상한 (억)
VOL60_MIN        = 30.0         # 60일 연환산 변동성 하한 (%)
STOP_LOSS_PCT    = 0.025        # 손절 -2.5%
TARGET_PCT       = 0.05         # 익절 트리거 +5%
MIN_AVG_VOLUME   = 10_000       # 일평균 거래량 하한 (주)

KOSPI_N      = 200              # --quick 전용
KOSDAQ_N     = 150              # --quick 전용
API_DELAY    = 0.15
LOCK_TIMEOUT           = 600    # 락 만료 시간 (초, 10분)
REBUY_COOLDOWN_MINUTES = 30     # 매도 후 재매수 쿨다운 (분)

GAP_DOWN_THRESHOLD      = -0.015   # 시가 갭다운 -1.5%
INTRADAY_DROP_THRESHOLD = -0.010   # 일중 추가 하락 -1.0%
RECOVERY_THRESHOLD      =  0.005   # 저점 대비 회복 +0.5%

USE_BEARISH_MARKET_GUARD = True    # 약세장 매수 중단 활성화
BEARISH_MARKET_THRESHOLD = -1.0    # 코스피 등락률 이 이하 시 매수 정지 (%)
PANIC_MARKET_THRESHOLD   = -2.0    # 패닉 임계값 이 이하 시 강제 중단 (%)

SUMMARY_LAST_FILE = DATA_DIR / "rebound_summary_last.txt"
SUMMARY_INTERVAL  = 3600           # 요약 알림 최소 간격 (초, 1시간)

USE_MINUTE_V_PATTERN         = True   # False = 분봉 V자 체크 없이 일중 필터만
USE_INTRADAY_SNAPSHOT_FILTER = False  # True  = 분봉 V자 미통과 시 일중 필터로 fallback
                                      # False = 분봉 V자만 사용 (단타 데이터 정확)
MINUTE_V_MIN_CANDLES   = 30      # 최소 분봉 수
MINUTE_V_DROP_MIN      = -0.010  # 시작점 대비 저점 하락 임계값 (-1%)
MINUTE_V_BULL_MIN      = 2       # 저점 이후 최소 양봉 수
MINUTE_V_VOL_RATIO_MIN = 1.3     # 최근 5분 vs 전체 평균 거래량 비율
MINUTE_V_RECOVERY_MIN  = 0.003   # 저점 대비 회복 최솟값 (+0.3%)
MINUTE_V_RECOVERY_MAX  = 0.020   # 저점 대비 회복 최댓값 (+2.0%)

DRY_RUN = False                 # main()에서 argparse로 덮어씀


# ── 로깅 ──────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"rebound_auto_{datetime.now().strftime('%Y%m%d')}.log"
    logger = logging.getLogger("rebound_auto")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger

log = _setup_logger()


# ── 락 파일 ───────────────────────────────────────────────────────

def _acquire_lock() -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
            age  = time.time() - info.get("ts", 0)
            if age < LOCK_TIMEOUT:
                log.info(f"  락 파일 존재 (age={age:.0f}s) → 이미 실행 중, 종료")
                return False
            log.warning(f"  오래된 락 파일 (age={age:.0f}s) → 강제 해제")
        except Exception:
            pass
    LOCK_FILE.write_text(
        json.dumps({"pid": os.getpid(), "ts": time.time(), "start": datetime.now().isoformat()}),
        encoding="utf-8",
    )
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── 상태 파일 ─────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"buy_count": 0, "bought_codes": []}


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 최근 매수 이력 확인 (다일 간) ─────────────────────────────────

def _has_recent_buy_days(code: str, days: int = 3) -> bool:
    """trades.json에서 최근 N일 내 동일 종목 BUY 기록 확인."""
    trades_path = Path(r"C:\Stock\data\trades.json")
    if not trades_path.exists():
        return False
    try:
        data   = json.loads(trades_path.read_text(encoding="utf-8"))
        cutoff = datetime.now() - timedelta(days=days)
        for t in data.get("trades", []):
            if t.get("code") != code or t.get("type") != "BUY":
                continue
            try:
                if datetime.fromisoformat(t.get("time", "")) >= cutoff:
                    return True
            except ValueError:
                if t.get("date", "") >= cutoff.strftime("%Y-%m-%d"):
                    return True
    except Exception:
        pass
    return False


def _last_sell_elapsed_today(code: str) -> float | None:
    """오늘 해당 종목의 마지막 SELL 이후 경과 분. 오늘 매도 없으면 None."""
    trades_path = Path(r"C:\Stock\data\trades.json")
    if not trades_path.exists():
        return None
    today = date.today().isoformat()
    try:
        data      = json.loads(trades_path.read_text(encoding="utf-8"))
        last_sell: datetime | None = None
        for t in data.get("trades", []):
            if t.get("code") != code or t.get("type") != "SELL":
                continue
            if t.get("date", "") != today:
                continue
            raw = t.get("time") or f"{today}T00:00:00"
            try:
                ts = datetime.fromisoformat(raw)
            except ValueError:
                ts = datetime.strptime(today, "%Y-%m-%d")
            if last_sell is None or ts > last_sell:
                last_sell = ts
        if last_sell is None:
            return None
        return (datetime.now() - last_sell).total_seconds() / 60
    except Exception:
        return None


# ── 신호 감지 (fdr 일봉 로컬) ─────────────────────────────────────

def _df_to_candles(df: pd.DataFrame) -> list[dict]:
    """fdr DataFrame(오래된→최신) → list[dict](최신→오래된)."""
    candles = []
    for ts, row in df.iterrows():
        candles.append({
            "date":   ts.strftime("%Y%m%d") if hasattr(ts, "strftime") else str(ts)[:8],
            "open":   int(float(row.get("Open",   0) or 0)),
            "high":   int(float(row.get("High",   0) or 0)),
            "low":    int(float(row.get("Low",    0) or 0)),
            "close":  int(float(row.get("Close",  0) or 0)),
            "volume": int(float(row.get("Volume", 0) or 0)),
        })
    candles.reverse()
    return candles


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


def _detect_local(df: pd.DataFrame) -> dict:
    """fdr 일봉 DataFrame으로 V자 반등 신호 감지 (4조건, API 불필요).

    Returns:
        {"is_rebound": bool, "confidence": "high"|"medium"|"low"|"none", "signals": list[str]}
    """
    candles = _df_to_candles(df)
    if len(candles) < 6:
        return {"is_rebound": False, "confidence": "none", "signals": []}

    signals: list[str] = []

    rsi = _calc_rsi(candles, period=14)
    if rsi is not None and rsi <= 30:
        signals.append(f"RSI {rsi:.0f}")

    if _is_hammer(candles[0]):
        signals.append("망치형 캔들")

    vol_ratio = _volume_ratio(candles, avg_n=20)
    if vol_ratio >= 1.5:
        signals.append(f"거래량 {vol_ratio:.1f}배")

    decline = _five_day_decline(candles)
    if decline <= -5.0:
        signals.append(f"5일 낙폭 {decline:.1f}%")

    count = len(signals)
    if count >= 4:
        return {"is_rebound": True,  "confidence": "high",   "signals": signals}
    if count >= 3:
        return {"is_rebound": True,  "confidence": "medium", "signals": signals}
    confidence = "low" if count == 2 else "none"
    return     {"is_rebound": False, "confidence": confidence, "signals": signals}


# ── 종목 목록 ─────────────────────────────────────────────────────

def _get_all_stocks(min_cap_b: float = CAP_MIN_B) -> list[dict]:
    """전체 KOSPI + KOSDAQ (시총 min_cap_b억 미만 제외)."""
    results: list[dict] = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            df       = fdr.StockListing(market)
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
            log.info(f"  [{market}] {len(results) - before}종목")
        except Exception as exc:
            log.error(f"  [{market}] 목록 오류: {exc}")
    return results


def _get_stock_list(kospi_n: int = KOSPI_N, kosdaq_n: int = KOSDAQ_N) -> list[dict]:
    """시총 상위 N개 (--quick 모드)."""
    results: list[dict] = []
    for market, n in (("KOSPI", kospi_n), ("KOSDAQ", kosdaq_n)):
        try:
            df       = fdr.StockListing(market)
            cap_col  = next((c for c in df.columns if "cap" in c.lower() or "시가" in c.lower()), None)
            df       = df.nlargest(n, cap_col) if cap_col else df.head(n)
            code_col = next((c for c in df.columns if c.lower() == "code"), df.columns[0])
            name_col = next((c for c in df.columns if c.lower() == "name"), df.columns[1])
            for _, row in df.iterrows():
                code = str(row[code_col]).strip().zfill(6)
                if not code or code == "000000":
                    continue
                cap_b = round(float(row[cap_col]) / 1e8, 1) if cap_col and pd.notna(row.get(cap_col)) else None
                results.append({
                    "code":         code,
                    "name":         str(row.get(name_col, code)).strip(),
                    "market":       market,
                    "market_cap_b": cap_b,
                })
        except Exception as exc:
            log.error(f"  [{market}] 목록 오류: {exc}")
    return results


# ── 스캔 ──────────────────────────────────────────────────────────

def _scan_stocks(stocks: list[dict]) -> list[dict]:
    """V자 반등 신호 스캔. 후보 list[dict] 반환."""
    candidates: list[dict] = []
    fetch_from = (date.today() - timedelta(days=100)).strftime("%Y-%m-%d")
    fetch_to   = TODAY
    width      = len(str(len(stocks)))

    for idx, s in enumerate(stocks, 1):
        code, name = s["code"], s["name"]
        cap_b = s.get("market_cap_b")
        print(f"  [{idx:{width}d}/{len(stocks)}] {code} {name:<18s}", end="", flush=True)

        try:
            df = fdr.DataReader(code, fetch_from, fetch_to)
            if df is None or len(df) < 25:
                print("  데이터 부족")
                time.sleep(API_DELAY)
                continue
            df = df.sort_index()
        except Exception as exc:
            print(f"  오류: {exc}")
            time.sleep(API_DELAY)
            continue

        # 거래량 필터
        avg_vol = df["Volume"].mean()
        if avg_vol < MIN_AVG_VOLUME:
            print(f"  거래량 부족 ({avg_vol:.0f}주)")
            time.sleep(API_DELAY)
            continue

        # 시총 상한 필터
        if cap_b is not None and cap_b > CAP_MAX_B:
            print(f"  시총 초과 ({cap_b:.0f}억)")
            time.sleep(API_DELAY)
            continue

        # 변동성 필터
        vol60 = _calc_vol60(df)
        if vol60 is not None and vol60 < VOL60_MIN:
            print(f"  변동성 부족 ({vol60:.1f}%)")
            time.sleep(API_DELAY)
            continue

        result = _detect_local(df)
        sigs_str = ", ".join(result["signals"][:2]) if result["signals"] else "-"
        print(f"  {result['confidence']:<6s}  {sigs_str}")

        if result["is_rebound"]:
            candidates.append({
                "code":         code,
                "name":         name,
                "market":       s["market"],
                "market_cap_b": cap_b,
                "vol60":        vol60,
                "confidence":   result["confidence"],
                "signals":      result["signals"],
                "last_close":   int(float(df["Close"].iloc[-1])),
                "scanned_at":   datetime.now().isoformat(timespec="seconds"),
            })

        time.sleep(API_DELAY)

    return candidates


# ── KIS API ───────────────────────────────────────────────────────

def _account_parts() -> tuple[str, str]:
    acct = KIS_ACCOUNT.replace("-", "")
    return acct[:8], acct[8:] or "01"


def _kis_headers(tr_id: str, token: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
        "Content-Type":  "application/json",
    }


def _get_total_assets(token: str) -> int:
    """KIS 잔고 조회 → 총평가금액(현금 + 유가증권 평가) 반환. 실패 시 0."""
    cano, prod = _account_parts()
    tr_id = "VTTC8434R" if IS_PAPER else "TTTC8434R"
    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=_kis_headers(tr_id, token),
            params={
                "CANO":                  cano,
                "ACNT_PRDT_CD":          prod,
                "AFHR_FLPR_YN":          "N",
                "OFL_YN":                "",
                "INQR_DVSN":             "02",
                "UNPR_DVSN":             "01",
                "FUND_STTL_ICLD_YN":     "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN":             "01",
                "CTX_AREA_FK100":        "",
                "CTX_AREA_NK100":        "",
            },
            timeout=10,
        )
        resp.raise_for_status()
        summary = resp.json().get("output2", [])
        if summary:
            return int(float(summary[0].get("tot_evlu_amt", "0") or "0"))
        return 0
    except Exception as exc:
        log.warning(f"  총자산 조회 실패: {exc}")
        return 0


def _calc_slot_amount(token: str) -> int:
    """총자산 × SLOT_PCT, SLOT_MIN~SLOT_MAX 범위로 클램핑.

    잔고 조회 실패 시 SLOT_FALLBACK 반환.
    """
    total = _get_total_assets(token)
    if total <= 0:
        log.warning(f"  총자산 조회 실패 → fallback {SLOT_FALLBACK:,}원")
        return SLOT_FALLBACK
    raw  = int(total * SLOT_PCT)
    slot = max(SLOT_MIN, min(SLOT_MAX, raw))
    log.info(
        f"  [자본 조회] 총자산 {total:,}원 → 슬롯당 {slot:,}원 "
        f"(자본의 {SLOT_PCT * 100:.0f}%)"
    )
    return slot


def _get_available_cash(token: str) -> int:
    """주문 가능 현금 조회 (원). 실패 시 0."""
    cano, prod = _account_parts()
    tr_id = "VTTC8908R" if IS_PAPER else "TTTC8908R"
    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            headers=_kis_headers(tr_id, token),
            params={
                "CANO":                  cano,
                "ACNT_PRDT_CD":          prod,
                "PDNO":                  "005930",   # 플레이스홀더
                "ORD_UNPR":              "0",
                "ORD_DVSN":              "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN":          "N",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return int(resp.json().get("output", {}).get("nrcvb_buy_amt", "0") or "0")
    except Exception as exc:
        log.warning(f"  현금 조회 실패: {exc}")
        return 0


def _get_current_price(token: str, code: str) -> int:
    """현재가 조회. 실패 시 0."""
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
        log.warning(f"  [{code}] 현재가 조회 실패: {exc}")
        return 0


def _check_intraday_pattern(code: str, token: str) -> tuple[bool, str]:
    """KIS 현재가 API로 일중 갭다운+반등 패턴 확인.

    Returns:
        (True,  "갭-2.0%, 일중-1.3%, 회복+0.7%")  → 매수 조건 충족
        (False, 이유문자열)                          → 미충족 또는 오류
    """
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
        out = resp.json()["output"]
    except Exception as exc:
        return False, f"조회 실패: {exc}"

    try:
        stck_prpr = float(out["stck_prpr"])   # 현재가
        stck_oprc = float(out["stck_oprc"])   # 시가
        stck_lwpr = float(out["stck_lwpr"])   # 일중 저가
        prdy_vrss = float(out["prdy_vrss"])   # 전일 대비
        prdy_clpr = stck_prpr - prdy_vrss     # 전일 종가 계산
    except (KeyError, ValueError) as exc:
        return False, f"필드 파싱 오류: {exc}"

    if prdy_clpr <= 0 or stck_oprc <= 0 or stck_lwpr <= 0:
        return False, "가격 데이터 이상"

    gap_down      = (stck_oprc - prdy_clpr) / prdy_clpr
    intraday_drop = (stck_lwpr - stck_oprc) / stck_oprc
    recovery      = (stck_prpr - stck_lwpr) / stck_lwpr

    detail = (
        f"갭{gap_down * 100:+.1f}%, "
        f"일중{intraday_drop * 100:+.1f}%, "
        f"회복{recovery * 100:+.1f}%"
    )

    if gap_down > GAP_DOWN_THRESHOLD:
        return False, f"갭다운 미충족 ({gap_down * 100:+.1f}%)"
    if intraday_drop > INTRADAY_DROP_THRESHOLD:
        return False, f"일중 하락 미충족 ({intraday_drop * 100:+.1f}%)"
    if recovery < RECOVERY_THRESHOLD:
        return False, f"회복 미충족 ({recovery * 100:+.1f}%)"

    return True, detail


def _check_minute_v_pattern(code: str, token: str, name: str = "") -> tuple[bool, dict]:
    """분봉 V자 패턴 인식 (KIS 1분봉 기준).

    조건 (모두 충족):
    1. 분봉 30개 확보
    2. 최저점이 시작점 대비 -1% 이상 하락
    3. 최저점 이후 양봉 2개 이상
    4. 최근 5분봉 평균 거래량 > 전체 평균 × 1.3
    5. 현재가가 최저점 대비 +0.3% ~ +2.0%
    6. 최저점이 마지막 분봉이 아닐 것

    반환: (passed, info)  info 키: bottom_mins_ago, drop_pct, recovery_pct, bull_after, vol_ratio, reason
    """
    info: dict = {
        "bottom_mins_ago": 0,
        "drop_pct":        0.0,
        "recovery_pct":    0.0,
        "bull_after":      0,
        "vol_ratio":       0.0,
        "reason":          "",
    }

    now      = datetime.now()
    hour_str = now.strftime("%H%M%S")

    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        KIS_APP_KEY,
                "appsecret":     KIS_APP_SECRET,
                "tr_id":         "FHKST03010200",
                "custtype":      "P",
            },
            params={
                "FID_ETC_CLS_CODE":       "",
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD":         code,
                "FID_INPUT_HOUR_1":       hour_str,
                "FID_PW_DATA_INCU_YN":    "N",
            },
            timeout=10,
        )
        resp.raise_for_status()
        raw_candles = resp.json().get("output2", [])
    except Exception as exc:
        info["reason"] = f"API 오류: {exc}"
        return False, info

    # 오늘 날짜 분봉만 필터링 + 시간 오름차순 정렬
    today_str = now.strftime("%Y%m%d")
    candles: list[dict] = []
    for c in raw_candles:
        if c.get("stck_bsop_date", "") != today_str:
            continue
        t_str = c.get("stck_cntg_hour", "")
        if len(t_str) < 4:
            continue
        try:
            candles.append({
                "time":   int(t_str[:6]) if len(t_str) >= 6 else int(t_str[:4]) * 100,
                "open":   int(c.get("stck_oprc", "0") or "0"),
                "high":   int(c.get("stck_hgpr", "0") or "0"),
                "low":    int(c.get("stck_lwpr", "0") or "0"),
                "close":  int(c.get("stck_prpr", "0") or "0"),
                "volume": int(c.get("cntg_vol",  "0") or "0"),
            })
        except (ValueError, TypeError):
            continue

    candles.sort(key=lambda x: x["time"])
    candles = candles[-MINUTE_V_MIN_CANDLES:]  # 최근 30개

    # 조건 1: 30개 확보
    if len(candles) < MINUTE_V_MIN_CANDLES:
        info["reason"] = f"분봉 {len(candles)}개 (30개 미만)"
        return False, info

    start_price = candles[0]["open"]
    if start_price <= 0:
        info["reason"] = "시작가 0"
        return False, info

    # 최저점 (저가 기준)
    bottom_idx   = min(range(len(candles)), key=lambda i: candles[i]["low"])
    bottom_price = candles[bottom_idx]["low"]

    # 조건 6: 최저점이 마지막 분봉이 아닐 것
    if bottom_idx == len(candles) - 1:
        info["reason"] = "최저점이 마지막 분봉 (반등 미확인)"
        return False, info

    # 조건 2: 시작점 대비 하락률
    drop_pct      = (bottom_price - start_price) / start_price
    info["drop_pct"] = round(drop_pct * 100, 2)
    if drop_pct > MINUTE_V_DROP_MIN:
        info["reason"] = f"하락률 미충족 ({drop_pct * 100:+.1f}%)"
        return False, info

    # 조건 3: 최저점 이후 양봉 수
    bull_after      = sum(1 for c in candles[bottom_idx + 1:] if c["close"] > c["open"])
    info["bull_after"] = bull_after
    if bull_after < MINUTE_V_BULL_MIN:
        info["reason"] = f"양봉 부족 ({bull_after}개)"
        return False, info

    # 조건 4: 최근 5분봉 거래량 비율
    avg_recent = sum(c["volume"] for c in candles[-5:]) / 5
    avg_all    = sum(c["volume"] for c in candles) / len(candles)
    vol_ratio  = avg_recent / avg_all if avg_all > 0 else 0.0
    info["vol_ratio"] = round(vol_ratio, 2)
    if vol_ratio < MINUTE_V_VOL_RATIO_MIN:
        info["reason"] = f"거래량 부족 ({vol_ratio:.2f}x)"
        return False, info

    # 조건 5: 저점 대비 현재가 회복률
    cur_price    = candles[-1]["close"]
    if cur_price <= 0 or bottom_price <= 0:
        info["reason"] = "가격 데이터 이상"
        return False, info
    recovery_pct      = (cur_price - bottom_price) / bottom_price
    info["recovery_pct"] = round(recovery_pct * 100, 2)
    if recovery_pct < MINUTE_V_RECOVERY_MIN:
        info["reason"] = f"회복률 미충족 ({recovery_pct * 100:+.1f}%)"
        return False, info
    if recovery_pct > MINUTE_V_RECOVERY_MAX:
        info["reason"] = f"회복 과도 ({recovery_pct * 100:+.1f}%) — 고점 매수 위험"
        return False, info

    info["bottom_mins_ago"] = len(candles) - 1 - bottom_idx
    return True, info


def _market_buy(token: str, code: str, qty: int) -> str | None:
    """시장가 매수. 성공 시 주문번호 반환. 실패 시 None (최대 3회 재시도)."""
    cano, prod = _account_parts()
    tr_id = "VTTC0802U" if IS_PAPER else "TTTC0802U"
    body = {
        "CANO":         cano,
        "ACNT_PRDT_CD": prod,
        "PDNO":         code,
        "ORD_DVSN":     "01",
        "ORD_QTY":      str(qty),
        "ORD_UNPR":     "0",
    }
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
                headers=_kis_headers(tr_id, token),
                json=body,
                timeout=10,
            )
            resp.raise_for_status()
            data  = resp.json()
            rt_cd = data.get("rt_cd", "")
            if rt_cd == "0":
                return data.get("output", {}).get("ODNO", "")
            msg = data.get("msg1", "")
            log.warning(f"  [{code}] 매수 실패 rt_cd={rt_cd} msg={msg} [{attempt}/3]")
        except Exception as exc:
            log.warning(f"  [{code}] 매수 API 오류 [{attempt}/3]: {exc}")
        if attempt < 3:
            time.sleep(1)
    return None


# ── positions.json 업데이트 ──────────────────────────────────────

def _add_position(
    code: str, name: str, buy_price: int, qty: int,
    signals: list[str], vol60: float | None,
    cap_b: float | None, order_no: str,
) -> None:
    """positions.json에 신규 포지션 추가. monitor.py가 이후 손절/익절 관리."""
    try:
        data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {"positions": []}

    for pos in data.get("positions", []):
        if pos["code"] == code and pos.get("status") in ("OPEN", "HOLDING"):
            log.warning(f"  [{code}] 이미 활성 포지션 → positions.json 미추가")
            return

    entry: dict = {
        "code":            code,
        "name":            name,
        "buy_price":       buy_price,
        "qty":             qty,
        "stop_loss":       round(buy_price * (1 - STOP_LOSS_PCT)),
        "target_price":    round(buy_price * (1 + TARGET_PCT)),
        "status":          "OPEN",
        "trailing_active": False,
        "peak_price":      0,
        "buy_order_no":    order_no,
        "buy_time":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "created_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "buy_source":      "rebound_auto",
        "buy_signals":     signals,
    }
    if vol60 is not None:
        entry["vol60"] = vol60
    if cap_b is not None:
        entry["market_cap_b"] = cap_b

    data.setdefault("positions", []).append(entry)
    POSITIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"  [{name}({code})] positions.json 추가: {buy_price:,}원 × {qty}주")


# ── 요약 알림 빈도 제한 ───────────────────────────────────────────

def _should_send_summary() -> bool:
    """마지막 요약 알림으로부터 SUMMARY_INTERVAL 초 이상 경과했는지 확인."""
    if not SUMMARY_LAST_FILE.exists():
        return True
    try:
        last = datetime.fromisoformat(SUMMARY_LAST_FILE.read_text(encoding="utf-8").strip())
        return (datetime.now() - last).total_seconds() >= SUMMARY_INTERVAL
    except Exception:
        return True


def _update_summary_ts() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_LAST_FILE.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")


# ── 약세장 가드 ──────────────────────────────────────────────────

def _check_bearish_market() -> tuple[bool, str]:
    """코스피 약세 여부 확인. True면 매수 중단."""
    if not USE_BEARISH_MARKET_GUARD:
        return False, "guard disabled"
    try:
        mkt = get_market_context()
        kospi_change = mkt["kospi"]["change"]
        trend = mkt["kospi"]["trend"]

        if kospi_change <= PANIC_MARKET_THRESHOLD:
            return True, f"패닉 (코스피 {kospi_change:+.2f}%)"
        if kospi_change <= BEARISH_MARKET_THRESHOLD:
            return True, f"약세 (코스피 {kospi_change:+.2f}%)"
        if trend == "panic":
            return True, f"패닉 트렌드 (코스피 {kospi_change:+.2f}%)"
        return False, f"정상 (코스피 {kospi_change:+.2f}%)"
    except Exception as exc:
        log.warning(f"  [시장체크] 실패: {exc} → 매수 진행 (fail-open)")
        return False, f"체크 실패: {exc}"


# ── 메인 흐름 ─────────────────────────────────────────────────────

def _run(args: argparse.Namespace) -> None:
    state = _load_state()

    # 사이클 통계 (요약 알림용)
    pass_stats      = {"갭다운 부족": 0, "일중 하락 부족": 0, "반등 부족": 0, "조회 실패/기타": 0}
    capital_skip    = 0
    intraday_passed = 0
    minute_v_count  = 0
    buys_this_cycle = 0

    try:
        pos_data_early = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        active_early   = sum(
            1 for p in pos_data_early.get("positions", [])
            if p.get("status") in ("OPEN", "HOLDING")
        )
    except Exception:
        active_early = 0
    log.info(
        f"  [매수 한도] 하루 최대 {MAX_BUYS_PER_DAY}건 / "
        f"동시 보유 {MAX_POSITIONS}건 (현재 {active_early}건)"
    )
    log.info(f"  오늘 매수: {state['buy_count']}건 / 최대 {MAX_BUYS_PER_DAY}건")

    if state["buy_count"] >= MAX_BUYS_PER_DAY and not args.scan_only:
        log.info(f"  오늘 최대 매수 건수({MAX_BUYS_PER_DAY}) 도달 → 종료")
        return

    # ── 스캔 캐시 ────────────────────────────────────────────────
    candidates: list[dict] = []

    if not args.force_scan and SCAN_CACHE.exists():
        try:
            cached     = json.loads(SCAN_CACHE.read_text(encoding="utf-8"))
            candidates = cached.get("candidates", [])
            log.info(f"  스캔 캐시 로드: {len(candidates)}개 후보 ({cached.get('scanned_at', '?')})")
        except Exception as exc:
            log.warning(f"  캐시 로드 실패: {exc} → 재스캔")
            candidates = []

    if not candidates:
        log.info("  전체 종목 스캔 시작...")
        if args.quick:
            stocks = _get_stock_list()
            log.info(f"  대상: {len(stocks)}종목 (--quick)")
        else:
            stocks = _get_all_stocks()
            log.info(f"  대상: {len(stocks)}종목")

        spac_n = sum(1 for s in stocks if is_spac(s.get("name", "")))
        stocks = [s for s in stocks if not is_spac(s.get("name", ""))]
        if spac_n:
            log.info(f"  스팩 제외: {spac_n}개 → {len(stocks)}종목")

        candidates = _scan_stocks(stocks)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SCAN_CACHE.write_text(
            json.dumps({
                "scanned_at":    datetime.now().isoformat(timespec="seconds"),
                "total_scanned": len(stocks),
                "candidates":    candidates,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(f"  스캔 완료: {len(candidates)}개 후보 캐시 저장")

    if args.scan_only:
        log.info("  [--scan-only] 캐시 생성 완료 → 매수 스킵, 종료")
        return

    # ── 장중 여부 확인 ────────────────────────────────────────────
    now = datetime.now()
    if now.weekday() >= 5:
        log.info("  주말 → 매수 스킵")
        return
    now_time = now.time()
    if now_time < dtime(9, 30):
        log.info(f"  장 시작 전 ({now_time.strftime('%H:%M')}) → 매수 스킵")
        return
    if now_time > MARKET_CLOSE:
        log.info(f"  장 마감 임박 ({now_time.strftime('%H:%M')}) → 매수 스킵")
        return

    # ── 약세장 매수 중단 ─────────────────────────────────────────
    blocked, reason = _check_bearish_market()
    if blocked:
        log.info(f"  ⚠️ 약세장 매수 중단: {reason}")
        try:
            if _should_send_summary():
                send_message(
                    f"🚨 약세장 매수 중단\n"
                    f"사유: {reason}\n"
                    f"매수 스킵 — 다음 사이클(5분 후) 재확인"
                )
                _update_summary_ts()
        except Exception as exc:
            log.debug(f"  텔레그램 발송 실패: {exc}")
        return
    log.info(f"  [시장체크] {reason} → 매수 진행")

    # ── 포지션 한도 확인 ─────────────────────────────────────────
    try:
        pos_data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        active = {p["code"] for p in pos_data.get("positions", []) if p.get("status") in ("OPEN", "HOLDING")}
    except Exception:
        active = set()
    if len(active) >= MAX_POSITIONS:
        log.info(f"  현재 보유 {len(active)}개 (한도 {MAX_POSITIONS}) → 매수 패스")
        return

    valid = [c for c in candidates if c.get("confidence") == "medium"]
    log.info(f"  매수 후보: {len(valid)}개 (medium, 전체 {len(candidates)}개)")

    if not valid:
        log.info("  매수 후보 없음 → 종료")
        return

    # ── KIS 토큰 / 현금 / 슬롯 금액 ─────────────────────────────
    if DRY_RUN:
        token       = ""
        cash        = 999_999_999
        slot_amount = SLOT_FALLBACK
        log.info(f"  [DRY-RUN] 토큰·현금 조회 스킵 → 슬롯 {slot_amount:,}원 (fallback)")
    else:
        try:
            token = get_token()
        except Exception as exc:
            log.error(f"  토큰 획득 실패: {exc}")
            return
        slot_amount = _calc_slot_amount(token)
        cash        = _get_available_cash(token)
        log.info(f"  주문 가능 현금: {cash:,}원")

    if cash < slot_amount * 0.9:
        log.info(f"  현금 부족 ({cash:,}원) → 매수 스킵")
        return

    # ── 후보별 매수 시도 ─────────────────────────────────────────
    for c in valid:
        if state["buy_count"] >= MAX_BUYS_PER_DAY:
            break
        if len(active) >= MAX_POSITIONS:
            log.info(f"  포지션 한도 {MAX_POSITIONS} 도달 → 매수 중단")
            break

        code = c["code"]
        name = c["name"]
        cap_b = c.get("market_cap_b")
        vol60 = c.get("vol60")
        sigs  = c.get("signals", [])
        conf  = c.get("confidence", "?")
        intraday_detail = ""
        minute_v_passed = False
        mv_info: dict   = {}

        # ── 재매수 쿨다운 / 기존 매수 이력 체크 ────────────────────
        sell_elapsed = _last_sell_elapsed_today(code)
        if sell_elapsed is not None:
            if sell_elapsed < REBUY_COOLDOWN_MINUTES:
                wait = int(REBUY_COOLDOWN_MINUTES - sell_elapsed)
                log.info(
                    f"  [재매수 쿨다운] {name}({code}) "
                    f"마지막 매도 {int(sell_elapsed)}분 전, {wait}분 후 가능"
                )
                continue
            log.info(f"  [재매수 OK] {name}({code}) 쿨다운 경과 ({int(sell_elapsed)}분)")
        else:
            if code in state["bought_codes"]:
                log.info(f"  [{name}({code})] 오늘 이미 매수 → 스킵")
                continue
            if _has_recent_buy_days(code, days=3):
                log.info(f"  [{name}({code})] 최근 3일 매수 이력 → 스킵")
                continue

        if code in active:
            log.info(f"  [{name}({code})] 활성 포지션 존재 → 스킵")
            continue

        # ── 진입 패턴 확인 ───────────────────────────────────────────
        if USE_MINUTE_V_PATTERN and not DRY_RUN:
            ok, mv_info = _check_minute_v_pattern(code, token, name)
            if ok:
                minute_v_passed = True
                minute_v_count += 1
                log.info(
                    f"  [분봉 V자] {name}({code}) "
                    f"저점 {mv_info['bottom_mins_ago']}분전 {mv_info['drop_pct']:+.1f}%, "
                    f"반등 {mv_info['recovery_pct']:+.1f}%, "
                    f"거래량 {mv_info['vol_ratio']:.1f}x → 매수"
                )
            else:
                if USE_INTRADAY_SNAPSHOT_FILTER:
                    log.info(
                        f"  [분봉 V자] {name}({code}) "
                        f"조건 미충족: {mv_info['reason']} → 일중 필터로"
                    )
                else:
                    log.info(
                        f"  [분봉 V자] {name}({code}) "
                        f"조건 미충족: {mv_info['reason']} → 다음 종목 (일중 필터 비활성)"
                    )
                    continue

        if not minute_v_passed:
            if not args.no_intraday and not DRY_RUN:
                ok, detail = _check_intraday_pattern(code, token)
                log.info(f"  [일중 필터] {name}({code}) {detail} → {'매수' if ok else '패스'}")
                if not ok:
                    if "갭다운" in detail:
                        pass_stats["갭다운 부족"] += 1
                    elif "일중 하락" in detail:
                        pass_stats["일중 하락 부족"] += 1
                    elif "회복" in detail:
                        pass_stats["반등 부족"] += 1
                    else:
                        pass_stats["조회 실패/기타"] += 1
                    continue
                intraday_passed += 1
                intraday_detail = detail

        log.info(f"  [{name}({code})] 매수 시도 | {conf} | {', '.join(sigs)}")

        if DRY_RUN:
            buy_price = c.get("last_close", 10_000)
            qty       = slot_amount // buy_price if buy_price > 0 else 0
            order_no  = "DRY-RUN"
            log.info(f"  [DRY-RUN] 시장가 매수 {qty}주 @{buy_price:,} (슬롯 {slot_amount:,}원)")
        else:
            buy_price = _get_current_price(token, code)
            if buy_price <= 0:
                log.warning(f"  [{code}] 현재가 조회 실패 → 스킵")
                continue

            qty = slot_amount // buy_price
            if qty <= 0:
                log.info(f"  [{name}({code})] 수량 0주 (가격 {buy_price:,}) → 스킵")
                capital_skip += 1
                continue

            if cash < buy_price * qty:
                log.info(f"  [{name}({code})] 현금 부족 → 스킵")
                capital_skip += 1
                continue

            order_no = _market_buy(token, code, qty)
            if order_no is None:
                log.error(f"  [{name}({code})] 매수 실패 → 스킵")
                try:
                    send_message(f"⚠️ [V자 매수 실패] {name}({code})\n사유: KIS API 주문 실패")
                except Exception:
                    pass
                continue

            cash -= buy_price * qty

        log.info(f"  [BUY] {name}({code}) {qty}주 @{buy_price:,} 주문번호:{order_no}")

        if not DRY_RUN:
            _add_position(code, name, buy_price, qty, sigs, vol60, cap_b, order_no)
            signal = "minute_v_pattern" if minute_v_passed else "intraday_snapshot"
            log_buy(code, name, buy_price, qty, source="rebound_auto", entry_signal=signal)
        else:
            log.info(f"  [DRY-RUN] positions.json / trades.json 추가 스킵: {code}")

        state["buy_count"] += 1
        state["bought_codes"].append(code)
        if not DRY_RUN:
            _save_state(state)
        else:
            log.info(f"  [DRY-RUN] 상태 파일 저장 스킵: {code}")
        active.add(code)

        if minute_v_passed:
            entry_line = (
                f"\n분봉 V자: 저점 {mv_info['bottom_mins_ago']}분전 "
                f"{mv_info['drop_pct']:+.1f}%, 반등 {mv_info['recovery_pct']:+.1f}%"
            )
        else:
            entry_line = f"\n일중: {intraday_detail}" if intraday_detail else ""
        msg = (
            f"🛒 [V자 매수] {name}({code}) {qty}주 @ {buy_price:,}원\n"
            f"일봉: 후보군 confidence={conf}{entry_line}\n"
            f"손절 {round(buy_price*(1-STOP_LOSS_PCT)):,} / 목표 {round(buy_price*(1+TARGET_PCT)):,}"
        )
        buys_this_cycle += 1
        if DRY_RUN:
            log.info(f"  [DRY-RUN] 텔레그램: {msg}")
        else:
            try:
                send_message(msg)
            except Exception as exc:
                log.warning(f"  텔레그램 발송 실패: {exc}")

        time.sleep(0.5)

    log.info(f"  완료: 오늘 총 매수 {state['buy_count']}건")

    # ── 사이클 요약 알림 (매수 0건일 때, 1시간 1회) ──────────────────
    if not DRY_RUN and buys_this_cycle == 0 and _should_send_summary():
        now_str     = datetime.now().strftime("%H:%M")
        stats_parts = [f"  {k}: {v}개" for k, v in pass_stats.items() if v > 0]
        if capital_skip > 0:
            stats_parts.append(f"  자본/한도 도달: {capital_skip}개")
        summary = (
            f"📊 [V자 자동매수] {now_str}\n"
            f"후보: {len(valid)}개 / 분봉V자: {minute_v_count}개 / 일중 통과: {intraday_passed}개 / 매수: 0건"
        )
        if stats_parts:
            summary += "\n패스 사유 통계:\n" + "\n".join(stats_parts)
        try:
            send_message(summary)
            _update_summary_ts()
        except Exception as exc:
            log.warning(f"  요약 알림 발송 실패: {exc}")


def main() -> None:
    global DRY_RUN

    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",    action="store_true", help="KIS API·텔레그램 스킵")
    parser.add_argument("--force-scan", action="store_true", help="캐시 무시 재스캔")
    parser.add_argument("--quick",      action="store_true", help=f"상위 {KOSPI_N+KOSDAQ_N}종목만")
    parser.add_argument("--scan-only",  action="store_true", help="스캔·캐시만 생성 후 종료 (매수 X)")
    parser.add_argument("--once",        action="store_true", help="1회 실행 후 종료 (5분 루프 없이)")
    parser.add_argument("--no-intraday", action="store_true", help="일중 갭다운 필터 비활성화 (캐시만으로 매수)")
    args = parser.parse_args()

    DRY_RUN  = args.dry_run
    SEP      = "=" * 60
    mode_tag = "[DRY-RUN]" if DRY_RUN else "[SCAN-ONLY]" if args.scan_only else ""
    cycle    = 0

    def _cycle_start() -> None:
        nonlocal cycle
        cycle += 1
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag = f"  {mode_tag}" if mode_tag else ""
        log.info(SEP)
        log.info(f"[{ts}] V자 자동매수 사이클 #{cycle} 시작{tag}")
        log.info(SEP)

    def _cycle_end(daemon: bool) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(SEP)
        if daemon:
            next_ts = (datetime.now() + timedelta(seconds=300)).strftime("%H:%M:%S")
            log.info(f"[{ts}] 사이클 #{cycle} 종료 → 5분 대기 (다음 시작: {next_ts})")
        else:
            log.info(f"[{ts}] 사이클 #{cycle} 종료")
        log.info(SEP)

    if args.once:
        if not _acquire_lock():
            sys.exit(0)
        _cycle_start()
        try:
            _run(args)
        finally:
            _release_lock()
        _cycle_end(daemon=False)
        return

    # 5분 루프 (데몬 모드)
    while True:
        if _acquire_lock():
            _cycle_start()
            try:
                _run(args)
            finally:
                _release_lock()
            _cycle_end(daemon=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
