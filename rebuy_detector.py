#!/usr/bin/env python3
"""손절 후 재매수 알림 시스템 (V자 Stage 2).

손절한 종목이 재진입 조건을 모두 충족하면 텔레그램 알림.
자동 매수 X — 수동 결정용.

체크 조건 (모두 충족 시 알림):
  1. 손절일 + 3영업일 경과
  2. 현재가 > MA20  (FinanceDataReader)
  3. detect_rebound confidence == "medium"  (KIS 토큰 전달)
  4. 현재 미보유  (positions.json)

사용:
  python rebuy_detector.py              # 5분 루프 (장중만 실행)
  python rebuy_detector.py --once       # 1회 실행 후 종료
  python rebuy_detector.py --dry-run    # 텔레그램 발송 스킵
  python rebuy_detector.py --force      # 중복 알림 무시 (테스트용)
  python rebuy_detector.py --days 60    # 조회 기간 60일
"""

import argparse
import json
import logging
import math
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── 경로 상수 ─────────────────────────────────────────────────────

TRADES_PATH    = Path(r"C:\Stock\data\trades.json")
POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")
STATE_DIR      = Path(r"C:\Stock\stock-bot\data")
LOCK_PATH      = Path(r"C:\Stock\stock-bot\rebuy_detector.lock")

MARKET_OPEN_H,  MARKET_OPEN_M  = 9,  30
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 20
LOCK_TIMEOUT   = 600   # 10분
CHECK_INTERVAL = 300   # 5분 루프
DEFAULT_DAYS   = 30


# ── 로깅 ─────────────────────────────────────────────────────────

def _setup_log() -> logging.Logger:
    logger = logging.getLogger("rebuy")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(ch)
    return logger

log = _setup_log()


# ── 락 파일 (중복 실행 방지) ──────────────────────────────────────

@contextmanager
def _process_lock(timeout: int = LOCK_TIMEOUT):
    """rebuy_detector.lock — 동시 실행 방지."""
    deadline  = time.monotonic() + timeout
    acquired  = False
    try:
        while time.monotonic() < deadline:
            try:
                fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                time.sleep(1)
        if not acquired:
            raise TimeoutError(
                f"rebuy_detector.lock 타임아웃 ({timeout}초) — 다른 프로세스 실행 중일 수 있음"
            )
        yield
    finally:
        if acquired:
            LOCK_PATH.unlink(missing_ok=True)


# ── 거래 시간 확인 ────────────────────────────────────────────────

def _is_trading_session() -> bool:
    """평일 09:30~15:20 여부."""
    now = datetime.now()
    if now.weekday() >= 5:   # 토(5), 일(6)
        return False
    h, m = now.hour, now.minute
    after_open  = (h, m) >= (MARKET_OPEN_H,  MARKET_OPEN_M)
    before_close = (h, m) <= (MARKET_CLOSE_H, MARKET_CLOSE_M)
    return after_open and before_close


# ── 상태 파일 (24시간 알림 중복 방지) ────────────────────────────

def _state_path(d: date | None = None) -> Path:
    return STATE_DIR / f"rebuy_notified_{(d or date.today()).isoformat()}.json"


def _load_notified(force: bool = False) -> set[str]:
    """오늘 이미 알림 발송한 종목코드 set. force=True면 무시."""
    if force:
        return set()
    path = _state_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("codes", []))
    except Exception:
        return set()


def _save_notified(codes: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {"date": date.today().isoformat(), "codes": sorted(codes)}
    _state_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── 영업일 계산 ───────────────────────────────────────────────────

def _business_days_since(sell_date_str: str) -> int:
    """손절일 이후 경과 영업일 수 (토·일 제외, 오늘 포함 X)."""
    try:
        sell_date = date.fromisoformat(sell_date_str)
    except ValueError:
        return 0
    today = date.today()
    count = 0
    d = sell_date + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:   # 0=월~4=금
            count += 1
        d += timedelta(days=1)
    return count


# ── 손절 기록 조회 ────────────────────────────────────────────────

def _load_stop_losses(days: int = DEFAULT_DAYS) -> list[dict]:
    """trades.json에서 최근 days일 손절 기록. 종목별 최신 1건만."""
    if not TRADES_PATH.exists():
        log.warning("trades.json 없음")
        return []
    data   = json.loads(TRADES_PATH.read_text(encoding="utf-8"))
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    latest: dict[str, dict] = {}
    for t in data.get("trades", []):
        if t.get("type") != "SELL":
            continue
        if t.get("reason") != "stop_loss":
            continue
        if t.get("status") == "ERROR":
            continue
        d = t.get("date", "")
        if d < cutoff:
            continue
        code = t["code"]
        if code not in latest or d > latest[code]["date"]:
            latest[code] = t

    return list(latest.values())


# ── 현재 보유 종목 ────────────────────────────────────────────────

def _held_codes() -> set[str]:
    """positions.json에서 보유 중인 종목코드."""
    if not POSITIONS_PATH.exists():
        return set()
    data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    return {
        p["code"] for p in data.get("positions", [])
        if p.get("status") in ("OPEN", "HOLDING", "PENDING")
    }


# ── 가격 데이터 (FinanceDataReader) ──────────────────────────────

def _get_price_ma20(code: str) -> tuple[int, int] | None:
    """(현재가, MA20) 반환. 데이터 부족 또는 실패 시 None."""
    try:
        import FinanceDataReader as fdr
        end   = date.today()
        start = end - timedelta(days=60)
        df    = fdr.DataReader(code, start.isoformat(), end.isoformat())
        if df is None or df.empty:
            return None
        closes = df["Close"].dropna().tolist()
        if len(closes) < 20:
            log.debug(f"  [{code}] 종가 데이터 부족: {len(closes)}일")
            return None
        cur_price = int(closes[-1])
        ma20      = round(sum(closes[-20:]) / 20)
        return cur_price, ma20
    except Exception as exc:
        log.debug(f"  [{code}] 가격 조회 실패: {exc}")
        return None


# ── 보조 정보 (pykrx / FinanceDataReader) ─────────────────────────

def _get_extra_info(code: str) -> dict[str, str]:
    """시총(억), 변동성(%) — 조회 실패 시 'N/A' 반환."""
    result: dict[str, str] = {"시총": "N/A", "변동성": "N/A"}

    # 시총: pykrx
    try:
        from pykrx import stock as px
        today_str = date.today().strftime("%Y%m%d")
        df = px.get_market_cap_by_date(today_str, today_str, code)
        if df is not None and not df.empty and "시가총액" in df.columns:
            cap_won = int(df["시가총액"].iloc[-1])
            result["시총"] = f"{cap_won // 100_000_000:,}"
    except Exception as exc:
        log.debug(f"  [{code}] 시총 조회 실패: {exc}")

    # 변동성: FinanceDataReader 20일 연율화 표준편차
    try:
        import FinanceDataReader as fdr
        df = fdr.DataReader(code, (date.today() - timedelta(days=45)).isoformat())
        if df is not None and not df.empty:
            closes = df["Close"].dropna().tolist()
            if len(closes) >= 21:
                returns = [
                    (closes[i] - closes[i - 1]) / closes[i - 1]
                    for i in range(len(closes) - 20, len(closes))
                ]
                vol = math.sqrt(sum(r ** 2 for r in returns) / len(returns)) * math.sqrt(252) * 100
                result["변동성"] = f"{vol:.1f}"
    except Exception as exc:
        log.debug(f"  [{code}] 변동성 계산 실패: {exc}")

    return result


# ── 텔레그램 발송 (3회 재시도) ────────────────────────────────────

def _send_alert(msg: str, dry_run: bool = False, retries: int = 3) -> bool:
    if dry_run:
        log.info("[DRY-RUN] 텔레그램 발송 스킵:\n" + msg)
        return True
    from telegram_alert import send_message
    for attempt in range(1, retries + 1):
        if send_message(msg):
            return True
        if attempt < retries:
            log.warning(f"  텔레그램 발송 실패 [{attempt}/{retries}] → 5초 후 재시도")
            time.sleep(5)
    log.error(f"  텔레그램 {retries}회 모두 실패")
    return False


# ── 알림 메시지 포맷 ──────────────────────────────────────────────

def _format_alert(
    sell:      dict,
    cur_price: int,
    ma20:      int,
    rebound:   dict,
    bus_days:  int,
    extra:     dict[str, str],
) -> str:
    code       = sell["code"]
    name       = sell.get("name", code)
    sell_date  = sell.get("date", "")
    sell_price = sell.get("price", 0)
    profit_pct = sell.get("profit_pct", 0.0)
    confidence = rebound.get("confidence", "")
    signals    = ", ".join(rebound.get("signals", []))

    return (
        f"🔄 [재매수 시그널] {name}({code})\n"
        f"손절일: {sell_date} ({sell_price:,}원, {profit_pct:+.2f}%)\n"
        f"현재가: {cur_price:,}원 (손절 후 {bus_days}영업일)\n"
        f"조건:\n"
        f"  ✅ 3영업일 경과\n"
        f"  ✅ MA20 돌파 (MA20: {ma20:,}원)\n"
        f"  ✅ V자 반등 ({confidence})\n"
        f"시그널: {signals}\n"
        f"필터: 시총 {extra['시총']}억, 변동성 {extra['변동성']}%\n"
        f"※ 수동 결정 필요 (자동 매수 X)"
    )


# ── 메인 체크 로직 ────────────────────────────────────────────────

def check_rebuy_signals(
    days:    int  = DEFAULT_DAYS,
    dry_run: bool = False,
    force:   bool = False,
) -> list[str]:
    """재매수 조건 점검 → 조건 충족 시 알림 발송.

    Returns:
        알림 발송된 종목코드 리스트
    """
    from rebound_detector import detect_rebound
    from stock_info        import is_spac, get_token

    stop_losses = _load_stop_losses(days)
    log.info(f"[rebuy] 손절 종목 {len(stop_losses)}개 (최근 {days}일)")

    if not stop_losses:
        return []

    held     = _held_codes()
    notified = _load_notified(force=force)
    alerted: list[str] = []

    # KIS 토큰은 detect_rebound 전달에만 사용
    token = ""
    try:
        token = get_token()
    except Exception as exc:
        log.warning(f"KIS 토큰 조회 실패: {exc} → detect_rebound 스킵")

    for sell in stop_losses:
        code = sell["code"]
        name = sell.get("name", code)

        # ── 전처리 필터 ────────────────────────────────────────────

        if is_spac(name):
            log.debug(f"  [{name}({code})] 스팩 → skip")
            continue

        if code in held:
            log.debug(f"  [{name}({code})] 보유 중 → skip")
            continue

        if code in notified:
            log.info(f"  [{name}({code})] 오늘 이미 알림 발송 → skip")
            continue

        log.info(f"  [{name}({code})] 조건 검사...")

        # ── 조건 1: 3영업일 경과 ────────────────────────────────

        bus_days = _business_days_since(sell.get("date", ""))
        if bus_days < 3:
            log.info(f"    ❌ 영업일 부족: {bus_days}일 < 3일")
            continue
        log.debug(f"    ✅ 영업일 경과: {bus_days}일")

        # ── 조건 2: 현재가 > MA20 ────────────────────────────────

        price_data = _get_price_ma20(code)
        if price_data is None:
            log.info(f"    ❌ 가격 데이터 조회 실패 → skip")
            continue
        cur_price, ma20 = price_data
        if cur_price <= ma20:
            log.info(f"    ❌ MA20 미돌파: 현재가 {cur_price:,} <= MA20 {ma20:,}")
            continue
        log.debug(f"    ✅ MA20 돌파: {cur_price:,} > MA20 {ma20:,}")

        # ── 조건 3: V자 반등 confidence == "medium" ──────────────

        if not token:
            log.info(f"    ❌ KIS 토큰 없음 → detect_rebound 불가 → skip")
            continue
        try:
            rebound = detect_rebound(code, token)
        except Exception as exc:
            log.info(f"    ❌ detect_rebound 오류: {exc} → skip")
            continue

        confidence = rebound.get("confidence", "none")
        if confidence != "medium":
            log.info(
                f"    ❌ 반등 신뢰도 불충족: {confidence} "
                f"(medium 필요, high는 백테스트 기준 제외)"
            )
            continue
        log.debug(f"    ✅ V자 반등 medium: {rebound.get('signals')}")

        # ── 모든 조건 충족 → 알림 발송 ──────────────────────────

        log.info(f"    ✅ 모든 조건 충족 → 알림 발송")
        extra = _get_extra_info(code)
        msg   = _format_alert(sell, cur_price, ma20, rebound, bus_days, extra)

        if _send_alert(msg, dry_run=dry_run):
            notified.add(code)
            alerted.append(code)
            _save_notified(notified)
            log.info(f"    📤 {name}({code}) 알림 완료")
        else:
            log.error(f"    ❌ {name}({code}) 알림 실패")

    log.info(
        f"[rebuy] 완료 — 조건 충족 {len(alerted)}건: "
        f"{alerted if alerted else '없음'}"
    )
    return alerted


# ── 루프 / 단발 실행 ──────────────────────────────────────────────

def _run_once(args: argparse.Namespace) -> None:
    """시간 게이트 없이 1회 실행."""
    with _process_lock():
        check_rebuy_signals(
            days    = args.days,
            dry_run = args.dry_run,
            force   = args.force,
        )


def _run_loop(args: argparse.Namespace) -> None:
    """평일 09:30~15:20 에만 CHECK_INTERVAL마다 반복 실행."""
    log.info(f"[rebuy] 루프 모드 시작 ({CHECK_INTERVAL // 60}분 주기, 09:30~15:20)")
    while True:
        if not _is_trading_session():
            now = datetime.now()
            if now.weekday() >= 5:
                log.info("[rebuy] 주말 → 종료")
                break
            if now.hour >= MARKET_CLOSE_H and now.minute > MARKET_CLOSE_M:
                log.info("[rebuy] 장 마감 → 종료")
                break
            # 장 시작 전: 다음 체크까지 대기
            log.info(f"[rebuy] 장외 시간 ({now.strftime('%H:%M')}) → {CHECK_INTERVAL}초 대기")
            time.sleep(CHECK_INTERVAL)
            continue

        try:
            with _process_lock():
                check_rebuy_signals(
                    days    = args.days,
                    dry_run = args.dry_run,
                    force   = args.force,
                )
        except TimeoutError as exc:
            log.warning(f"[rebuy] 락 획득 실패: {exc}")

        log.info(f"[rebuy] {CHECK_INTERVAL // 60}분 후 재실행")
        time.sleep(CHECK_INTERVAL)


# ── 진입점 ────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="손절 후 재매수 시그널 감지 및 텔레그램 알림"
    )
    parser.add_argument(
        "--dry-run",  action="store_true",
        help="텔레그램 발송 스킵 (로그만 출력)",
    )
    parser.add_argument(
        "--days",  type=int, default=DEFAULT_DAYS, metavar="N",
        help=f"손절 기록 조회 기간 (기본: {DEFAULT_DAYS}일)",
    )
    parser.add_argument(
        "--once",  action="store_true",
        help="1회 실행 후 종료 (시간 게이트 무시)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="알림 중복 방지 무시 (테스트용)",
    )
    args = parser.parse_args()

    if args.once:
        _run_once(args)
    else:
        # 주말이면 즉시 종료
        if datetime.now().weekday() >= 5:
            log.info("[rebuy] 주말 → 즉시 종료")
            return
        # 장외 시간이고 루프 모드라면 종료 (스케줄러 등록 시 --once 권장)
        if not _is_trading_session():
            now = datetime.now()
            log.info(f"[rebuy] 장외 시간 ({now.strftime('%H:%M')}) — 루프 대기 또는 --once 사용")
        _run_loop(args)


if __name__ == "__main__":
    main()
