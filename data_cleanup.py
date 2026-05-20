"""
일회성 trades.json 손상 데이터 복구 스크립트.

손상 케이스:
  1. 에스앤디(260970) 5/15 SELL - qty/amount/buy_price/profit 전부 0 또는 잘못된 값
  2. 한화비전(489790) 5/19 SELL - 10주분 레코드 누락 (20주 중 10주만 기록)

사용:
  python data_cleanup.py           # dry-run (기본)
  python data_cleanup.py --apply   # 실제 저장
"""

import argparse
import copy
import json
import os
import shutil
from datetime import datetime

TRADES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "trades.json")
POSITIONS_PATH = os.path.join(os.path.dirname(__file__), "positions.json")

# ────────────────────────────────────────────────
# 복구 명세
# ────────────────────────────────────────────────

# Case 1: 에스앤디(260970) SELL 레코드 수정
CASE1_SELL_ID = "20260515-SELL-260970"
CASE1_FIX = {
    "qty": 25,
    "amount": 1_582_500,   # 25 * 63,300
    "buy_price": 59_700,   # 실제 매수가 (BUY 레코드 참조)
    "profit_amt": 90_000,  # (63,300 - 59,700) * 25
    "profit_pct": 6.03,
}

# Case 2: 한화비전(489790) 누락 SELL 레코드
CASE2_MISSING_SELL = {
    "id": "20260519-SELL-489790-1",
    "date": "2026-05-19",
    "type": "SELL",
    "code": "489790",
    "name": "한화비전",
    "price": 66_900,
    "qty": 10,
    "amount": 669_000,      # 66,900 * 10
    "buy_price": 71_400,    # 첫 번째 매수분 (20260519-BUY-489790)
    "hold_days": 0,
    "profit_amt": -45_000,  # (66,900 - 71,400) * 10
    "profit_pct": -6.30,
    "reason": "stop_loss",
}


# ────────────────────────────────────────────────
# 유틸
# ────────────────────────────────────────────────

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def backup_trades(trades_path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_path = f"{trades_path}.bak.{ts}"
    shutil.copy2(trades_path, bak_path)
    return bak_path


# ────────────────────────────────────────────────
# 손상 케이스 처리
# ────────────────────────────────────────────────

def fix_case1(trades: list) -> tuple[bool, str]:
    """에스앤디 SELL 레코드 수정. 이미 정정된 경우 skip."""
    for t in trades:
        if t.get("id") == CASE1_SELL_ID:
            if t.get("qty", 0) > 0:
                return False, f"[SKIP] 에스앤디(260970) SELL 이미 정정됨 (qty={t['qty']})"
            before = {k: t[k] for k in ("qty", "amount", "buy_price", "profit_amt", "profit_pct")}
            t.update(CASE1_FIX)
            after = {k: t[k] for k in ("qty", "amount", "buy_price", "profit_amt", "profit_pct")}
            msg = (
                f"[복구] 에스앤디(260970) SELL: "
                f"qty {before['qty']}→{after['qty']}, "
                f"buy_price {before['buy_price']}→{after['buy_price']}, "
                f"profit_amt {before['profit_amt']:+,}→{after['profit_amt']:+,}원, "
                f"profit_pct {before['profit_pct']}→{after['profit_pct']}"
            )
            return True, msg
    return False, f"[ERROR] {CASE1_SELL_ID} 레코드를 찾을 수 없음"


def fix_case2(trades: list) -> tuple[bool, str]:
    """한화비전 누락 SELL 추가. 이미 존재하면 skip."""
    for t in trades:
        if t.get("id") == CASE2_MISSING_SELL["id"]:
            return False, f"[SKIP] {CASE2_MISSING_SELL['id']} 이미 존재"

    # 기존 SELL(20260519-SELL-489790) 바로 뒤에 삽입
    anchor_id = "20260519-SELL-489790"
    anchor_idx = next((i for i, t in enumerate(trades) if t.get("id") == anchor_id), None)
    if anchor_idx is not None:
        trades.insert(anchor_idx + 1, CASE2_MISSING_SELL.copy())
    else:
        trades.append(CASE2_MISSING_SELL.copy())

    r = CASE2_MISSING_SELL
    msg = (
        f"[추가] 한화비전(489790) SELL 누락분: "
        f"id={r['id']}, qty={r['qty']}, "
        f"buy_price={r['buy_price']:,}, sell_price={r['price']:,}, "
        f"profit_amt={r['profit_amt']:+,}원, profit_pct={r['profit_pct']}"
    )
    return True, msg


# ────────────────────────────────────────────────
# 일관성 검증
# ────────────────────────────────────────────────

def verify_consistency(trades: list, positions: list) -> tuple[bool, list[str]]:
    """CLOSED_* position의 qty와 trades SELL 합산 수량 비교."""
    # trades SELL 수량 집계
    sell_qty: dict[str, int] = {}
    for t in trades:
        if t.get("type") == "SELL":
            sell_qty[t["code"]] = sell_qty.get(t["code"], 0) + t.get("qty", 0)

    messages = []
    ok = True
    for pos in positions:
        if not pos.get("status", "").startswith("CLOSED"):
            continue
        code = pos["code"]
        name = pos.get("name", code)
        expected = pos["qty"]
        actual = sell_qty.get(code, 0)
        if actual == expected:
            messages.append(f"[검증 OK] {name}({code}): SELL qty={actual} / position qty={expected}")
        else:
            ok = False
            messages.append(
                f"[검증 FAIL] {name}({code}): SELL qty={actual} ≠ position qty={expected} "
                f"(차이: {actual - expected:+d}주)"
            )

    return ok, messages


# ────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="trades.json 손상 데이터 복구")
    parser.add_argument(
        "--apply", action="store_true",
        help="실제 파일 저장 (미지정 시 dry-run)"
    )
    args = parser.parse_args()
    dry_run = not args.apply

    if dry_run:
        print("=== DRY-RUN 모드 (변경 미저장) ===\n")
    else:
        print("=== APPLY 모드 (실제 저장) ===\n")

    trades_data = load_json(TRADES_PATH)
    positions_data = load_json(POSITIONS_PATH)

    trades = trades_data["trades"]
    positions = positions_data["positions"]

    # 작업 대상 복사본에서 처리
    working_trades = copy.deepcopy(trades)

    changed = False

    # Case 1
    ok1, msg1 = fix_case1(working_trades)
    print(msg1)
    if ok1:
        changed = True

    # Case 2
    ok2, msg2 = fix_case2(working_trades)
    print(msg2)
    if ok2:
        changed = True

    print()

    # 검증
    verify_ok, verify_msgs = verify_consistency(working_trades, positions)
    for m in verify_msgs:
        print(m)
    print()

    if not verify_ok:
        print("[ERROR] 일관성 검증 실패 — 변경을 취소합니다.")
        return

    if not changed:
        print("[INFO] 변경 사항 없음 — 저장 불필요.")
        return

    if dry_run:
        print("[DRY-RUN] 검증 통과. --apply 옵션으로 재실행하면 저장됩니다.")
    else:
        bak = backup_trades(TRADES_PATH)
        print(f"[백업] {bak}")
        trades_data["trades"] = working_trades
        save_json(TRADES_PATH, trades_data)
        print(f"[저장] trades.json 업데이트 완료")


if __name__ == "__main__":
    main()
