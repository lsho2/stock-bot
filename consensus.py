#!/usr/bin/env python3
"""
한경 컨센서스 리포트 자동 분석
- consensus.hankyung.com 오늘 리포트 수집 (분류: 시장, 산업)
- PDF 다운로드 → pymupdf 텍스트 추출 → Gemini API 분석
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import fitz  # pymupdf
from google import genai
from google.genai import types
from dotenv import load_dotenv
from stock_info import get_stock_info
from telegram_alert import send_message

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LOG_DIR = Path(r"C:\Stock\stock-bot")
PDF_CACHE_DIR = Path(r"C:\Stock\hankyungpdf")

BASE_URL = "https://consensus.hankyung.com"
LIST_URL = f"{BASE_URL}/analysis/list"
TARGET_CATEGORIES = {"시장", "산업", "기업"}
TODAY = datetime.now().strftime("%Y.%m.%d")
TODAY_PARAM = datetime.now().strftime("%Y-%m-%d")
MAX_CHARS_PER_PDF = 8_000
MAX_TOTAL_CHARS = 50_000

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


_SYSTEM = (
    "당신은 10년 경력의 한국 주식시장 전문 애널리스트입니다. "
    "오늘 발행된 증권사 리서치 리포트를 종합해 실전 투자에 바로 활용 가능한 인사이트를 제공합니다. "
    "투자자의 성향은 중장기(3~6개월) 보유, 기업 성장성 기반 투자이며 단타는 하지 않습니다. "
    "목표 수익률은 월 2%, 연 10% 수준입니다. "
    "근거 없는 추측은 하지 않으며, 리포트에 언급된 내용만을 바탕으로 답변합니다. "
    "한국어로 답변하세요."
)


def _first_pass(client: genai.Client, reports_text: str) -> str:
    """1차 분석: 추천 종목 선별 (종목코드 추출용, 비스트리밍)."""
    prompt = (
        f"아래는 {TODAY} 발행된 시장·산업·기업 분류 증권사 리서치 리포트입니다.\n\n"
        f"{reports_text}\n\n"
        "리포트를 분석하여 중장기(3~6개월) 보유에 적합한 추천 종목 TOP5를 선정하세요.\n"
        "단기 트레이딩 성격의 종목은 제외하고, 기업 성장성이 뒷받침되는 종목만 선정하세요.\n\n"
        "각 종목은 반드시 아래 형식으로 작성하세요 (종목코드 필수):\n"
        "종목명(종목코드) — 예: 삼성전자(005930)\n"
        "- 추천 근거:\n"
        "- 목표가: (언급된 경우)\n"
        "- 리스크:\n"
    )
    resp = client.models.generate_content(
        model="gemini-2.5-pro",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            max_output_tokens=8192,
        ),
    )
    return resp.text or ""


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


def _format_stock_infos(stock_infos: dict[str, dict]) -> str:
    """종목 기업정보 dict를 프롬프트용 텍스트로 변환."""
    lines = []
    for code, info in stock_infos.items():
        name = info.get("종목명", code)
        lines.append(f"【{name} ({code})】")
        for k, v in info.items():
            if k not in ("종목코드", "종목명") and v:
                lines.append(f"  {k}: {v}")
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
        if "추천 종목" in key:
            lines.append("💎 추천 종목 TOP5")
            # 종목 번호(**1. 종목명 (코드)**) 단위로 분리
            items = re.split(r"\*\*\d+\.", val)
            for i, item in enumerate(items[1:6], 1):
                name_part = item.split("\n")[0].rstrip("*").strip()
                price_m = re.search(
                    r"현재가:\s*([^\s/]+).*?목표가:\s*([^\s/]+).*?upside:\s*([^\n,]+)",
                    item,
                )
                if price_m:
                    cur, tgt, upside = (x.strip() for x in price_m.groups())
                    lines.append(f"{i}. {name_part}  {cur}→{tgt} ({upside})")
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

    # 📅 다음 주 이벤트 (최대 5줄)
    for key, val in sections.items():
        if "이벤트" in key:
            summary = [l for l in _plain(val).split("\n") if l.strip()][:5]
            lines.append("📅 다음 주 이벤트")
            lines.extend(summary)
            break

    return _plain("\n".join(lines))


def _second_pass(
    client: genai.Client,
    reports_text: str,
    first_result: str,
    stock_infos: dict[str, dict],
) -> str:
    """2차 최종 분석: 기업정보 포함, 스트리밍 출력. 전체 텍스트 반환."""
    info_block = _format_stock_infos(stock_infos) if stock_infos else "조회된 기업정보 없음"

    prompt = (
        f"아래는 {TODAY} 발행된 시장·산업·기업 분류 증권사 리서치 리포트와\n"
        "1차 분석에서 선별된 추천 종목의 실시간 기업정보입니다.\n\n"
        f"=== 리서치 리포트 ===\n{reports_text}\n\n"
        f"=== 1차 추천 종목 선별 결과 ===\n{first_result}\n\n"
        f"=== 추천 종목 실시간 기업정보 ===\n{info_block}\n\n"
        "위 모든 정보를 종합하여 최종 투자 분석 결과를 아래 형식으로 작성하세요.\n"
        "투자자 성향: 중장기(3~6개월) 보유, 기업 성장성 기반, 목표 수익률 연 10%.\n\n"
        "## 📊 오늘의 시장 요약\n"
        "오늘 시장 전반의 핵심 흐름을 2~3줄로 요약하세요.\n\n"
        "## 🌱 중장기 성장 가능 섹터 TOP3\n"
        "각 섹터마다 아래 형식으로 작성하세요:\n"
        "**1. [섹터명]**\n"
        "- 💡 성장 근거:\n"
        "- ⚠️ 리스크:\n\n"
        "## 💎 3~6개월 보유 추천 종목 TOP5\n"
        "실시간 기업정보(현재가, 목표주가, PER, PBR 등)를 반영하여 작성하세요.\n"
        "각 종목마다 아래 형식으로 작성하세요:\n"
        "**1. 종목명 (종목코드)**\n"
        "- 📈 기업 성장성 근거:\n"
        "- 💰 현재가: / 🎯 목표가: / 📊 upside:\n"
        "- 📉 PER: / PBR: / 외국인보유율:\n"
        "- ⚠️ 리스크:\n\n"
        "## 🚫 피해야 할 종목/섹터\n"
        "리포트에서 부정적으로 언급되거나, 중장기 성장성이 불투명한 섹터·종목과 그 이유를 작성하세요.\n\n"
        "## 📅 다음 주 주목 이벤트\n"
        "중장기 투자 관점에서 주가에 영향을 줄 수 있는 경제지표, 실적 발표, 정책 이벤트 등을 나열하세요.\n"
    )

    parts: list[str] = []
    for chunk in client.models.generate_content_stream(
        model="gemini-2.5-pro",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            max_output_tokens=8192,
        ),
    ):
        if chunk.text:
            print(chunk.text, end="", flush=True)
            parts.append(chunk.text)
    return "".join(parts)


def analyze_with_gemini(reports_text: str) -> None:
    """2패스 Gemini 분석: 1차 종목 선별 → 기업정보 조회 → 2차 최종 분석."""
    client = genai.Client(api_key=GEMINI_API_KEY)

    print("[4/6] AI 1차 분석: 추천 종목 선별 중...", end="", flush=True)
    first_result = _first_pass(client, reports_text)

    if not first_result:
        print(" → 응답 없음 (2차 분석 건너뜀)")
        return

    codes = _extract_stock_codes(first_result)
    code_display = ", ".join(codes) if codes else "없음"
    print(f" → 선별 완료: {code_display}")

    stock_infos: dict[str, dict] = {}
    if codes:
        print(f"[5/6] 기업정보 조회 중...")
        for code in codes:
            stock_infos[code] = get_stock_info(code)

        SHOW_KEYS = ["현재가", "목표주가", "PER", "PBR", "외국인보유율", "시가총액", "52주최고", "52주최저", "배당수익률"]
        for code, info in stock_infos.items():
            print(f"\n  [{info.get('종목명', code)} ({code})]")
            for k in SHOW_KEYS:
                if info.get(k):
                    print(f"    {k}: {info[k]}")
    else:
        print("[5/6] 기업정보 조회 건너뜀 (종목코드 없음)")

    print("[6/6] AI 2차 분석: 최종 리포트 생성 중...\n")
    print("=" * 60)
    full_text = _second_pass(client, reports_text, first_result, stock_infos)
    print("\n" + "=" * 60)

    if not full_text:
        return

    # 결과 파일 저장 (send_report.bat 에서도 재사용 가능)
    report_path = LOG_DIR / "last_report.txt"
    telegram_msg = _build_telegram_message(full_text)
    report_path.write_text(telegram_msg, encoding="utf-8")

    print("\n[텔레그램] 전송 중...")
    ok = send_message(telegram_msg)
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

    analyze_with_gemini(combined)


if __name__ == "__main__":
    main()
