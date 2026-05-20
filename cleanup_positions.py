#!/usr/bin/env python3
"""positions.json 정리 유틸리티.

실행: python cleanup_positions.py

수행 작업:
  1. OPEN 종목 중복 제거 (buy_order_no 있는 항목 우선 보존)
  2. status "CLOSED" → CLOSED_PROFIT / CLOSED_LOSS 보정
  3. name 이 코드와 동일한 경우 네이버 금융으로 종목명 조회
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

POSITIONS_PATH = Path(r"C:\Stock\stock-bot\positions.json")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")

    data      = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    positions = data.get("positions", [])
    report: list[str] = []

    # ── 1) CLOSED → CLOSED_PROFIT / CLOSED_LOSS 보정 ─────────────────
    for pos in positions:
        if pos.get("status") != "CLOSED":
            continue

        pct = pos.get("profit_pct")
        if pct is None:
            sp = pos.get("sell_price")
            bp = pos.get("buy_price")
            if sp and bp and bp > 0:
                pct = (sp - bp) / bp * 100

        if pct is not None:
            new_status = "CLOSED_PROFIT" if pct >= 0 else "CLOSED_LOSS"
            report.append(
                f"  [status 보정] {pos.get('name', pos['code'])}({pos['code']}): "
                f"CLOSED → {new_status} ({pct:+.2f}%)"
            )
            pos["status"] = new_status
        else:
            report.append(
                f"  [status 보정] {pos.get('name', pos['code'])}({pos['code']}): "
                f"매도가 정보 없음 → CLOSED 유지 (수동 확인 필요)"
            )

    # ── 2) OPEN 중복 제거 ────────────────────────────────────────────
    # 코드별 OPEN 그룹 구성
    open_by_code: dict[str, list[dict]] = defaultdict(list)
    for pos in positions:
        if pos.get("status") == "OPEN":
            open_by_code[pos["code"]].append(pos)

    # 각 코드의 대표 항목 결정 (buy_order_no 있는 것 우선)
    primary_by_code: dict[str, dict] = {}
    for code, group in open_by_code.items():
        primary_by_code[code] = next(
            (p for p in group if p.get("buy_order_no")),
            group[0],
        )

    # 중복 없이 순서 보존하여 재구성
    new_positions: list[dict] = []
    seen_open: set[str] = set()

    for pos in positions:
        if pos.get("status") != "OPEN":
            new_positions.append(pos)
            continue

        code    = pos["code"]
        primary = primary_by_code[code]

        if code not in seen_open:
            seen_open.add(code)
            new_positions.append(primary)
            # primary 가 현재 pos 와 다르면 중복 제거 예고 (첫 등장 시 primary 삽입)
        else:
            # 이미 대표 항목이 삽입됨 → 현재 항목은 중복
            report.append(
                f"  [중복 제거] {pos.get('name', code)}({code}) "
                f"target_price={pos.get('target_price', 0):,}원 항목 제거"
            )

    # 첫 등장이 primary 가 아닌 경우(primary 가 뒤에 있을 때) 처리
    # → 위 로직에서 이미 primary 로 교체하므로 추가 처리 불필요

    # ── 3) name 이 코드와 같은 경우 조회 ────────────────────────────
    needs_name = [
        p for p in new_positions
        if not p.get("name") or p.get("name") == p.get("code")
    ]

    if needs_name:
        try:
            from stock_info import get_stock_info
            for pos in needs_name:
                code = pos["code"]
                try:
                    info    = get_stock_info(code)
                    fetched = (info.get("종목명") or "").strip()
                    if fetched and fetched != code:
                        report.append(
                            f"  [name 보정] {code} → {fetched}"
                        )
                        pos["name"] = fetched
                    else:
                        report.append(
                            f"  [name 보정] {code} 조회 실패 → 유지"
                        )
                except Exception as exc:
                    report.append(f"  [name 보정] {code} 오류: {exc}")
        except ImportError:
            report.append("  [name 보정] stock_info 없음 → 스킵")

    # ── 저장 및 리포트 ──────────────────────────────────────────────
    if report:
        data["positions"] = new_positions
        POSITIONS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("positions.json 정리 완료:")
        for line in report:
            print(line)
        print(f"\n  정리 전: {len(positions)}개 항목 → 정리 후: {len(new_positions)}개 항목")
    else:
        print("변경사항 없음.")


if __name__ == "__main__":
    main()
