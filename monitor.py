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
from tracker import log_buy, log_sell

load_dotenv()

KIS_APP_KEY    = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT    = os.getenv("KIS_ACCOUNT", "")
KIS_BASE_URL   = "https://openapivts.koreainvestment.com:29443"  # 모의투자 고정 (실전: 9443)
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3"))

POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")
LOG_DIR        = Path(r"C:\Stock\data\logs")

MARKET_OPEN    = dtime(9, 0)
MARKET_CLOSE   = dtime(15, 35)
MAX_ERRORS     = 5
TRAILING_DROP  = 0.03  # 트레일링 스탑: peak 대비 -3%


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
            if p.get("status") == "OPEN"
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
                    "stop_loss":       int(avg * 0.95),
                    "target_price":    int(avg * 1.10),
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
            # ── 완전히 새로운 종목 ─────────────────────────────────
            log.info(f"[잔고감지] 새 매수: {name}({code}) {cur_qty}주 @ {cur_avg:,}원")
            send_message(f"🛒 새 매수 감지: {name}({code}) {cur_qty}주 @ {cur_avg:,}원")
            log_buy(code, name, cur_avg, cur_qty, source="manual")
            data["positions"].append({
                "code":            code,
                "name":            name,
                "buy_price":       cur_avg,
                "qty":             cur_qty,
                "stop_loss":       int(cur_avg * 0.95),
                "target_price":    int(cur_avg * 1.08),
                "status":          "OPEN",
                "trailing_active": False,
                "peak_price":      None,
                "last_seen_price": cur_avg,
            })
            changed = True

        else:
            prev_qty = known[code]["qty"]
            if cur_qty > prev_qty:
                # ── 수량 증가 = 추가 매수 ──────────────────────────
                added_qty = cur_qty - prev_qty
                existing  = next(
                    (p for p in data["positions"]
                     if p["code"] == code and p["status"] == "OPEN"),
                    None,
                )
                use_name = existing["name"] if existing else name
                log.info(
                    f"[잔고감지] 추가 매수: {use_name}({code}) "
                    f"+{added_qty}주 @ {cur_avg:,}원"
                )
                send_message(
                    f"🛒 추가 매수 감지: {use_name}({code}) "
                    f"+{added_qty}주 @ {cur_avg:,}원"
                )
                log_buy(code, use_name, cur_avg, added_qty, source="manual")
                if existing:
                    existing["qty"]       = cur_qty
                    existing["buy_price"] = cur_avg
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
        if pos["status"] not in ("PENDING", "OPEN"):
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
            if pos["status"] != "OPEN" or pos["code"] in balance:
                continue
            code       = pos["code"]
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

            profit     = (sell_price - pos["buy_price"]) * pos["qty"]
            profit_pct = (sell_price - pos["buy_price"]) / pos["buy_price"] * 100

            msg = (
                f"📤 외부 매도 감지: {pos['name']}({code}) "
                f"@ {sell_price:,}원 [{reason}] {profit_pct:+.1f}%"
            )
            log.info(msg)
            send_message(msg)
            log_sell(code, sell_price, reason, pos["name"])

            pos["status"]     = "CLOSED"
            pos["sell_price"] = sell_price
            pos["sell_time"]  = now.strftime("%Y-%m-%d %H:%M:%S")
            pos["profit_pct"] = round(profit_pct, 2)
            changed = True

    for pos in data["positions"]:
        code   = pos["code"]
        status = pos["status"]
        if status not in ("PENDING", "OPEN"):
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
            if pos.get("trailing_active"):
                peak = pos["peak_price"] or cur_price
                drop_pct = (cur_price - peak) / peak * 100
                log.info(
                    f"  - {pos['name']}({code}): {cur_price:,}원"
                    f" / 트레일링 peak {peak:,} (현재 {drop_pct:.1f}% 하락)"
                )
            else:
                margin_pct = (cur_price - pos["stop_loss"]) / pos["buy_price"] * 100
                log.info(
                    f"  - {pos['name']}({code}): {cur_price:,}원"
                    f" / 손절 {pos['stop_loss']:,} ({margin_pct:+.1f}%)"
                    f" / 트레일링 OFF"
                )

            log.debug(
                f"    목표={pos['target_price']:,}"
            )

            # ── 손절 판단 (항상 최우선) ──────────────────────────────
            if cur_price <= pos["stop_loss"]:
                should_stop = True
                trail_triggered = False
            else:
                should_stop = False
                trail_triggered = False

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
                            f"→ 트레일링 스탑 활성화 (-3% 하락 시 매도)"
                        )
                        log.info(msg)
                        send_message(msg)
                        continue

            if not should_stop and not trail_triggered:
                continue

            # ── 매도 실행 ─────────────────────────────────────────────
            if trail_triggered:
                emoji, label = "📉", "트레일링매도"
                is_loss = False
            else:
                emoji, label = "🛡️", "손절"
                is_loss = True

            if DRY_RUN:
                order_no = "DRY_RUN"
                log.info(f"  [DRY_RUN] {pos['name']} {label} @ {cur_price:,}원")
            else:
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

            msg = (
                f"{emoji} [{pos['name']}] {label} {profit_pct:+.1f}% "
                f"({pos['buy_price']:,} → {cur_price:,}원, {profit:+,}원)"
            )
            log.info(msg)
            send_message(msg)
            sell_reason = "trailing" if trail_triggered else "stop_loss"
            log_sell(code, cur_price, sell_reason, pos["name"])

        except Exception as exc:
            cnt = error_counts.get(code, 0) + 1
            error_counts[code] = cnt
            log.error(f"  [{pos['name']}] 오류 ({cnt}/{MAX_ERRORS}): {exc}")
            if cnt >= MAX_ERRORS:
                pos["status"] = "ERROR"
                changed = True
                send_message(f"❌ [{pos['name']}] 연속 {MAX_ERRORS}회 오류 → ERROR 마킹")

    if changed:
        _save(data)


# ── 일간 요약 ────────────────────────────────────────────────────

def _daily_summary(data: dict) -> None:
    """일간 요약 계산, positions 상태 업데이트, 텔레그램 전송."""
    profit_cnt = loss_cnt = holding_cnt = pending_cnt = 0
    profit_sum = loss_sum = 0

    for pos in data["positions"]:
        s = pos["status"]
        if s == "CLOSED_PROFIT":
            profit_cnt += 1
            profit_sum += (pos["sell_price"] - pos["buy_price"]) * pos["qty"]
        elif s == "CLOSED_LOSS":
            loss_cnt += 1
            loss_sum += (pos["sell_price"] - pos["buy_price"]) * pos["qty"]
        elif s == "OPEN":
            holding_cnt += 1
            pos["status"] = "HOLDING"
        elif s == "PENDING":
            pending_cnt += 1
            pos["status"] = "CANCELED"

    seed    = data.get("seed_money", 0)
    net     = profit_sum + loss_sum
    net_pct = net / seed * 100 if seed else 0.0
    today   = datetime.now().strftime("%Y-%m-%d")

    msg = "\n".join([
        f"📊 [{today}] 일간 거래 요약",
        f"✅ 익절: {profit_cnt}건 {profit_sum:+,}원",
        f"🛡️ 손절: {loss_cnt}건 {loss_sum:+,}원",
        f"📦 보유: {holding_cnt}건 (다음 영업일 모니터링)",
        f"⏸️ 미체결: {pending_cnt}건 (CANCELED)",
        "────────",
        f"누적 손익: {net:+,}원 ({net_pct:+.1f}%)",
    ])
    log.info(msg)
    send_message(msg)


# ── 메인 루프 ────────────────────────────────────────────────────

def main() -> None:
    if not POSITIONS_PATH.exists():
        sys.exit("positions.json 없음. trader.py 먼저 실행.")

    token = get_token() if not DRY_RUN else "DRY"
    data  = _load()

    # 시작 시 잔고 동기화 — 잔고에 있지만 positions.json 에 없는 종목 자동 추가
    if not DRY_RUN:
        balance = _get_balance(token)
        if _detect_new_buys(data, balance):
            _save(data)

    active = [p for p in data["positions"] if p["status"] in ("PENDING", "OPEN")]
    log.info(f"monitor.py 시작. 감시 종목 {len(active)}개")
    log.info(f"[설정] {CHECK_INTERVAL}분마다 자동 체크")
    send_message(f"🛡️ monitor.py 시작. 감시 종목 {len(active)}개")

    _startup_check(token)

    error_counts: dict[str, int] = {}
    last_check_minute = -1

    try:
        while True:
            now = datetime.now()
            t   = now.time()

            if t < MARKET_OPEN:
                log.info(f"[{now.strftime('%H:%M')}] 장 시작 대기 중")
                time.sleep(60)
                continue

            if t >= MARKET_CLOSE:
                data = _load()
                _daily_summary(data)
                _save(data)
                sys.exit(0)

            # 09:00 ~ 15:35: CHECK_INTERVAL 분마다 체크
            if now.minute % CHECK_INTERVAL == 0 and now.minute != last_check_minute:
                last_check_minute = now.minute
                log.info(f"[{now.strftime('%H:%M')}] 종목 체크 중...")
                _check_stocks(error_counts)

            time.sleep(60)

    except KeyboardInterrupt:
        send_message("⛔ monitor.py 강제 종료")
        log.info("강제 종료.")


if __name__ == "__main__":
    main()
