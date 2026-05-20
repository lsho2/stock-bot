import json
import collections
import sys
sys.stdout.reconfigure(encoding="utf-8")

pos_data = json.load(open(r"C:\Stock\stock-bot\positions.json", encoding="utf-8"))
trd_data = json.load(open(r"C:\Stock\data\trades.json", encoding="utf-8"))

holding = {p["code"]: p for p in pos_data["positions"] if p.get("status") in ("OPEN", "HOLDING")}

# 전체 날짜 BUY/SELL 합산
trade_qty = collections.defaultdict(lambda: {"buy": 0, "sell": 0})
for t in trd_data["trades"]:
    if t.get("type") == "BUY":
        trade_qty[t["code"]]["buy"] += t.get("qty", 0)
    elif t.get("type") == "SELL":
        trade_qty[t["code"]]["sell"] += t.get("qty", 0)

print("=== 전체 날짜 정합성 검증 ===\n")
mismatch_count = 0
for code, pos in holding.items():
    pos_qty = pos.get("qty", 0)
    buy = trade_qty[code]["buy"]
    sell = trade_qty[code]["sell"]
    net = buy - sell
    match = "OK" if net == pos_qty else "MISMATCH"
    if match == "MISMATCH":
        mismatch_count += 1
    print(f"[{match}] {pos.get('name','')}({code}): positions={pos_qty}, trades(BUY-SELL)={net} (BUY={buy}, SELL={sell})")

print(f"\n총 {mismatch_count}건 불일치")
