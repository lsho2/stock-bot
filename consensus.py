#!/usr/bin/env python3
"""
한경 컨센서스 리포트 자동 분석
- consensus.hankyung.com 오늘 리포트 수집 (분류: 시장, 산업)
- PDF 다운로드 → pymupdf 텍스트 추출 → Gemini API 분석
"""

import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import fitz  # pymupdf
from google import genai
from google.genai import types
from dotenv import load_dotenv
from stock_info import get_stock_info, get_intraday_info  # get_intraday_info: standalone용
from telegram_alert import send_broadcast
from volume_surge import get_volume_surge
from fundamental_filter import evaluate_fundamentals, FundamentalResult
from tracker import log_recommendation, analyze_recommendations
from market_context import get_market_context

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LOG_DIR = Path(r"C:\Stock\stock-bot")
PDF_CACHE_DIR           = Path(r"C:\Stock\hankyungpdf")
POSITIONS_PATH          = Path(r"C:\Stock\stock-bot\positions.json")
TRADES_PATH             = Path(r"C:\Stock\data\trades.json")
STOP_LOSS_COOLDOWN_DAYS = 3

BASE_URL = "https://consensus.hankyung.com"
LIST_URL = f"{BASE_URL}/analysis/list"
TARGET_CATEGORIES = {"시장", "산업", "기업"}
TODAY = datetime.now().strftime("%Y.%m.%d")
TODAY_PARAM = datetime.now().strftime("%Y-%m-%d")
MAX_CHARS_PER_PDF = 8_000
MAX_TOTAL_CHARS = 50_000
_RETRY_DELAYS = [10, 20, 40, 80, 120]

http = requests.Session()
http.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BASE_URL,
})


def get_today_reports() -> list[dict]:
    """오늘 날짜의 시장·산업·기업 분류 리포트 목록을 반환."""
    print(f"[1/6] 리포트 수집 중...", end="", flush=True)

    params = {
        "sdate": TODAY_PARAM,
        "edate": TODAY_PARAM,
        "now_page": 1,
        "pagenum": 80,
    }

    try:
        resp = http.get(LIST_URL, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f" 실패: {exc}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table tbody tr")

    reports: list[dict] = []
    for row in rows:
        tds = row.select("td")
        if len(tds) < 6:
            continue

        category = tds[1].get_text(strip=True)
        if not any(t in category for t in TARGET_CATEGORIES):
            continue

        title_el = row.select_one("td.text_l a")
        pdf_el = row.select_one("div.dv_input a")

        if not title_el or not pdf_el:
            continue

        href = pdf_el.get("href", "")
        if not href:
            continue

        reports.append({
            "title": title_el.get_text(strip=True),
            "category": category,
            "pdf_url": urljoin(BASE_URL, href),
        })

    print(f" → 완료 ({len(reports)}개)")
    return reports


def download_pdf(url: str, dest: Path) -> bool:
    """PDF 다운로드. 성공 시 True."""
    try:
        resp = http.get(url, timeout=30, stream=True)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return True
    except requests.RequestException as exc:
        print(f"  다운로드 실패 ({url}): {exc}")
        return False


def extract_text(pdf_path: Path, max_chars: int = MAX_CHARS_PER_PDF) -> str:
    """pymupdf로 PDF 텍스트 추출."""
    try:
        doc = fitz.open(str(pdf_path))
        pages_text = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(pages_text)[:max_chars]
    except Exception as exc:
        print(f"  텍스트 추출 실패 ({pdf_path.name}): {exc}")
        return ""


def _load_system_prompt() -> str:
    base = (
        "당신은 10년 경력의 한국 주식시장 전문 애널리스트입니다.\n"
        "오늘 발행된 증권사 리서치 리포트, 실시간 기업정보, 기술적 지표, 거래량 급등 데이터를 종합해 "
        "실전 스윙 매매에 활용 가능한 인사이트를 제공합니다.\n\n"
        "투자자 성향: 스윙 트레이딩\n"
        "- 보유 기간: 며칠 ~ 몇 주 (1~4주)\n"
        "- 목표 수익: 종목당 5~15%\n"
        "- 손절 기준: -5%\n"
        "- 기업의 단기 모멘텀, 수급, 차트 기술적 신호를 종합 판단\n"
        "- 단순 단타(당일 매매)는 아님\n\n"
        "분석 시 다음 원칙을 따르세요:\n"
        "- 확률 높은 지점에서 저점을 잡는다 (큰 추세 상승 후 좁은 박스권 저점 포착)\n"
        "- 추세를 따른다 (역추세 거래 금지)\n"
        "- 거래량 신뢰 조건: 60일 평균 대비 3배 이상 + 장대봉 + 지지/저항 돌파 동시 충족\n"
        "- MA20 위에서 진입, 200일선(MA60) 아래 종목은 보유 금지\n"
        "- 다중 컨펌 후 진입: 단일 지표만으로 진입 결정 금지\n"
        "- RSI 다이버전스, 거래량 변화, 외국인 연속순매수 등 여러 신호 종합\n"
        "- 손절가는 진입 근거가 무효화되는 지점\n\n"
        "근거 없는 추측 금지. 리포트/기업정보/기술적 지표에 명시된 내용만 사용.\n"
        "전체 트레이딩 원칙 파일이 별도로 로드되어 있으면 함께 참고하세요.\n"
        "한국어로 답변하세요."
    )
    principles_path = Path(r"C:\Stock\stock-bot\data\principles\trading_principles.txt")
    if principles_path.exists():
        try:
            principles = principles_path.read_text(encoding="utf-8")
            print(f"트레이딩 원칙 로드 완료 ({len(principles)}자)")
            return base + "\n\n=== 트레이딩 원칙 전문 ===\n" + principles
        except Exception as exc:
            print(f"  트레이딩 원칙 로드 실패: {exc}")
    else:
        print("  트레이딩 원칙 파일 없음 (스킵)")
    return base

_SYSTEM = _load_system_prompt()


def _first_pass(client: genai.Client, reports_text: str) -> str:
    """1차 분석: 추천 종목 선별 (종목코드 추출용, 비스트리밍)."""
    prompt = (
        f"아래는 {TODAY} 발행된 시장·산업·기업 분류 증권사 리서치 리포트입니다.\n\n"
        f"{reports_text}\n\n"
        "리포트를 분석하여 스윙 트레이딩(1~4주 보유)에 적합한 추천 종목 TOP5를 선정하세요.\n"
        "단기 모멘텀, 수급 변화, 차트 기술적 신호가 뒷받침되는 종목 위주로 선정하세요.\n"
        "초단타(당일 매매) 성격의 종목은 제외하세요.\n\n"
        "각 종목은 반드시 아래 형식으로 작성하세요 (종목코드 필수):\n"
        "종목명(종목코드) — 예: 삼성전자(005930)\n"
        "- 추천 근거:\n"
        "- 목표가: (언급된 경우)\n"
        "- 리스크:\n"
    )
    last_exc: Exception | None = None
    max_attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    max_output_tokens=16384,
                    temperature=0,
                ),
            )
            return resp.text or ""
        except Exception as exc:
            if "503" not in str(exc) and "ServerError" not in type(exc).__name__:
                raise
            last_exc = exc
            if attempt <= len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[attempt - 1]
                print(f"\nGemini 503 ({attempt}/5), {delay}초 후 재시도...")
                time.sleep(delay)
    raise last_exc


def _extract_stock_codes(text: str | None) -> list[str]:
    """텍스트에서 6자리 종목코드 추출 (중복 제거, 순서 유지)."""
    if not text:
        return []
    codes = re.findall(r'\((\d{6})\)', text)
    seen: set[str] = set()
    result = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def filter_owned_and_recent_losses(
    codes: list[str],
) -> tuple[list[str], list[tuple[str, str, str]], str]:
    """보유 중·최근 손절 종목을 추천 후보에서 제외.

    Returns:
        filtered_codes  — 제외 후 남은 코드 목록
        excluded        — [(code, name, reason), ...]
        exclusion_note  — Gemini 2차 프롬프트용 섹션 텍스트
    """
    # ── 현재 보유 종목 (OPEN) ────────────────────────────────────────
    owned: list[tuple[str, str]] = []
    try:
        pos_data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        owned = [
            (p["code"], p.get("name", p["code"]))
            for p in pos_data.get("positions", [])
            if p.get("status") == "OPEN"
        ]
    except Exception:
        pass

    # ── 최근 N일 내 손절 종목 ────────────────────────────────────────
    recent_losses: list[tuple[str, str]] = []
    try:
        trd_data = json.loads(TRADES_PATH.read_text(encoding="utf-8"))
        cutoff   = datetime.now() - timedelta(days=STOP_LOSS_COOLDOWN_DAYS)
        seen_loss: set[str] = set()
        for t in trd_data.get("trades", []):
            code = t.get("code", "")
            if (
                code
                and code not in seen_loss
                and t.get("type") == "SELL"
                and t.get("reason") == "stop_loss"
            ):
                try:
                    if datetime.strptime(t["date"], "%Y-%m-%d") >= cutoff:
                        recent_losses.append((code, t.get("name", code)))
                        seen_loss.add(code)
                except (KeyError, ValueError):
                    pass
    except Exception:
        pass

    # ── 필터링 ──────────────────────────────────────────────────────
    owned_codes = {c for c, _ in owned}
    loss_codes  = {c for c, _ in recent_losses}
    name_map    = dict(owned + recent_losses)

    filtered: list[str] = []
    excluded: list[tuple[str, str, str]] = []
    for code in codes:
        name = name_map.get(code, code)
        if code in owned_codes:
            excluded.append((code, name, "보유 중"))
        elif code in loss_codes:
            excluded.append((code, name, f"최근 {STOP_LOSS_COOLDOWN_DAYS}일 내 손절"))
        else:
            filtered.append(code)

    # ── Gemini 프롬프트용 제외 규칙 텍스트 ──────────────────────────
    excl_items = (
        [f"{n}({c}) [보유]" for c, n in owned]
        + [f"{n}({c}) [최근손절]" for c, n in recent_losses]
    )
    note = (
        f"=== 추천 제외 규칙 ===\n"
        f"- 현재 보유 중인 종목은 추천하지 마세요 (중복 매수 금지)\n"
        f"- 최근 {STOP_LOSS_COOLDOWN_DAYS}영업일 내 손절한 종목은 추천하지 마세요 (감정 매매 방지)\n"
    )
    if excl_items:
        note += f"- 제외 대상: {', '.join(excl_items)}\n"

    return filtered, excluded, note


def _format_stock_infos(
    stock_infos: dict[str, dict],
    fundamentals: dict[str, "FundamentalResult"] | None = None,
    intraday_infos: dict[str, dict] | None = None,
) -> str:
    """종목 기업정보 dict를 프롬프트용 텍스트로 변환."""
    lines = []
    for code, info in stock_infos.items():
        name = info.get("종목명", code)
        lines.append(f"【{name} ({code})】")
        for k, v in info.items():
            if k not in ("종목코드", "종목명") and v:
                lines.append(f"  {k}: {v}")
        if fundamentals and code in fundamentals:
            lines.append(f"  {fundamentals[code].to_prompt_block()}")
        if intraday_infos and code in intraday_infos:
            iday = intraday_infos[code]
            lines.append("  [장중]")
            for k in ("등락률",):  # 시가/고가/저가/현재가는 상단 루프에서 이미 포함됨
                if iday.get(k):
                    lines.append(f"    {k}: {iday[k]}")
    return "\n".join(lines)


def _build_telegram_message(analysis: str) -> str:
    """전체 분석 텍스트 → 텔레그램 요약 메시지 생성."""
    today_str = datetime.now().strftime("%Y년 %m월 %d일")
    lines = [f"📊 오늘의 주식 브리핑 ({today_str})\n"]

    # ## 섹션별로 분리
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_buf: list[str] = []
    for line in analysis.split("\n"):
        if line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_buf).strip()
            current_key = line[3:].strip()
            current_buf = []
        else:
            current_buf.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_buf).strip()

    def _plain(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"\*(.+?)\*", r"\1", text)
        return re.sub(r"\*+", "", text)

    # 📈 시장 요약 (최대 3줄)
    for key, val in sections.items():
        if "시장 요약" in key or "시장" in key:
            summary = [l for l in _plain(val).split("\n") if l.strip()][:3]
            lines.append("📈 시장 요약")
            lines.extend(summary)
            lines.append("")
            break

    # 💎 추천 종목 TOP5
    for key, val in sections.items():
        if "보유 추천 종목" in key:
            lines.append("💎 추천 종목 TOP5")
            # 종목 번호(**1. 종목명 (코드)**) 단위로 분리
            items = re.split(r"\*\*\d+\.", val)
            for i, item in enumerate(items[1:6], 1):
                name_part = item.split("\n")[0].rstrip("*").strip()
                cur_m = re.search(r"현재가:\s*([\d,]+원)", item)
                tgt_m = re.search(r"목표가:\s*([\d,]+원)", item)
                up_m = re.search(r"upside:\s*([+\-\d.]+%)", item)
                if cur_m and tgt_m and up_m:
                    lines.append(f"{i}. {name_part}  {cur_m.group(1)}→{tgt_m.group(1)} ({up_m.group(1)})")
                else:
                    lines.append(f"{i}. {name_part}")
            lines.append("")
            break

    # 🚫 피해야 할 섹터 (최대 4줄)
    for key, val in sections.items():
        if "피해야" in key:
            summary = [l for l in _plain(val).split("\n") if l.strip()][:4]
            lines.append("🚫 피해야 할 섹터")
            lines.extend(summary)
            lines.append("")
            break

    # 🔥 거래량 급등 종목 (최대 3줄)
    for key, val in sections.items():
        if "거래량 급등" in key:
            lines.append("🔥 거래량 급등 종목")
            summary = [l for l in _plain(val).split("\n") if l.strip()][:3]
            lines.extend(summary)
            lines.append("")
            break

    # 📅 이번 주 이벤트 (최대 5줄)
    for key, val in sections.items():
        if "이벤트" in key:
            summary = [l for l in _plain(val).split("\n") if l.strip()][:5]
            lines.append("📅 이번 주 이벤트")
            lines.extend(summary)
            break

    return _plain("\n".join(lines))


def _second_pass(
    client,
    reports_text: str,
    first_result: str,
    stock_infos: dict,
    surge_stocks: list,
    fundamentals: dict[str, "FundamentalResult"] | None = None,
    is_intraday: bool = False,
    intraday_infos: dict[str, dict] | None = None,
    exclusion_note: str = "",
    reco_note: str = "",
    market_note: str = "",
) -> str:
    """2차 최종 분석: 기업정보+펀더멘털+거래량 급등 포함, 스트리밍 출력. 전체 텍스트 반환."""
    def _format_surge(stocks):
        if not stocks:
            return "조회된 거래량 급등 종목 없음"
        lines = []
        for s in stocks:
            lines.append(
                f"- {s['종목명']}({s['종목코드']}): "
                f"{s['배율']}배, {s['등락률']:+.1f}%, 현재가 {s['현재가']:,}원"
            )
        return "\n".join(lines)

    info_block = _format_stock_infos(stock_infos, fundamentals, intraday_infos) if stock_infos else "조회된 기업정보 없음"

    # 펀더멘털 FAIL 종목 목록 (Gemini 지시용)
    fail_codes = [
        f"{stock_infos.get(c, {}).get('종목명', c)}({c})"
        for c, f in (fundamentals or {}).items()
        if f.verdict == "FAIL"
    ]
    fail_note = (
        f"\n※ 펀더멘털 FAIL 종목: {', '.join(fail_codes)} — TOP5 추천 절대 금지, 관심 종목 언급만 허용.\n"
        if fail_codes else ""
    )

    prompt = (
        f"아래는 {TODAY} 발행된 증권사 리서치 리포트와 추천 종목의 실시간 데이터입니다.\n\n"
        f"=== 리서치 리포트 ===\n{reports_text}\n\n"
        f"=== 1차 추천 종목 선별 결과 ===\n{first_result}\n\n"
        f"=== 추천 종목 실시간 기업정보 + 기술적 지표 + 펀더멘털 평가 ===\n{info_block}\n\n"
        f"=== 오늘 거래량 급등 종목 ===\n{_format_surge(surge_stocks)}\n\n"
        f"{market_note}\n"
        f"{exclusion_note}\n"
        f"{reco_note}\n"
        f"{fail_note}"
        "위 모든 정보를 종합하여 스윙 트레이딩 관점에서 최종 분석 결과를 아래 형식으로 작성하세요.\n"
        "펀더멘털 FAIL 종목(적자/ROE 음수/영업CF 음수)은 TOP5에서 제외하세요.\n"
        "투자자 성향: 스윙 트레이딩 (1~4주 보유), 목표 5~15%, 손절 -5%.\n\n"
        "## 📊 오늘의 시장 요약\n"
        "오늘 시장 전반 흐름을 2~3줄로 요약.\n\n"
        "## 🚀 1~4주 보유 추천 종목 TOP5\n"
        "1차 선별 종목 + 거래량 급등 종목 중 스윙 매매 적합 5개 선정.\n"
        "각 종목 아래 형식:\n\n"
        "**1. 종목명(종목코드)**\n"
        "- 💡 매수 근거: 리포트 모멘텀 + 기술적 신호 + 수급 종합 (한 줄)\n"
        "- 💰 현재가: XXX원 / 🎯 목표가: XXX원 / 📊 upside: +X.X%\n"
        "- 🛡️ 손절가: XXX원 (-5%)\n"
        "- 📐 기술적 신호: MA20 위/아래, 골든크로스 여부, RSI14, 외국인 연속순매수\n"
        "- ⏱️ 예상 보유: X주\n"
        "- ⚠️ 리스크: 한 줄\n\n"
        "## 🔥 거래량 급등 주목 종목\n"
        "리포트엔 없지만 거래량 급등 + 스윙 진입 여력 있는 종목 2개 (각 한 줄):\n"
        "- 종목명(코드): 매수 근거\n\n"
        "## 🚫 피해야 할 종목/섹터\n"
        "스윙 관점에서 진입 금지 종목/섹터 2~3개 (각 한 줄):\n"
        "- 이름: 이유\n\n"
        "## 📅 이번 주 주목 이벤트\n"
        "스윙 보유 기간 동안 영향 줄 이벤트 3개 (각 한 줄):\n"
        "- 이벤트: 영향\n"
    )

    if is_intraday:
        now = datetime.now()
        close_dt = now.replace(hour=15, minute=30, second=0, microsecond=0)
        minutes_left = max(0, int((close_dt - now).total_seconds() // 60))
        prompt += (
            f"\n=== 장중 매매 모드 ===\n"
            f"현재 시각: {now.strftime('%H:%M')}\n"
            f"장 마감까지: {minutes_left}분 남음\n\n"
            "추천 시 다음 추가 룰:\n"
            "1. 일중 등락률 +15% 이상 종목: FOMO 제외, \"관심 종목\"으로만\n"
            "2. 일중 -5% 이상 빠진 종목: 반등 신호 시 매수 검토 (지지선 확인 필수)\n"
            "3. 시가 대비 -1~3% 빠진 종목 (눌림목): 매수 타이밍 양호\n"
            "4. 시가 대비 +5% 이상 갭상승: 추격 매수 금지\n\n"
            "장 마감까지 남은 시간을 보유 기간 권장에 반영.\n"
        )

    last_exc: Exception | None = None
    max_attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(1, max_attempts + 1):
        try:
            parts: list[str] = []
            for chunk in client.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    max_output_tokens=16384,
                    temperature=0,
                ),
            ):
                if chunk.text:
                    print(chunk.text, end="", flush=True)
                    parts.append(chunk.text)
            return "".join(parts)
        except Exception as exc:
            if "503" not in str(exc) and "ServerError" not in type(exc).__name__:
                raise
            last_exc = exc
            if attempt <= len(_RETRY_DELAYS):
                delay = _RETRY_DELAYS[attempt - 1]
                print(f"\nGemini 503 ({attempt}/5), {delay}초 후 재시도...")
                time.sleep(delay)
    raise last_exc


def analyze_with_gemini(reports_text: str, surge_stocks: list[dict]) -> None:
    """2패스 Gemini 분석: 1차 종목 선별 → 기업정보 조회 → 2차 최종 분석."""
    client = genai.Client(api_key=GEMINI_API_KEY)

    _now = datetime.now()
    is_intraday = (_now.weekday() < 5) and (dtime(11, 0) <= _now.time() <= dtime(15, 30))
    print(f"[모드] {'장중 매매' if is_intraday else '일간 추천'} 모드")

    print("[시장] 시장 컨텍스트 조회 중...")
    market_note = ""
    try:
        market = get_market_context()
        k = market["kospi"]
        q = market["kosdaq"]
        print(f"[시장] 코스피 {k['change']:+.1f}% {k['trend']} | 코스닥 {q['change']:+.1f}% {q['trend']}")
        print(f"[시장] {market['trend_summary']}")
        market_note = (
            f"=== 시장 컨텍스트 ===\n"
            f"코스피: {k['price']}pt ({k['change']:+.1f}%, {k['trend']})\n"
            f"코스닥: {q['price']}pt ({q['change']:+.1f}%, {q['trend']})\n"
            f"{market['trend_summary']}\n"
        )
    except Exception as exc:
        print(f"[시장] 컨텍스트 조회 실패 (스킵): {exc}")

    print("[4/6] AI 1차 분석: 추천 종목 선별 중...", end="", flush=True)
    first_result = _first_pass(client, reports_text)

    if not first_result:
        print(" → 응답 없음 (2차 분석 건너뜀)")
        return

    codes = _extract_stock_codes(first_result)
    code_display = ", ".join(codes) if codes else "없음"
    print(f" → 선별 완료: {code_display}")

    # ── 보유/손절 종목 제외 필터 ─────────────────────────────────────
    codes, excluded, exclusion_note = filter_owned_and_recent_losses(codes)
    if excluded:
        print(f"[필터] 보유·손절 제외 {len(excluded)}개:")
        for code, name, reason in excluded:
            print(f"  [제외] {name}({code}): {reason}")
        if codes:
            print(f"  → 통과: {', '.join(codes)}")
        else:
            print("  → 통과 종목 없음 (모두 제외)")

    stock_infos: dict[str, dict] = {}
    fundamentals: dict[str, "FundamentalResult"] = {}
    intraday_infos: dict[str, dict] = {}
    if codes:
        print(f"[5/6] 기업정보 조회 중...")
        for code in codes:
            stock_infos[code] = get_stock_info(code)

        if is_intraday:
            # stock_infos 에 이미 _fetch_kis 가 포함한 OHLC 필드가 있음 → 별도 API 호출 불필요
            intraday_infos = {
                code: {k: info.get(k, "") for k in ("시가", "고가", "저가", "현재가", "등락률")}
                for code, info in stock_infos.items()
            }

        SHOW_KEYS = ["현재가", "목표주가", "PER", "PBR", "외국인보유율", "시가총액", "52주최고", "52주최저", "배당수익률"]
        for code, info in stock_infos.items():
            print(f"\n  [{info.get('종목명', code)} ({code})]")
            for k in SHOW_KEYS:
                if info.get(k):
                    print(f"    {k}: {info[k]}")
            if is_intraday and code in intraday_infos:
                iday = intraday_infos[code]
                s = iday.get("시가") or "-"
                h = iday.get("고가") or "-"
                l = iday.get("저가") or "-"
                print(f"    시가: {s}  고가: {h}  저가: {l}")
                if iday.get("등락률"):
                    print(f"    일중 등락률: {iday['등락률']}")

        print(f"\n[5b/6] 펀더멘털 필터 실행 중...")
        _VERDICT_EMOJI = {"PASS": "✅", "WATCH": "⚠️", "FAIL": "❌"}
        for code, info in stock_infos.items():
            fund = evaluate_fundamentals(code, info)
            fundamentals[code] = fund
            emoji = _VERDICT_EMOJI.get(fund.verdict, "")
            print(f"  [{info.get('종목명', code)}] {emoji} {fund.verdict} ({fund.score}점)")
            if fund.flags:
                print(f"    ✅ {' | '.join(fund.flags)}")
            if fund.warnings:
                print(f"    ⚠️  {' | '.join(fund.warnings)}")
            if fund.fails:
                print(f"    ❌ {' | '.join(fund.fails)}")

        print(f"\n[5c/6] 추천 종목 기록 중...")
        for code, info in stock_infos.items():
            raw_price = info.get("현재가", "0")
            price_int = int(re.sub(r"[^\d]", "", str(raw_price)) or "0")
            log_recommendation(code, info.get("종목명", code), price_int)

        # ── 추천 이력 분석 및 출력 ────────────────────────────────────
        reco_history = analyze_recommendations()
        reco_note_lines = ["=== 추천 이력 ==="]
        for code in stock_infos:
            h = reco_history.get(code)
            if not h:
                continue
            streak  = h["streak"]
            count   = h["count"]
            trend   = h["score_trend"]
            dates_short = [d[5:] for d in h["dates"]]   # MM-DD 형식

            # 강도 배지
            if streak >= 5 and (not h["scores"] or (h["scores"] and h["scores"][-1] >= 70)):
                badge = "🔥 핵심 종목"
            elif streak >= 3:
                badge = "⭐ 강력 추천" if trend in ("rising", "stable") else "⭐"
            else:
                badge = ""

            trend_str = {"rising": "점수 상승↑", "falling": "점수 하락↓", "stable": "점수 유지"}.get(trend, "")
            history_str = ", ".join(dates_short)

            # 콘솔 출력
            badge_disp = f" {badge}" if badge else ""
            print(f"  [{h['name']}({code})] {count}일 추천{badge_disp} | {trend_str} | 이력: {history_str}")

            # Gemini 프롬프트용 한 줄
            note = f"- {h['name']}({code}): {streak}일 연속 추천, {trend_str}"
            if badge:
                note += f" → {badge}"
            reco_note_lines.append(note)

        reco_note = "\n".join(reco_note_lines) if len(reco_note_lines) > 1 else ""
    else:
        print("[5/6] 기업정보 조회 건너뜀 (종목코드 없음)")
        reco_note = ""

    print("[6/6] AI 2차 분석: 최종 리포트 생성 중...\n")
    print("=" * 60)
    full_text = _second_pass(client, reports_text, first_result, stock_infos, surge_stocks, fundamentals, is_intraday, intraday_infos, exclusion_note, reco_note, market_note)
    print("\n" + "=" * 60)

    if not full_text:
        return

    # 결과 파일 저장 (send_report.bat 에서도 재사용 가능)
    analysis_path = LOG_DIR / "last_analysis.txt"
    analysis_path.write_text(full_text, encoding="utf-8")

    report_path = LOG_DIR / "last_report.txt"
    telegram_msg = _build_telegram_message(full_text)
    report_path.write_text(telegram_msg, encoding="utf-8")

    print("\n[텔레그램] 전송 중...")
    ok = send_broadcast(telegram_msg)
    print("[텔레그램] 전송 완료" if ok else "[텔레그램] 전송 실패 (last_report.txt 확인)")


def main() -> None:
    global TODAY, TODAY_PARAM

    if len(sys.argv) > 1:
        date_arg = sys.argv[1]
        try:
            parsed = datetime.strptime(date_arg, "%Y-%m-%d")
        except ValueError:
            sys.exit(f"날짜 형식 오류: '{date_arg}' → YYYY-MM-DD 형식으로 입력하세요.\n예: python consensus.py 2026-05-13")
        TODAY_PARAM = parsed.strftime("%Y-%m-%d")
        TODAY = parsed.strftime("%Y.%m.%d")
        print(f"[날짜 지정] {TODAY}")

    if not GEMINI_API_KEY:
        sys.exit(
            "오류: .env 파일에 GEMINI_API_KEY=your_key 를 추가해 주세요.\n"
            "API 키 발급: https://aistudio.google.com/apikey"
        )

    reports = get_today_reports()
    if not reports:
        sys.exit(f"오늘({TODAY}) 시장·산업 분류 리포트를 찾을 수 없습니다.")

    print("[거래량] 급등 종목 스캔 중...")
    try:
        surge_stocks = get_volume_surge()
    except Exception as exc:
        print(f"  거래량 급등 스캔 실패: {exc}")
        surge_stocks = []

    cache_dir = PDF_CACHE_DIR / TODAY_PARAM
    cache_dir.mkdir(parents=True, exist_ok=True)

    # [2/6] 다운로드
    print(f"[2/6] PDF 다운로드 중...")
    pdf_ready: list[tuple[Path, dict]] = []
    for i, report in enumerate(reports, 1):
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", report["title"])[:50]
        pdf_path = cache_dir / f"{i:02d}_{safe_title}.pdf"
        if pdf_path.exists():
            print(f"  ({i}/{len(reports)}) [캐시] {report['title'][:45]}")
            pdf_ready.append((pdf_path, report))
        else:
            print(f"  ({i}/{len(reports)}) {report['title'][:45]}")
            if download_pdf(report["pdf_url"], pdf_path):
                pdf_ready.append((pdf_path, report))

    # [3/6] 텍스트 추출
    print(f"[3/6] 텍스트 추출 중...", end="", flush=True)
    all_texts: list[str] = []
    for i, (pdf_path, report) in enumerate(pdf_ready, 1):
        text = extract_text(pdf_path)
        if text.strip():
            header = f"【리포트 {i} | {report['category']} | {report['title']}】\n"
            all_texts.append(header + text)
    print(f" → 완료 ({len(all_texts)}개)")

    if not all_texts:
        sys.exit("PDF에서 텍스트를 추출할 수 없습니다.")

    combined = ("\n\n" + "─" * 40 + "\n\n").join(all_texts)
    combined = combined[:MAX_TOTAL_CHARS]

    analyze_with_gemini(combined, surge_stocks)


if __name__ == "__main__":
    main()
