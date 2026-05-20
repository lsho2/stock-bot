#!/usr/bin/env python3
"""보유 종목 일간 평가 — 내일 보유/매도/추가매수 권장.

실행: python daily_holdings_review.py
작업 스케줄러: 평일 15:36 자동 실행 (daily_report.py 15:35 직후)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

from stock_info import get_stock_info
from fundamental_filter import evaluate_fundamentals, FundamentalResult
from market_context import get_market_context
from tracker import analyze_recommendations
from telegram_alert import send_message

load_dotenv()

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
POSITIONS_PATH  = Path(r"C:\Stock\stock-bot\positions.json")
LAST_REPORT_PATH = Path(r"C:\Stock\data\last_report.txt")
TODAY           = date.today().isoformat()

SPLIT_THRESHOLD = 3_500
_RETRY_DELAYS   = [10, 20, 40]


# ── 데이터 수집 ──────────────────────────────────────────────────

def _parse_price(val: str) -> int:
    """"88,500원" → 88500. 실패 시 0."""
    return int(re.sub(r"[^\d]", "", str(val))) if val else 0


def _build_holding_block(
    pos: dict,
    info: dict,
    fund: FundamentalResult,
    reco_history: dict,
    seed_money: int,
) -> str:
    """포지션 1개 분석 블록 (Gemini 프롬프트용)."""
    code     = pos["code"]
    name     = pos.get("name", code)
    buy_px   = pos["buy_price"]
    qty      = pos["qty"]

    cur_price = _parse_price(info.get("현재가", "")) or buy_px
    pnl_pct   = (cur_price - buy_px) / buy_px * 100 if buy_px else 0.0
    weight    = buy_px * qty / seed_money * 100 if seed_money else 0.0

    # 기술적 지표
    rsi        = info.get("RSI14")
    golden     = info.get("골든크로스")
    above_ma20 = info.get("MA20_위")
    frgn       = info.get("외국인연속순매수", 0)

    ma_tag  = "MA20 위 ✅" if above_ma20 else "MA20 아래 ⚠️"
    rsi_tag = ""
    if rsi is not None:
        rsi_label = "(과열)" if rsi >= 70 else "(과매도)" if rsi <= 30 else "(적정)"
        rsi_tag = f"RSI {rsi:.0f} {rsi_label}"
    gc_tag = "골든크로스 ✅" if golden else ("데드크로스 ⚠️" if golden is False else "")

    tech_parts = [ma_tag]
    if rsi_tag:
        tech_parts.append(rsi_tag)
    if gc_tag:
        tech_parts.append(gc_tag)

    # 추천 이력
    h = reco_history.get(code)
    reco_str = ""
    if h:
        dates_str = ", ".join(d[5:] for d in h["dates"][-5:])
        reco_str = (
            f"  추천 이력: {h['count']}일 추천 / 연속 {h['streak']}일 "
            f"({dates_str}) / 점수 {h['score_trend']}"
        )

    lines = [
        f"【{name} ({code})】",
        f"  매수가: {buy_px:,}원  수량: {qty}주  비중: {weight:.0f}%",
        f"  종가: {cur_price:,}원 ({pnl_pct:+.1f}%)",
        f"  기술: {' / '.join(tech_parts)}",
        f"  펀더: {fund.verdict} {fund.score}점",
    ]
    if fund.flags:
        lines.append(f"    긍정 — {' | '.join(fund.flags)}")
    if fund.warnings:
        lines.append(f"    경고 — {' | '.join(fund.warnings)}")
    if fund.fails:
        lines.append(f"    탈락 — {' | '.join(fund.fails)}")
    if frgn > 0:
        lines.append(f"  외국인 연속순매수: {frgn}일")
    if reco_str:
        lines.append(reco_str)

    return "\n".join(lines)


# ── Gemini 호출 ──────────────────────────────────────────────────

def _call_gemini(prompt: str) -> str:
    """Gemini 스트리밍 호출. 전체 텍스트 반환."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    system = (
        "당신은 한국 주식 스윙 트레이딩 전문가입니다. "
        "보유 종목 데이터를 분석해 내일 전략을 간결하게 제시하세요. "
        "한국어로, 실전에 바로 쓸 수 있는 구체적인 조언을 주세요. "
        "손절 -5%, 트레일링 -3%, 목표 +10% 기준으로 판단하세요."
    )
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS, 1):
        if delay:
            print(f"\nGemini 재시도 ({attempt}/{len(_RETRY_DELAYS)+1}), {delay}초 대기...")
            time.sleep(delay)
        try:
            parts: list[str] = []
            for chunk in client.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=8192,
                    temperature=0,
                ),
            ):
                if chunk.text:
                    print(chunk.text, end="", flush=True)
                    parts.append(chunk.text)
            return "".join(parts)
        except Exception as exc:
            last_exc = exc
            if "503" not in str(exc) and "ServerError" not in type(exc).__name__:
                raise
    raise last_exc


# ── 메시지 분할 / 전송 ───────────────────────────────────────────

def _split_by_lines(msg: str) -> list[str]:
    """🔍 마커 없는 메시지의 fallback 줄 단위 분할."""
    parts: list[str] = []
    current: list[str] = []
    length = 0
    for line in msg.splitlines(keepends=True):
        if length + len(line) > SPLIT_THRESHOLD and current:
            parts.append("".join(current))
            current = []
            length = 0
        current.append(line)
        length += len(line)
    if current:
        parts.append("".join(current))
    return parts or [msg]


def _split_analysis_messages(msg: str) -> list[str]:
    """종목 단위(🔍 기준)로 분할. 헤더→첫 메시지, 푸터→마지막 메시지.

    분할 결과가 1개면 [1/1] 태그 없이 그대로 반환.
    """
    lines = msg.splitlines(keepends=True)

    # 헤더 / 종목 블록 / 푸터 파싱
    header_lines: list[str] = []
    stock_blocks: list[list[str]] = []
    footer_lines: list[str] = []

    current_block: list[str] | None = None
    footer_started = False

    for line in lines:
        stripped = line.lstrip()
        if footer_started:
            footer_lines.append(line)
        elif stripped.startswith("🔍"):
            if current_block is not None:
                stock_blocks.append(current_block)
            current_block = [line]
        elif stripped.startswith("⚠️ 종합 권장") or stripped.startswith("⚠️ 종합"):
            if current_block is not None:
                stock_blocks.append(current_block)
                current_block = None
            footer_started = True
            footer_lines.append(line)
        elif current_block is not None:
            current_block.append(line)
        else:
            header_lines.append(line)

    if current_block is not None:
        stock_blocks.append(current_block)

    # 🔍 마커가 없으면 줄 단위 fallback
    if not stock_blocks:
        return _split_by_lines(msg)

    header = "".join(header_lines)
    footer = "".join(footer_lines)

    # 종목 블록을 메시지 단위로 묶기
    messages: list[str] = []
    current_parts: list[str] = [header]
    current_len = len(header)

    for block in stock_blocks:
        block_text = "".join(block)
        needed = len(block_text)
        if current_len + needed > SPLIT_THRESHOLD and len(current_parts) > 1:
            messages.append("".join(current_parts))
            current_parts = [block_text]
            current_len = needed
        else:
            current_parts.append(block_text)
            current_len += needed

    # 마지막 묶음에 푸터 추가
    if footer:
        last_candidate = "".join(current_parts) + footer
        if len(last_candidate) > SPLIT_THRESHOLD and current_parts:
            messages.append("".join(current_parts))
            messages.append(footer)
        else:
            current_parts.append(footer)
            messages.append("".join(current_parts))
    else:
        messages.append("".join(current_parts))

    # 단일 메시지면 태그 없이 반환
    if len(messages) == 1:
        return messages

    # [i/N] 태그를 첫 줄에 삽입
    total = len(messages)
    tagged: list[str] = []
    for i, part in enumerate(messages, 1):
        first_newline = part.find("\n")
        if first_newline == -1:
            tagged.append(f"[{i}/{total}] {part}")
        else:
            tagged.append(f"[{i}/{total}] " + part[:first_newline] + part[first_newline:])
    return tagged


def _send_multi_message(messages: list[str], dry_run: bool = False) -> None:
    """메시지 목록을 순서대로 전송. 각 사이 1초 sleep, 실패 시 3회 재시도."""
    total = len(messages)
    print(f"[분할] 총 {total}개 메시지")
    for i, part in enumerate(messages, 1):
        print(f"[발송 {i}/{total}] {len(part):,}자")
        if dry_run:
            print(part)
            print("---")
        else:
            for attempt in range(1, 4):
                try:
                    send_message(part)
                    break
                except Exception as exc:
                    if attempt == 3:
                        print(f"  [경고] 메시지 {i}/{total} 전송 실패 (3회 재시도 초과): {exc}")
                    else:
                        print(f"  [재시도 {attempt}/3] {exc}")
                        time.sleep(2)
        if i < total:
            time.sleep(1)


# ── 메인 ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="보유 종목 일간 평가")
    parser.add_argument("--dry-run", action="store_true", help="텔레그램 전송 없이 콘솔 출력만")
    args = parser.parse_args()
    dry_run: bool = args.dry_run

    sys.stdout.reconfigure(encoding="utf-8")

    pos_data   = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    seed_money = pos_data.get("seed_money", 0)
    open_pos   = [p for p in pos_data["positions"] if p.get("status") in ("OPEN", "HOLDING")]

    if not open_pos:
        print("보유 종목 없음 → 스킵")
        return

    print(f"[보유 평가] {TODAY} — {len(open_pos)}개 종목")

    # 시장 컨텍스트
    print("\n[시장] 시장 컨텍스트 조회...")
    try:
        market = get_market_context()
        k, q = market["kospi"], market["kosdaq"]
        print(f"  코스피 {k['change']:+.1f}% ({k['trend']}) | 코스닥 {q['change']:+.1f}% ({q['trend']})")
    except Exception as exc:
        print(f"  시장 조회 실패: {exc}")
        market = {
            "kospi":  {"price": 0, "change": 0.0, "trend": "unknown"},
            "kosdaq": {"price": 0, "change": 0.0, "trend": "unknown"},
            "trend_summary": "시장 정보 조회 실패",
        }
        k, q = market["kospi"], market["kosdaq"]

    # 추천 이력
    reco_history = analyze_recommendations()

    # 종목별 데이터 수집
    holding_blocks: list[str] = []
    position_summaries: list[str] = []

    for pos in open_pos:
        code = pos["code"]
        name = pos.get("name", code)
        buy_px = pos["buy_price"]
        qty    = pos["qty"]
        print(f"\n[{name}({code})] 데이터 수집 중...")

        try:
            info  = get_stock_info(code)
            fund  = evaluate_fundamentals(code, info)
            block = _build_holding_block(pos, info, fund, reco_history, seed_money)
            holding_blocks.append(block)

            cur_price = _parse_price(info.get("현재가", "")) or buy_px
            pnl_pct   = (cur_price - buy_px) / buy_px * 100 if buy_px else 0.0
            weight    = buy_px * qty / seed_money * 100 if seed_money else 0.0
            rsi       = info.get("RSI14")
            rsi_str   = f"RSI {rsi:.0f}" if rsi else ""
            ma_str    = "MA20↑" if info.get("MA20_위") else "MA20↓"

            print(f"  종가: {cur_price:,}원 ({pnl_pct:+.1f}%) | 펀더: {fund.verdict} {fund.score}점 | {ma_str} {rsi_str}")

            # 텔레그램용 짧은 요약 (Gemini 분석 실패 시 fallback)
            position_summaries.append(
                f"• {name}: {buy_px:,}→{cur_price:,}원 ({pnl_pct:+.1f}%) "
                f"{qty}주 {weight:.0f}% | 펀더 {fund.score}점 {ma_str}"
            )
        except Exception as exc:
            print(f"  [{code}] 데이터 수집 실패: {exc}")
            holding_blocks.append(f"【{name} ({code})】\n  데이터 조회 실패: {exc}")
            position_summaries.append(f"• {name}: 데이터 조회 실패")

    # Gemini 프롬프트 구성
    prompt = (
        f"오늘({TODAY}) 장 마감 후 보유 종목 평가입니다.\n\n"
        f"=== 시장 컨텍스트 ===\n"
        f"코스피: {k['price']}pt ({k['change']:+.1f}%, {k['trend']})\n"
        f"코스닥: {q['price']}pt ({q['change']:+.1f}%, {q['trend']})\n"
        f"{market['trend_summary']}\n\n"
        f"=== 보유 종목 ({len(open_pos)}개) ===\n"
        + "\n\n".join(holding_blocks)
        + "\n\n"
        "위 데이터를 바탕으로 내일 전략을 아래 형식으로 작성하세요:\n\n"
        "각 종목:\n"
        "🔍 종목명 (수량, 비중%)\n"
        "   매수: X원 → 종가: Y원 (±Z%)\n"
        "   기술/펀더 핵심 1줄\n"
        "   🎯 내일 권장: 보유 / 매도 / 추가매수\n"
        "   💡 이유: 한 줄\n"
        "   ⚠️ 주의: 리스크 또는 기회 한 줄 (없으면 생략)\n\n"
        "마지막:\n"
        "⚠️ 종합 권장:\n"
        "- 비중 관리 / 신규 매수 가능 자금 / 내일 주목할 점 2-3줄"
    )

    # Gemini 분석
    print(f"\n[Gemini] 내일 전략 생성 중...\n{'='*55}")
    analysis = ""
    try:
        if GEMINI_API_KEY:
            analysis = _call_gemini(prompt)
            print(f"\n{'='*55}")
        else:
            print("GEMINI_API_KEY 없음 → 분석 생략")
    except Exception as exc:
        print(f"\nGemini 분석 실패: {exc}")

    # 텔레그램 메시지 구성
    if analysis.strip():
        msg = f"📊 [내일 보유 전략] {TODAY}\n\n" + analysis
    else:
        msg = (
            f"📊 [내일 보유 전략] {TODAY}\n"
            f"(AI 분석 실패 — 데이터 요약)\n\n"
            + "\n".join(position_summaries)
            + f"\n\n시장: {market['trend_summary']}"
        )

    messages = _split_analysis_messages(msg)
    _send_multi_message(messages, dry_run=dry_run)

    LAST_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_REPORT_PATH.write_text(msg, encoding="utf-8")
    print(f"[완료] last_report.txt 저장 ({len(msg):,}자)")


if __name__ == "__main__":
    main()
