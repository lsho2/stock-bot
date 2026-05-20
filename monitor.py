#!/usr/bin/env python3
"""보유 종목 자동 손절/익절/트레일링 스탑 모니터."""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import requests
from dotenv import load_dotenv

from stock_info import get_token
from telegram_alert import send_message
from tracker import log_buy, log_sell, get_open_buy_qty
from market_context import get_market_context
from rebound_detector import detect_rebound

load_dotenv()

KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.getenv("KIS_ACCOUNT", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"  # 모의투자 고정 (실전: 9443)
IS_PAPER       = "vts" in KIS_BASE_URL.lower()   # True=모의, False=실거래 → TR_ID 자동 선택
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1"))

POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")
LOG_DIR        = Path(r"C:\Stock\data\logs")
DATA_DIR       = Path(r"C:\Stock\data")

MARKET_OPEN    = dtime(9, 0)
MARKET_CLOSE   = dtime(15, 35)
EOD_LIQUIDATION_TIME = dtime(15, 15)   # 당일 청산 트리거 시각
USE_EOD_LIQUIDATION  = True            # False면 기존 동작 (스윙 모드)
MAX_ERRORS     = 5
TRAILING_DROP             = 0.015  # 트레일링 스탑: peak 대비 -1.5%
QUICK_TAKE_PROFIT         = 0.07   # 즉시 익절: 매수가 대비 +7%
STOP_LOSS_HOLD_ON_VBOUNCE = False  # True=V자 신호 시 손절 보류 / False=즉시 손절 (단타)

ERROR_COOLDOWN_FILE  = DATA_DIR / "monitor_error_cooldown.json"
ERROR_COOLDOWN_SECS  = 300  # 같은 종목 반복 오류 알림 5분 억제
LOCK_FILE            = Path(r"C:\Stock\stock-bot\monitor.lock")
PENDING_FILL_GRACE   = 300  # 매수 직후 체결 대기 허용 시간 (초, 5분)


# ── 로깅 ─────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"monitor_{datetime.now().strftime('%Y%m%d')}.log"
    logger = logging.getLogger("monitor")
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


# ── KIS 공통 헬퍼 ────────────────────────────────────────────────

def _account_parts() -> tuple[str, str]:
    acct = KIS_ACCOUNT.replace("-", "")
    return acct[:8], acct[8:] or "01"


def _kis_auth_headers(tr_id: str, token: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "appkey":        KIS_APP_KEY,
        "appsecret":     KIS_APP_SECRET,
        "tr_id":         tr_id,
        "custtype":      "P",
        "Content-Type":  "application/json",
    }


# ── positions.json I/O ───────────────────────────────────────────

def _load() -> dict:
    return json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    POSITIONS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── KIS API 호출 ─────────────────────────────────────────────────

def _get_price(token: str, code: str) -> int:
    """현재가 조회. 500/502/503 시 5초 대기 후 최대 3회 재시도. 실패 시 0 반환."""
    _RETRY_STATUS = {500, 502, 503}
    for attempt in range(1, 4):
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
            if resp.status_code in _RETRY_STATUS and attempt < 3:
                log.warning(f"  [{code}] HTTP {resp.status_code} [재시도 {attempt}/3] 5초 대기...")
                time.sleep(5)
                continue
            resp.raise_for_status()
            return int(resp.json()["output"]["stck_prpr"])
        except Exception as exc:
            if attempt < 3:
                log.warning(f"  [{code}] 현재가 조회 실패 [재시도 {attempt}/3]: {exc}")
                time.sleep(5)
            else:
                log.warning(f"  [{code}] 현재가 조회 3회 모두 실패: {exc}")
    return 0


def _get_minute_high(token: str, code: str, lookback_minutes: int) -> int:
    """5분봉 기준 최근 lookback_minutes+5분 이내 최고가 반환. 실패 시 0.
    트레일링 활성 종목에서만 호출 (API 부담 최소화).
    """
    try:
        now = datetime.now()
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
                "FID_ETC_CLS_CODE":       "0",
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD":         code,
                "FID_INPUT_HOUR_1":       "5",   # 5분봉
                "FID_PW_DATA_INCU_YN":    "N",   # 당일 데이터만
            },
            timeout=10,
        )
        resp.raise_for_status()
        candles = resp.json().get("output2", [])
        if not candles:
            return 0

        today_str = now.strftime("%Y%m%d")
        # 5분봉 크기(5분) 만큼 여유를 두어 직전 체크 ~ 현재 구간 완전히 커버
        cutoff = now - timedelta(minutes=lookback_minutes + 5)

        highs: list[int] = []
        for c in candles:
            if c.get("stck_bsop_date", "") != today_str:
                continue
            t_str = c.get("stck_cntg_hour", "")
            if len(t_str) < 4:
                continue
            try:
                candle_dt = now.replace(
                    hour=int(t_str[:2]),
                    minute=int(t_str[2:4]),
                    second=int(t_str[4:6]) if len(t_str) >= 6 else 0,
                    microsecond=0,
                )
                if candle_dt >= cutoff:
                    high = int(c.get("stck_hgpr", "0") or "0")
                    if high > 0:
                        highs.append(high)
            except (ValueError, TypeError):
                continue

        result = max(highs) if highs else 0
        log.debug(f"  [{code}] 분봉 최고가: {result:,}원 ({len(highs)}개 캔들)")
        return result

    except Exception as exc:
        log.debug(f"  [{code}] 분봉 조회 실패 (현재가로 대체): {exc}")
        return 0


def _get_balance(token: str) -> dict[str, dict]:
    """잔고 조회. 종목코드 → {avg_price, qty} 매핑 반환."""
    try:
        cano, acnt_prdt = _account_parts()
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        KIS_APP_KEY,
                "appsecret":     KIS_APP_SECRET,
                "tr_id":         "VTTC8434R",
                "custtype":      "P",
            },
            params={
                "CANO":                  cano,
                "ACNT_PRDT_CD":          acnt_prdt,
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
        result: dict[str, dict] = {}
        for item in resp.json().get("output1", []):
            code = item.get("pdno", "")
            if code:
                result[code] = {
                    "name":      item.get("prdt_name", code),
                    "avg_price": int(float(item.get("pchs_avg_pric", "0"))),
                    "qty":       int(item.get("hldg_qty", "0")),
                }
        return result
    except Exception as exc:
        log.warning(f"  잔고 조회 실패: {exc}")
        return {}


def _sell(token: str, code: str, qty: int) -> str:
    """KIS 모의 시장가 매도. 주문번호 반환."""
    cano, acnt_prdt = _account_parts()
    resp = requests.post(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_kis_auth_headers("VTTC0801U", token),
        json={
            "CANO":         cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "PDNO":         code,
            "ORD_DVSN":     "01",
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     "0",
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1", "매도 실패"))
    return data.get("output", {}).get("ODNO", "")


# ── 미체결 주문 조회 / 취소 ──────────────────────────────────────

def _get_pending_sell_orders(token: str, code: str) -> list[dict]:
    """해당 종목의 매도 미체결 주문 목록 반환. 조회 실패 시 빈 리스트 (fallback용)."""
    tr_id = "VTTC8036R" if IS_PAPER else "TTTC8036R"
    cano, acnt_prdt = _account_parts()
    try:
        resp = requests.get(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            headers={
                "authorization": f"Bearer {token}",
                "appkey":        KIS_APP_KEY,
                "appsecret":     KIS_APP_SECRET,
                "tr_id":         tr_id,
                "custtype":      "P",
            },
            params={
                "CANO":           cano,
                "ACNT_PRDT_CD":   acnt_prdt,
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "INQR_DVSN_1":   "0",
                "INQR_DVSN_2":   "0",
            },
            timeout=10,
        )
        resp.raise_for_status()
        orders = []
        for item in resp.json().get("output", []):
            if item.get("pdno", "") != code:
                continue
            if item.get("sll_buy_dvsn_cd", "") != "01":  # 매도 주문만
                continue
            psbl = int(item.get("psbl_qty", "0") or "0")
            if psbl <= 0:
                continue
            orders.append({
                "order_no": item.get("odno", ""),
                "org_no":   item.get("ord_gno_brno", ""),
                "qty":      int(item.get("ord_qty", "0") or "0"),
                "price":    int(float(item.get("ord_unpr", "0") or "0")),
                "psbl_qty": psbl,
            })
        return orders
    except Exception as exc:
        log.warning(f"  [{code}] 미체결 주문 조회 실패: {exc}")
        return []  # 빈 리스트 → 호출자가 fallback(바로 매도) 처리


def _cancel_order(token: str, code: str, order: dict) -> bool:
    """매도 주문 단건 취소. 성공 True, 실패 False."""
    tr_id = "VTTC0011U" if IS_PAPER else "TTTC0011U"
    cano, acnt_prdt = _account_parts()
    try:
        resp = requests.post(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl",
            headers=_kis_auth_headers(tr_id, token),
            json={
                "CANO":                cano,
                "ACNT_PRDT_CD":        acnt_prdt,
                "KRX_FWDG_ORD_ORGNO": order["org_no"],
                "ORGN_ODNO":           order["order_no"],
                "ORD_DVSN":            "00",
                "RVSE_CNCL_DVSN_CD":  "02",
                "ORD_QTY":             str(order["psbl_qty"]),
                "ORD_UNPR":            "0",
                "QTY_ALL_ORD_YN":     "Y",
            },
            timeout=10,
        )
        resp.raise_for_status()
        rdata = resp.json()
        if rdata.get("rt_cd") != "0":
            log.warning(
                f"  [{code}] 주문취소 실패 (주문번호 {order['order_no']}): {rdata.get('msg1')}"
            )
            return False
        log.info(
            f"[CANCEL] {code} 매도 {order['psbl_qty']}주 @{order['price']:,} "
            f"(주문번호: {order['order_no']})"
        )
        return True
    except Exception as exc:
        log.warning(f"  [{code}] 주문취소 요청 실패: {exc}")
        return False


# ── 잔고 변화 감지 (외부 매수) ───────────────────────────────────

def _detect_new_buys(data: dict, balance: dict) -> bool:
    """last_known_balance 대비 새 종목/수량 증가 감지 → log_buy + positions 자동 추가.

    DRY_RUN=True 또는 balance 비어 있으면 스킵 (오탐 방지).
    data 를 직접 수정. 변경 여부 반환.
    """
    if DRY_RUN or not balance:
        return False

    known = data.get("last_known_balance")

    # 첫 실행: 잔고 vs positions.json 동기화 후 기준 잔고 초기화
    if known is None:
        log.info("[잔고감지] 첫 실행 → 잔고 vs positions.json 동기화")
        open_codes = {
            p["code"]
            for p in data["positions"]
            if p.get("status") in ("OPEN", "HOLDING")
        }
        added = 0
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for code, cur in balance.items():
            if code not in open_codes:
                name = cur.get("name", code)
                avg  = cur["avg_price"]
                qty  = cur["qty"]
                log.info(
                    f"[잔고감지] 누락 종목 자동 추가: {name}({code}) "
                    f"- 평단 {avg:,}원"
                )
                data["positions"].append({
                    "code":            code,
                    "name":            name,
                    "buy_price":       avg,
                    "qty":             qty,
                    "stop_loss":       int(avg * 0.975),
                    "target_price":    int(avg * 1.05),
                    "status":          "OPEN",
                    "trailing_active": False,
                    "peak_price":      0,
                    "last_seen_price": avg,
                    "auto_added":      True,
                    "added_at":        now_str,
                })
                added += 1
        if added:
            log.info(f"[잔고감지] 자동 추가 완료: {added}개 종목")
        data["last_known_balance"] = {
            k: {"avg_price": v["avg_price"], "qty": v["qty"]}
            for k, v in balance.items()
        }
        return True  # 저장 필요

    changed = False

    for code, cur in balance.items():
        name    = cur.get("name", code)
        cur_qty = cur["qty"]
        cur_avg = cur["avg_price"]

        if code not in known:
            # buy.py / rebound_auto 가 이미 추가했을 수 있으므로 OPEN 중복 체크
            existing = next(
                (p for p in data["positions"]
                 if p["code"] == code and p["status"] in ("OPEN", "HOLDING")),
                None,
            )
            recorded_qty = get_open_buy_qty(code)

            if existing:
                # positions.json 에는 있음 — trades 커버 수량과 잔고 비교
                if recorded_qty >= cur_qty:
                    log.info(
                        f"[잔고감지] {existing.get('name', code)}({code}) "
                        f"BUY 기록 {recorded_qty}주 ≥ 잔고 {cur_qty}주 → log_buy 스킵"
                    )
                else:
                    diff = cur_qty - recorded_qty
                    log.info(
                        f"[잔고감지] {existing.get('name', code)}({code}) "
                        f"BUY 기록 {recorded_qty}주 < 잔고 {cur_qty}주 → 차이 {diff}주 기록"
                    )
                    log_buy(code, existing.get("name", code), cur_avg, diff, source="monitor_detected", entry_signal="monitor_detected")
                    changed = True
            else:
                # ── 완전히 새로운 종목 (외부/앱 매수) ────────────────
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log.info(f"[잔고감지] 새 매수: {name}({code}) {cur_qty}주 @ {cur_avg:,}원")
                send_message(f"🛒 새 매수 감지: {name}({code}) {cur_qty}주 @ {cur_avg:,}원")
                if recorded_qty >= cur_qty:
                    log.info(f"[잔고감지] {name}({code}) BUY 기록 {recorded_qty}주 ≥ 잔고 → log_buy 스킵")
                else:
                    diff = cur_qty - recorded_qty
                    log_buy(code, name, cur_avg, diff, source="monitor_detected", entry_signal="external")
                data["positions"].append({
                    "code":            code,
                    "name":            name,
                    "buy_price":       cur_avg,
                    "qty":             cur_qty,
                    "stop_loss":       int(cur_avg * 0.975),
                    "target_price":    int(cur_avg * 1.05),
                    "status":          "OPEN",
                    "trailing_active": False,
                    "peak_price":      None,
                    "last_seen_price": cur_avg,
                    "created_at":      now_str,
                })
                changed = True

        else:
            prev_qty = known[code]["qty"]

            existing = next(
                (p for p in data["positions"]
                 if p["code"] == code and p["status"] in ("OPEN", "HOLDING")),
                None,
            )
            use_name = existing["name"] if existing else name

            # ── 케이스 2: 수량 변화 ─────────────────────────────────
            if cur_qty > prev_qty:
                added_qty    = cur_qty - prev_qty
                recorded_qty = get_open_buy_qty(code)

                if recorded_qty >= cur_qty:
                    # 분할 체결로 잔고만 증가 — 이미 BUY 기록됨
                    log.info(
                        f"[잔고감지] {use_name}({code}) 잔고 +{added_qty}주 "
                        f"→ 분할 체결 (BUY 기록 {recorded_qty}주 ≥ 잔고 {cur_qty}주) → 스킵"
                    )
                else:
                    # 진짜 추가 매수 (BUY 기록에 없는 수량)
                    diff = cur_qty - recorded_qty
                    log.info(
                        f"[잔고감지] 추가 매수: {use_name}({code}) "
                        f"+{diff}주 @ {cur_avg:,}원"
                    )
                    send_message(
                        f"🛒 추가 매수 감지: {use_name}({code}) "
                        f"+{diff}주 @ {cur_avg:,}원"
                    )
                    log_buy(code, use_name, cur_avg, diff, source="monitor_detected", entry_signal="monitor_detected")

                if existing:
                    existing["qty"] = cur_qty
                changed = True

            elif cur_qty < prev_qty:
                removed_qty = prev_qty - cur_qty
                log.info(
                    f"[잔고감지] 일부 매도: {use_name}({code}) "
                    f"-{removed_qty}주 (잔여 {cur_qty}주)"
                )
                if existing:
                    sell_price = existing.get("last_seen_price") or existing.get("buy_price", 0)
                    buy_price  = existing.get("buy_price", 0)
                    profit_pct = (sell_price - buy_price) / buy_price * 100 if buy_price else 0.0
                    msg = (
                        f"📤 부분 매도 감지: {use_name}({code}) "
                        f"-{removed_qty}주 @ {sell_price:,}원 [manual] {profit_pct:+.1f}%"
                    )
                    log.info(msg)
                    send_message(msg)
                    log_sell(code, sell_price, "manual", use_name, sell_qty=removed_qty)
                    existing["qty"] = cur_qty
                changed = True

            # ── 케이스 1: 평단가 변화 (수량 변화와 독립적으로 처리) ──────
            if existing and cur_avg != existing.get("buy_price", 0):
                old_avg  = existing["buy_price"]
                old_sl   = existing["stop_loss"]
                old_tp   = existing["target_price"]
                new_sl   = int(cur_avg * 0.975)
                new_tp   = int(cur_avg * 1.05)
                trailing = existing.get("trailing_active", False)

                existing["buy_price"] = cur_avg
                existing["stop_loss"] = new_sl
                if not trailing:
                    existing["target_price"] = new_tp
                # trailing_active=True: peak_price·target_price 유지 (이미 +10% 확보)

                log.info(
                    f"[잔고감지] 평단 업데이트: {use_name}({code}) "
                    f"{old_avg:,} → {cur_avg:,}원 "
                    f"손절:{old_sl:,}→{new_sl:,}"
                )
                tp_display = old_tp if trailing else new_tp
                trailing_note = " (트레일링 활성 → peak 유지)" if trailing else ""
                send_message(
                    f"📊 추가 매수 감지: {use_name}\n"
                    f"평단: {old_avg:,} → {cur_avg:,}원\n"
                    f"손절: {old_sl:,} → {new_sl:,}원\n"
                    f"목표: {old_tp:,} → {tp_display:,}원"
                    + trailing_note
                )
                changed = True

    # 기준 잔고 업데이트 (name 제외하고 저장 — 순수 비교용)
    data["last_known_balance"] = {
        k: {"avg_price": v["avg_price"], "qty": v["qty"]}
        for k, v in balance.items()
    }
    return changed


# ── 시작 시 검증 출력 ─────────────────────────────────────────────

def _startup_check(token: str) -> None:
    """토큰·잔고·현재가·여유% 출력 후 5분 체크 안내."""
    sep = "=" * 55
    log.info(sep)
    log.info(f"[검증] {'DRY_RUN 모드' if DRY_RUN else '실거래 모드 (모의투자)'}")
    log.info(f"[검증] KIS_BASE_URL = {KIS_BASE_URL}")
    log.info(f"[검증] 토큰 = {token[:12]}...")

    # 잔고 조회
    balance = _get_balance(token) if not DRY_RUN else {}
    log.info(f"\n[잔고] KIS 잔고 조회: {len(balance)}개 종목")
    for code, b in balance.items():
        log.info(f"  {code}: 평단가 {b['avg_price']:,}원 × {b['qty']}주")

    # 포지션별 현재가 + 여유%
    data = _load()
    log.info(f"\n[포지션] 감시 대상 종목:")
    for pos in data.get("positions", []):
        if pos["status"] not in ("PENDING", "OPEN", "HOLDING"):
            continue
        code = pos["code"]
        cur = _get_price(token, code) if not DRY_RUN else pos["buy_price"]
        if cur and pos.get("buy_price"):
            to_stop   = (cur - pos["stop_loss"])    / pos["buy_price"] * 100
            to_target = (pos["target_price"] - cur) / pos["buy_price"] * 100
            trail_tag = " [트레일링 활성]" if pos.get("trailing_active") else ""
            log.info(
                f"  {pos['name']}({code})  현재가:{cur:,}원"
                f"  손절까지:{to_stop:+.1f}%  목표까지:{to_target:+.1f}%{trail_tag}"
            )
        else:
            log.info(f"  {pos['name']}({code})  현재가 조회 실패 또는 buy_price 없음")

    log.info(f"\n[안내] {CHECK_INTERVAL}분마다 자동 체크 시작 (장중 09:00~15:35)")
    log.info(sep)


# ── 당일 청산 ────────────────────────────────────────────────────

def _eod_liquidate(token: str) -> None:
    """장 마감 전 보유 종목 전량 시장가 매도 (당일 청산).

    OPEN/HOLDING 상태인 종목 중 KIS 잔고에 실제로 존재하는 것만 매도.
    PENDING은 체결 미확인이므로 스킵.
    """
    data = _load()
    balance = _get_balance(token) if not DRY_RUN else {}
    now = datetime.now()
    liquidated = 0
    skipped = 0

    for pos in data["positions"]:
        if pos["status"] not in ("OPEN", "HOLDING"):
            continue
        code = pos["code"]
        name = pos.get("name", code)

        if not DRY_RUN and code not in balance:
            log.info(f"  [EOD] {name}({code}) 잔고 없음 → 스킵")
            skipped += 1
            continue

        try:
            cur_price = _get_price(token, code) if not DRY_RUN else pos["buy_price"]
            if cur_price <= 0:
                log.warning(f"  [EOD] {name}({code}) 현재가 조회 실패 → 스킵")
                skipped += 1
                continue

            qty = balance.get(code, {}).get("qty", pos["qty"]) if not DRY_RUN else pos["qty"]
            if qty <= 0:
                log.warning(f"  [EOD] {name}({code}) 수량 0 → 스킵")
                skipped += 1
                continue

            if not DRY_RUN:
                pending_sells = _get_pending_sell_orders(token, code)
                for order in pending_sells:
                    _cancel_order(token, code, order)
                    time.sleep(0.3)
                if pending_sells:
                    time.sleep(1)

            if DRY_RUN:
                order_no = "DRY_RUN"
                log.info(f"  [DRY_RUN][EOD] {name}({code}) 청산 @ {cur_price:,}원 ({qty}주)")
            else:
                log.info(f"  [EOD] {name}({code}) 전량 매도 {qty}주 시장가")
                order_no = _sell(token, code, qty)

            profit     = (cur_price - pos["buy_price"]) * qty
            profit_pct = (cur_price - pos["buy_price"]) / pos["buy_price"] * 100
            is_profit  = profit_pct >= 0

            pos["status"]        = "CLOSED_PROFIT" if is_profit else "CLOSED_LOSS"
            pos["sell_price"]    = cur_price
            pos["sell_time"]     = now.strftime("%Y-%m-%d %H:%M:%S")
            pos["profit_pct"]    = round(profit_pct, 2)
            pos["sell_order_no"] = order_no

            emoji = "🌅" if is_profit else "🌇"
            msg = (
                f"{emoji} [EOD청산] {name} {profit_pct:+.1f}% "
                f"({pos['buy_price']:,} → {cur_price:,}원, {profit:+,}원)"
            )
            log.info(msg)
            send_message(msg)
            log_sell(code, cur_price, "eod_liquidation", name)
            liquidated += 1

        except Exception as exc:
            log.error(f"  [EOD] {name}({code}) 청산 실패: {exc}")
            try:
                send_message(f"❌ [EOD청산 실패] {name}: {exc}")
            except Exception:
                pass
            skipped += 1

    _save(data)
    summary = f"🔚 당일 청산 완료: 매도 {liquidated}건 / 스킵 {skipped}건"
    log.info(summary)
    try:
        send_message(summary)
    except Exception:
        pass


# ── 종목 체크 ────────────────────────────────────────────────────

def _check_stocks(error_counts: dict[str, int]) -> None:
    """PENDING/OPEN 종목 점검 — 손절 / 트레일링 스탑 / 목표가 활성화."""
    data = _load()
    token = get_token() if not DRY_RUN else "DRY"
    balance = _get_balance(token) if not DRY_RUN else {}
    now = datetime.now()
    changed = False

    # ── 새 매수 / 추가 매수 감지 ─────────────────────────────────────
    if _detect_new_buys(data, balance):
        changed = True

    # ── 작업 3: 잔고에서 사라진 OPEN 종목 → 외부 매도로 처리 ──────────
    if not DRY_RUN and balance:
        for pos in data["positions"]:
            if pos["status"] not in ("OPEN", "HOLDING") or pos["code"] in balance:
                continue
            code = pos["code"]

            # 매수 직후 분할 체결 대기 중인지 확인 (5분 이내 → 오탐 방지)
            created_raw = pos.get("created_at") or pos.get("buy_time")
            if created_raw:
                try:
                    elapsed = (now - datetime.fromisoformat(created_raw)).total_seconds()
                    if elapsed < PENDING_FILL_GRACE:
                        if not pos.get("pending_fill_notified"):
                            msg = (
                                f"⏳ 매수 체결 대기: {pos['name']}({code})\n"
                                f"매수 후 {int(elapsed)}초 경과 — 잔고 반영 대기 중"
                            )
                            log.info(
                                f"  [체결대기] {pos['name']}({code}) "
                                f"{int(elapsed)}초 경과 → 잔고 미반영 무시"
                            )
                            send_message(msg)
                            pos["pending_fill_notified"] = True
                            changed = True
                        else:
                            log.info(
                                f"  [체결대기] {pos['name']}({code}) "
                                f"{int(elapsed)}초 경과 → 잔고 미반영 무시 (알림 기발송)"
                            )
                        continue
                except (ValueError, TypeError):
                    pass

            sell_price = pos.get("last_seen_price") or pos["buy_price"]
            stop_loss  = pos["stop_loss"]
            target     = pos["target_price"]

            if sell_price <= stop_loss * 1.01:
                reason = "stop_loss"
            elif pos.get("trailing_active"):
                reason = "trailing"
            elif sell_price >= target * 0.99:
                reason = "take_profit"
            else:
                reason = "manual"

            profit_pct = (sell_price - pos["buy_price"]) / pos["buy_price"] * 100

            msg = (
                f"📤 외부 매도 감지: {pos['name']}({code}) "
                f"@ {sell_price:,}원 [{reason}] {profit_pct:+.1f}%"
            )
            log.info(msg)
            send_message(msg)
            log_sell(code, sell_price, reason, pos["name"])

            pos["status"]     = "CLOSED_PROFIT" if profit_pct >= 0 else "CLOSED_LOSS"
            pos["sell_price"] = sell_price
            pos["sell_time"]  = now.strftime("%Y-%m-%d %H:%M:%S")
            pos["profit_pct"] = round(profit_pct, 2)
            changed = True

    for pos in data["positions"]:
        code   = pos["code"]
        status = pos["status"]
        if status not in ("PENDING", "OPEN", "HOLDING"):
            continue

        try:
            # (a) PENDING: 체결 여부 확인
            if status == "PENDING":
                if DRY_RUN or code in balance:
                    avg = balance.get(code, {}).get("avg_price", pos["buy_price"])
                    pos["buy_price"] = avg or pos["buy_price"]
                    pos["status"] = "OPEN"
                    log.info(f"  [{pos['name']}] PENDING → OPEN (평단가: {pos['buy_price']:,}원)")
                    changed = True
                continue

            # (b) OPEN: 현재가 조회
            cur_price = _get_price(token, code) if not DRY_RUN else pos["buy_price"]
            if cur_price <= 0:
                continue

            # last_seen_price 갱신 (외부 매도 시 sell_price 추정에 사용)
            if pos.get("last_seen_price") != cur_price:
                pos["last_seen_price"] = cur_price
                changed = True

            # ── 한 줄 상태 요약 ──────────────────────────────────────────
            stop_pct   = (pos["stop_loss"]    - cur_price) / cur_price * 100
            target_pct = (pos["target_price"] - cur_price) / cur_price * 100
            if pos.get("trailing_active"):
                peak = pos["peak_price"] or cur_price
                drop_pct = (cur_price - peak) / peak * 100
                log.info(
                    f"  - {pos['name']}({code}): {cur_price:,}원"
                    f" / 손절 {pos['stop_loss']:,} ({stop_pct:+.1f}%)"
                    f" / 목표 {pos['target_price']:,} ({target_pct:+.1f}%)"
                    f" / 트레일링 peak {peak:,} (현재 {drop_pct:.1f}% 하락)"
                )
            else:
                log.info(
                    f"  - {pos['name']}({code}): {cur_price:,}원"
                    f" / 손절 {pos['stop_loss']:,} ({stop_pct:+.1f}%)"
                    f" / 목표 {pos['target_price']:,} ({target_pct:+.1f}%)"
                    f" / 트레일링 OFF"
                )

            log.debug(
                f"    목표={pos['target_price']:,}"
            )

            # ── 손절 판단 (항상 최우선) ──────────────────────────────
            if cur_price <= pos["stop_loss"]:
                if not DRY_RUN and STOP_LOSS_HOLD_ON_VBOUNCE:
                    rebound = detect_rebound(code, token)
                    if rebound["is_rebound"] and rebound["confidence"] in ("high", "medium"):
                        log.info(
                            f"  [{pos['name']}] 손절선 도달 but V자 반등 신호 → 매도 보류"
                        )
                        log.info(f"    신호: {', '.join(rebound['signals'])}")
                        send_message(
                            f"⏸ [{pos['name']}] 손절선 도달했지만 V자 반등 신호로 보류\n"
                            f"신호: {', '.join(rebound['signals'])}"
                        )
                        continue
                should_stop = True
                trail_triggered = False
            else:
                should_stop = False
                trail_triggered = False
                quick_profit_triggered = cur_price >= pos["buy_price"] * (1 + QUICK_TAKE_PROFIT)

                if not quick_profit_triggered:
                    if pos.get("trailing_active"):
                        # ── 트레일링 스탑 ─────────────────────────────────
                        # 분봉 최고가로 peak 정확도 개선 (trailing 활성 종목만 API 호출)
                        minute_high    = _get_minute_high(token, code, CHECK_INTERVAL) if not DRY_RUN else 0
                        effective_high = max(cur_price, minute_high)
                        peak           = pos["peak_price"] or cur_price

                        if effective_high > peak:
                            old_peak          = peak
                            pos["peak_price"] = effective_high
                            peak              = effective_high   # 당 사이클 트리거 판단에도 반영
                            changed           = True
                            src = f" (분봉 최고가 {minute_high:,}원 반영)" if minute_high > cur_price else ""
                            log.info(
                                f"  [{pos['name']}] 트레일링 고점 갱신: "
                                f"{old_peak:,} → {effective_high:,}원{src}"
                            )

                        if cur_price <= peak * (1 - TRAILING_DROP):
                            trail_triggered = True
                    else:
                        # ── 목표가 도달 → 트레일링 활성화 ───────────────────
                        if cur_price >= pos["target_price"]:
                            pos["trailing_active"] = True
                            pos["peak_price"]      = cur_price
                            changed = True
                            msg = (
                                f"🎯 [{pos['name']}] 목표가 도달 ({cur_price:,}원) "
                                f"→ 트레일링 스탑 활성화 (-1.5% 하락 시 매도)"
                            )
                            log.info(msg)
                            send_message(msg)
                            continue

            if not should_stop and not trail_triggered and not quick_profit_triggered:
                continue

            # ── 손절 전처리: 매도 미체결 취소 → 잔고 복구 대기 ──────────
            if should_stop and not DRY_RUN:
                pending_sells = _get_pending_sell_orders(token, code)
                if pending_sells:
                    log.info(
                        f"  [{pos['name']}] 손절 전 매도 미체결 {len(pending_sells)}건 취소 중..."
                    )
                    for order in pending_sells:
                        _cancel_order(token, code, order)

                    # 잔고 복구 대기 (3초 간격 × 최대 3회)
                    qty_available = 0
                    for attempt in range(1, 4):
                        time.sleep(3)
                        fresh_bal = _get_balance(token)
                        qty_available = fresh_bal.get(code, {}).get("qty", 0)
                        log.info(
                            f"  [{pos['name']}] 잔고 재조회 {attempt}/3: {qty_available}주"
                        )
                        if qty_available > 0:
                            pos["qty"] = qty_available
                            break

                    if qty_available <= 0:
                        log.error(
                            f"  [{pos['name']}] 취소 후 잔고 재조회 실패 (0주) → ERROR"
                        )
                        pos["status"] = "ERROR"
                        changed = True
                        send_message(
                            f"❌ [{pos['name']}({code})] 손절 실패: "
                            f"미체결 취소 후 잔고 없음 → ERROR"
                        )
                        continue
                # pending_sells 없음(미체결 없거나 조회 실패): 기존대로 바로 매도

            # ── 매도 실행 ─────────────────────────────────────────────
            if quick_profit_triggered:
                emoji, label = "💰", "즉시익절"
                is_loss = False
            elif trail_triggered:
                emoji, label = "📉", "트레일링매도"
                is_loss = False
            else:
                emoji, label = "🛡️", "손절"
                is_loss = True

            if DRY_RUN:
                order_no = "DRY_RUN"
                log.info(f"  [DRY_RUN] {pos['name']} {label} @ {cur_price:,}원")
            else:
                if should_stop:
                    log.info(f"[STOPLOSS] {code} 전량 매도 {pos['qty']}주 시장가")
                order_no = _sell(token, code, pos["qty"])

            profit     = (cur_price - pos["buy_price"]) * pos["qty"]
            profit_pct = (cur_price - pos["buy_price"]) / pos["buy_price"] * 100

            pos["status"]        = "CLOSED_LOSS" if is_loss else "CLOSED_PROFIT"
            pos["sell_price"]    = cur_price
            pos["sell_time"]     = now.strftime("%Y-%m-%d %H:%M:%S")
            pos["profit_pct"]    = round(profit_pct, 2)
            pos["sell_order_no"] = order_no
            changed = True
            error_counts[code] = 0

            # A: 매도 직후 즉시 저장 — 다음 사이클 외부매도 오탐 방지
            if not DRY_RUN:
                _save(data)

            msg = (
                f"{emoji} [{pos['name']}] {label} {profit_pct:+.1f}% "
                f"({pos['buy_price']:,} → {cur_price:,}원, {profit:+,}원)"
            )
            log.info(msg)
            send_message(msg)
            sell_reason = "quick_profit" if quick_profit_triggered else ("trailing" if trail_triggered else "stop_loss")
            log_sell(code, cur_price, sell_reason, pos["name"])

        except Exception as exc:
            cnt = error_counts.get(code, 0) + 1
            error_counts[code] = cnt
            log.error(f"  [{pos['name']}] 오류 ({cnt}/{MAX_ERRORS}): {exc}")
            if cnt >= MAX_ERRORS:
                pos["status"] = "ERROR"
                changed = True
                if _can_send_error_alert(code):
                    send_message(f"❌ [{pos['name']}] 연속 {MAX_ERRORS}회 오류 → ERROR 마킹")
                    _mark_error_alert(code)

    if changed:
        _save(data)


# ── 오류 알림 디바운스 ───────────────────────────────────────────

def _can_send_error_alert(code: str) -> bool:
    """같은 종목에 대해 5분 이내 오류 알림이 이미 발송됐으면 False."""
    try:
        if ERROR_COOLDOWN_FILE.exists():
            cd = json.loads(ERROR_COOLDOWN_FILE.read_text(encoding="utf-8"))
            if time.time() - cd.get(code, 0) < ERROR_COOLDOWN_SECS:
                return False
    except Exception:
        pass
    return True


def _mark_error_alert(code: str) -> None:
    try:
        cd: dict = {}
        if ERROR_COOLDOWN_FILE.exists():
            cd = json.loads(ERROR_COOLDOWN_FILE.read_text(encoding="utf-8"))
        cd[code] = time.time()
        ERROR_COOLDOWN_FILE.write_text(json.dumps(cd), encoding="utf-8")
    except Exception:
        pass


# ── 단일 인스턴스 락 ─────────────────────────────────────────────

def _acquire_monitor_lock() -> bool:
    """이미 실행 중인 인스턴스가 있으면 False 반환."""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)  # 프로세스 생존 확인 (존재하면 예외 없음)
            log.warning(f"이미 monitor.py 실행 중 (PID {pid}) → 새 인스턴스 종료")
            send_message(f"⚠️ monitor.py 중복 실행 감지 (PID {pid}) → 새 인스턴스 종료")
            return False
        except (ProcessLookupError, PermissionError):
            log.info("기존 lock PID 프로세스 없음 → lock 파일 덮어씀")
        except ValueError:
            log.info("lock 파일 내용 비정상 → 덮어씀")
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_monitor_lock() -> None:
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


# ── 메인 루프 ────────────────────────────────────────────────────

def main() -> None:
    if not POSITIONS_PATH.exists():
        sys.exit("positions.json 없음. trader.py 먼저 실행.")

    # D: 단일 인스턴스 보장
    if not _acquire_monitor_lock():
        sys.exit(1)

    try:
        token = get_token() if not DRY_RUN else "DRY"
        data  = _load()

        # 시작 시 잔고 동기화 — 잔고에 있지만 positions.json 에 없는 종목 자동 추가
        if not DRY_RUN:
            balance = _get_balance(token)
            if _detect_new_buys(data, balance):
                _save(data)

        active = [p for p in data["positions"] if p["status"] in ("PENDING", "OPEN", "HOLDING")]
        log.info(f"monitor.py 시작. 감시 종목 {len(active)}개")
        log.info(f"[설정] {CHECK_INTERVAL}분마다 자동 체크")
        send_message(f"🛡️ monitor.py 시작. 감시 종목 {len(active)}개")

        _startup_check(token)

        error_counts: dict[str, int] = {}
        last_check_minute = -1
        last_market_hour  = -1
        eod_done          = False

        try:
            while True:
                now = datetime.now()
                t   = now.time()

                if t < MARKET_OPEN:
                    log.info(f"[{now.strftime('%H:%M')}] 장 시작 대기 중")
                    time.sleep(60)
                    continue

                if USE_EOD_LIQUIDATION and not eod_done and EOD_LIQUIDATION_TIME <= t < MARKET_CLOSE:
                    log.info(f"[{now.strftime('%H:%M')}] 🔚 당일 청산 시작")
                    try:
                        send_message(f"🔚 당일 청산 시작 ({now.strftime('%H:%M')})")
                    except Exception:
                        pass
                    try:
                        _eod_liquidate(token)
                    except Exception as exc:
                        log.error(f"[EOD] 청산 중 오류: {exc}")
                        try:
                            send_message(f"❌ EOD 청산 오류: {exc}")
                        except Exception:
                            pass
                    eod_done = True

                if t >= MARKET_CLOSE:
                    log.info(f"[{now.strftime('%H:%M')}] 장 마감 → 모니터 종료")
                    sys.exit(0)

                # 1시간마다 시장 패닉 감지
                if now.hour != last_market_hour:
                    last_market_hour = now.hour
                    try:
                        mkt = get_market_context()
                        k   = mkt["kospi"]
                        log.info(f"[시장] 코스피 {k['change']:+.1f}% ({k['trend']})")
                        if k["trend"] == "panic":
                            send_message(
                                f"🚨 시장 패닉 (코스피 {k['change']:+.1f}%)\n"
                                f"보유 종목 손절 라인 -4% 임시 완화 권장"
                            )
                    except Exception as exc:
                        log.debug(f"[시장] 컨텍스트 조회 실패: {exc}")

                # 09:00 ~ 15:35: CHECK_INTERVAL 분마다 체크
                if now.minute % CHECK_INTERVAL == 0 and now.minute != last_check_minute:
                    last_check_minute = now.minute
                    log.info(f"[{now.strftime('%H:%M')}] 종목 체크 중...")
                    _check_stocks(error_counts)

                time.sleep(60)

        except KeyboardInterrupt:
            send_message("⛔ monitor.py 강제 종료")
            log.info("강제 종료.")

    finally:
        _release_monitor_lock()


if __name__ == "__main__":
    main()
