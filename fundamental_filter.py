#!/usr/bin/env python3
"""가치 투자 펀더멘털 필터 — 홍진채 '거인의 어깨' 기준.

평가 항목:
1. PER 음수 (적자 기업) → 즉시 FAIL, 절대 추천 금지
2. ROE: 음수 → FAIL / 30% 이상 우수 / 추세 확인
3. PBR < ROE/10 → 저평가 시그널
4. 부채비율: 200% 이상 경고 / ROE-ROA 괴리 크면 부채 레버리지 의심
5. 매출·영업이익 성장률: 꾸준한 성장 가산점 / 감소 감점
6. 영업현금흐름 < 0 → FAIL (데이터 확보 시)
"""

import re

import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass, field


_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com",
    "Accept-Language": "ko-KR,ko;q=0.9",
})


@dataclass
class FundamentalResult:
    verdict: str = "WATCH"      # "PASS" | "WATCH" | "FAIL"
    score: int = 50             # 0~100
    flags: list[str] = field(default_factory=list)    # 긍정 시그널
    warnings: list[str] = field(default_factory=list) # 경고
    fails: list[str] = field(default_factory=list)    # 탈락 사유
    raw: dict = field(default_factory=dict)           # 원본 수치

    def to_prompt_block(self) -> str:
        emoji = {"PASS": "✅", "WATCH": "⚠️", "FAIL": "❌"}.get(self.verdict, "")
        lines = [f"[펀더멘털] {emoji} {self.verdict} ({self.score}/100점)"]
        if self.flags:
            lines.append("  ✅ " + " | ".join(self.flags))
        if self.warnings:
            lines.append("  ⚠️ " + " | ".join(self.warnings))
        if self.fails:
            lines.append("  ❌ " + " | ".join(self.fails))
        return "\n".join(lines)


def _parse_num(text: str) -> float | None:
    """문자열에서 숫자(음수 포함) 추출. 실패 시 None."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", str(text).replace(",", ""))
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _scrape_finsum(code: str) -> dict[str, list[float]]:
    """네이버 증권 연간 재무요약 스크래핑.

    반환: {"매출액": [v2021, v2022, v2023, v2024], "영업이익": [...], ...}
    값은 연도 오름차순(과거→현재). N/A 셀은 건너뜀.
    """
    url = f"https://finance.naver.com/item/coinfo.naver?code={code}&target=finsum_D"
    try:
        resp = _session.get(url, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 여러 테이블 클래스 시도
        table = (
            soup.select_one("table.grayBg")
            or soup.select_one("table.tb_type1_ifrs")
            or soup.select_one("table.tb_type1")
            or soup.find("table")
        )
        if not table:
            return {}

        result: dict[str, list[float]] = {}
        for row in table.select("tr"):
            th = row.select_one("th")
            if not th:
                continue
            key = th.get_text(strip=True)
            if not key:
                continue
            vals = []
            for td in row.select("td"):
                raw_text = td.get_text(strip=True)
                v = _parse_num(raw_text)
                if v is not None:
                    vals.append(v)
            if vals:
                result[key] = vals
        return result
    except Exception as exc:
        print(f"  [{code}] 재무요약 스크래핑 실패: {exc}")
        return {}


def _get_from_finsum(finsum: dict, *keys: str) -> list[float]:
    """finsum dict에서 여러 후보 키 중 첫 번째로 매칭되는 값 반환."""
    for k in keys:
        if k in finsum:
            return finsum[k]
        # 부분 매칭 (예: "ROE" → "ROE(%)" 도 허용)
        for fk in finsum:
            if k in fk or fk in k:
                return finsum[fk]
    return []


def evaluate_fundamentals(code: str, stock_info: dict) -> FundamentalResult:
    """종목코드와 stock_info를 바탕으로 펀더멘털 평가 수행.

    stock_info는 get_stock_info() 반환값. PER·PBR·ROE 필드를 활용.
    추가로 네이버 재무요약을 스크래핑해 다년도 트렌드를 분석.
    """
    result = FundamentalResult()
    raw: dict = {}

    # ── 1. PER 음수 → 즉시 FAIL (적자 기업) ─────────────────────────────────
    per_str = str(stock_info.get("PER", ""))
    per_val: float | None = None
    # "15.4배" 형식 처리
    m = re.search(r"([\-\d.]+)배", per_str)
    if m:
        per_val = float(m.group(1))
    else:
        per_val = _parse_num(per_str)

    raw["PER"] = per_val
    if per_val is not None and per_val < 0:
        result.fails.append(f"적자 기업 (PER={per_val:.1f}배)")
        result.verdict = "FAIL"
        result.score = 0
        result.raw = raw
        return result

    # ── 2. 네이버 재무요약 스크래핑 ─────────────────────────────────────────
    finsum = _scrape_finsum(code)

    score = 50  # 기본 점수

    # ── 3. ROE 평가 ──────────────────────────────────────────────────────────
    roe_list = _get_from_finsum(finsum, "ROE(%)", "ROE", "자기자본이익률")

    # 스크래핑 실패 시 stock_info ROE 사용
    if not roe_list:
        roe_str = str(stock_info.get("ROE", ""))
        roe_val = _parse_num(roe_str.replace("%", ""))
        if roe_val is not None:
            roe_list = [roe_val]

    raw["ROE_list"] = roe_list

    if roe_list:
        latest_roe = roe_list[-1]

        # ROE 음수 → FAIL
        if latest_roe < 0:
            result.fails.append(f"ROE 음수 ({latest_roe:.1f}%)")
            result.verdict = "FAIL"
            result.score = 0
            result.raw = raw
            return result

        # ROE 크기별 점수
        if latest_roe >= 30:
            result.flags.append(f"ROE 우수 {latest_roe:.1f}%≥30%")
            score += 15
        elif latest_roe >= 15:
            result.flags.append(f"ROE 양호 {latest_roe:.1f}%≥15%")
            score += 8
        elif latest_roe >= 10:
            score += 3
        else:
            result.warnings.append(f"ROE 낮음 {latest_roe:.1f}%<10%")
            score -= 5

        # 3년 이상 추세 분석
        if len(roe_list) >= 3:
            tail = [r for r in roe_list[-3:] if r is not None]
            if len(tail) >= 2:
                if all(tail[i] <= tail[i + 1] for i in range(len(tail) - 1)):
                    result.flags.append("ROE 3년 상승 추세")
                    score += 10
                elif tail[-1] >= tail[0]:
                    result.flags.append("ROE 추세 유지")
                    score += 5
                else:
                    result.warnings.append(f"ROE 하락 추세 ({tail[0]:.1f}%→{tail[-1]:.1f}%)")
                    score -= 8
    else:
        result.warnings.append("ROE 데이터 부족 — 관심 종목 수준으로 유지")

    # ── 4. PBR vs ROE 저평가 체크 ────────────────────────────────────────────
    pbr_str = str(stock_info.get("PBR", ""))
    m_pbr = re.search(r"([\d.]+)배", pbr_str)
    pbr_val = float(m_pbr.group(1)) if m_pbr else _parse_num(pbr_str)
    raw["PBR"] = pbr_val

    if pbr_val is not None and roe_list:
        latest_roe = roe_list[-1]
        threshold = latest_roe / 10.0
        if pbr_val < threshold:
            result.flags.append(f"PBR 저평가 ({pbr_val:.1f}배 < ROE/10={threshold:.1f})")
            score += 12
        elif latest_roe > 0 and pbr_val > threshold * 2:
            result.warnings.append(f"PBR 고평가 ({pbr_val:.1f}배 > ROE/10×2={threshold*2:.1f})")
            score -= 5

    # ── 5. 부채비율 ───────────────────────────────────────────────────────────
    debt_list = _get_from_finsum(finsum, "부채비율(%)", "부채비율", "부채비율(%)E")
    raw["부채비율_list"] = debt_list

    if debt_list:
        latest_debt = debt_list[-1]
        if latest_debt >= 200:
            result.warnings.append(f"부채비율 높음 {latest_debt:.0f}%≥200%")
            score -= 10
        elif latest_debt >= 100:
            result.warnings.append(f"부채비율 주의 {latest_debt:.0f}%")
            score -= 3
        else:
            result.flags.append(f"부채비율 건전 {latest_debt:.0f}%<100%")
            score += 5

    # ── 6. ROE - ROA 괴리 (부채로 ROE 부풀림 의심) ───────────────────────────
    roa_list = _get_from_finsum(finsum, "ROA(%)", "ROA", "총자산이익률")
    raw["ROA_list"] = roa_list

    if roe_list and roa_list:
        gap = roe_list[-1] - roa_list[-1]
        if gap > 20:
            result.warnings.append(f"ROE-ROA 괴리 {gap:.1f}%p (부채 레버리지 의심)")
            score -= 5

    # ── 7. 매출 성장률 ────────────────────────────────────────────────────────
    rev_list = _get_from_finsum(finsum, "매출액", "매출액(억원)", "매출액(백만원)")
    raw["매출액_list"] = rev_list

    if len(rev_list) >= 3:
        tail = [r for r in rev_list[-3:] if r and r > 0]
        if len(tail) >= 2:
            growth = (tail[-1] - tail[0]) / abs(tail[0]) * 100
            if growth > 30:
                result.flags.append(f"매출 3년 {growth:.0f}%↑ 급성장")
                score += 8
            elif growth > 10:
                result.flags.append(f"매출 3년 {growth:.0f}%↑ 성장")
                score += 4
            elif growth < -10:
                result.warnings.append(f"매출 3년 {growth:.0f}% 감소")
                score -= 8

    # ── 8. 영업이익 성장률 ────────────────────────────────────────────────────
    op_list = _get_from_finsum(finsum, "영업이익", "영업이익(억원)", "영업이익(백만원)")
    raw["영업이익_list"] = op_list

    if len(op_list) >= 3:
        tail = [r for r in op_list[-3:] if r and r > 0]
        if len(tail) >= 2:
            op_growth = (tail[-1] - tail[0]) / abs(tail[0]) * 100
            if op_growth > 50:
                result.flags.append(f"영업이익 3년 {op_growth:.0f}%↑ 급성장")
                score += 10
            elif op_growth > 20:
                result.flags.append(f"영업이익 3년 {op_growth:.0f}%↑ 성장")
                score += 5
            elif op_growth < -20:
                result.warnings.append(f"영업이익 3년 {op_growth:.0f}% 감소")
                score -= 10

    # ── 9. 영업현금흐름 (데이터 확보 시) ────────────────────────────────────
    ocf_list = _get_from_finsum(finsum, "영업활동현금흐름", "영업현금흐름", "영업CF")
    raw["영업CF_list"] = ocf_list

    if ocf_list:
        latest_ocf = ocf_list[-1]
        if latest_ocf < 0:
            result.warnings.append(f"영업CF 음수 ({latest_ocf:,.0f}) — 현금흐름 주의")
            score -= 15
        elif op_list and op_list[-1] > 0 and latest_ocf > op_list[-1]:
            result.flags.append("영업CF > 영업이익 (현금흐름 우량)")
            score += 8
    # 영업CF 데이터 없으면 무시 (영향 없음)

    # ── 10. 최종 판정 ─────────────────────────────────────────────────────────
    score = max(0, min(100, score))
    result.score = score
    result.raw = raw

    # PER·PBR 둘 다 없으면 수익성 확인 불가 → 점수 무관하게 WATCH(관심 종목)
    per_absent = raw.get("PER") is None
    pbr_absent = raw.get("PBR") is None

    if result.fails:
        result.verdict = "FAIL"
        result.score = 0
    elif per_absent and pbr_absent:
        result.verdict = "WATCH"
        result.warnings.append("PER·PBR 데이터 없음 → 관심 종목으로만 분류")
    elif score >= 70:
        result.verdict = "PASS"
    elif score >= 40:
        result.verdict = "WATCH"
    else:
        result.verdict = "FAIL"

    return result


if __name__ == "__main__":
    import sys
    from stock_info import get_stock_info

    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    print(f"[{code}] 기업정보 조회 중...")
    info = get_stock_info(code)
    print(f"  PER: {info.get('PER')}  PBR: {info.get('PBR')}  ROE: {info.get('ROE')}")

    print(f"[{code}] 펀더멘털 평가 중...")
    res = evaluate_fundamentals(code, info)
    print(res.to_prompt_block())
    print(f"\n원본 수치: {res.raw}")
