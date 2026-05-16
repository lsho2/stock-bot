#!/usr/bin/env python3
"""네이버 증권 거래량 상위 종목 탐색 (requests + BeautifulSoup 크롤링)."""

import re
import time

import requests
from bs4 import BeautifulSoup

from stock_info import get_chart_data

VOLUME_RATIO_MIN = 3.0  # 20일 평균 대비 최소 배율
MIN_PRICE = 1000         # 최소 현재가 (원)
TOP_N = 10               # 최종 출력 건수
CANDIDATES = 50          # 일봉 조회할 후보 수

NAVER_QUANT_URL = "https://finance.naver.com/sise/sise_quant.naver"


def _parse_int(val: str) -> int:
    try:
        return int(str(val).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def _parse_float(val: str) -> float:
    try:
        return float(str(val).replace(",", "").replace("%", "").replace("+", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def _fetch_volume_rank_naver(sosok: int) -> list[dict]:
    """네이버 증권 거래량 순위 크롤링 (requests + BeautifulSoup).

    sosok: 0=코스피, 1=코스닥
    반환: [{"종목명", "종목코드", "현재가", "등락률", "거래량"}, ...]
    """
    label = "코스피" if sosok == 0 else "코스닥"
    url = f"{NAVER_QUANT_URL}?sosok={sosok}"
    print(f"  [{label}] 크롤링: {url}")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.encoding = "euc-kr"

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.select_one("table.type_2")
    if not table:
        print(f"  [{label}] 테이블 없음")
        return []

    rows = table.find_all("tr")
    print(f"  [{label}] tr 총 {len(rows)}개")

    results: list[dict] = []
    for row in rows:
        tds = row.find_all("td")
        # 헤더 구조: N | 종목명 | 현재가 | 전일비 | 등락률 | 거래량 | ...
        if len(tds) < 6:
            continue

        anchor = tds[1].find("a")
        if not anchor:
            continue
        name = anchor.get_text(strip=True)
        if not name:
            continue
        href = anchor.get("href", "")
        m = re.search(r"code=(\d{6})", href)
        if not m:
            continue
        code = m.group(1)

        price = _parse_int(tds[2].get_text(strip=True))
        rate = _parse_float(tds[4].get_text(strip=True))
        volume = _parse_int(tds[5].get_text(strip=True))

        results.append({
            "종목명": name,
            "종목코드": code,
            "현재가": price,
            "등락률": rate,
            "거래량": volume,
        })

    print(f"  [{label}] 수집 완료: {len(results)}개")
    return results


def _avg_volume(chart: list[dict]) -> float:
    """일봉 리스트에서 20일 평균 거래량 (당일 제외)."""
    vols = [r["거래량"] for r in chart if r["거래량"] > 0]
    past = vols[1:] if len(vols) > 1 else vols
    return sum(past) / len(past) if past else 0.0


def get_volume_surge() -> list[dict]:
    """오늘 거래량이 20일 평균 대비 3배 이상인 종목 반환.

    반환:
        [{"종목명": str, "종목코드": str, "현재가": int, "등락률": float,
          "거래량": int, "평균거래량": int, "배율": float}, ...]
    """
    # ── 1. 코스피 + 코스닥 거래량 순위 수집 ──────────────────────────────
    rows: list[dict] = []
    for sosok, label in ((0, "코스피"), (1, "코스닥")):
        try:
            batch = _fetch_volume_rank_naver(sosok)
            rows.extend(batch)
        except Exception as exc:
            print(f"  [{label}] 크롤링 오류: {exc}")

    if not rows:
        return []

    # ── 2. 1차 필터: 현재가, 상한가 제외 ──────────────────────────────────
    candidates: list[dict] = []
    for r in rows:
        if r["현재가"] < MIN_PRICE:
            continue
        if r["등락률"] >= 29.0:
            continue
        candidates.append(r)

    print(f"  1차 필터 통과: {len(candidates)}개 → 상위 {CANDIDATES}개 일봉 조회")
    candidates = candidates[:CANDIDATES]

    # ── 3. 20일 평균 거래량 대비 배율 계산 ───────────────────────────────
    results: list[dict] = []
    for i, r in enumerate(candidates, 1):
        code = r["종목코드"]
        today_vol = r["거래량"]

        try:
            chart = get_chart_data(code)
            avg_vol = _avg_volume(chart)
            time.sleep(0.12)
        except Exception as exc:
            print(f"    일봉 오류 ({code}): {exc}")
            continue

        if avg_vol <= 0 or today_vol <= 0:
            continue

        ratio = today_vol / avg_vol
        if ratio < VOLUME_RATIO_MIN:
            continue

        results.append({
            "종목명": r["종목명"],
            "종목코드": code,
            "현재가": r["현재가"],
            "등락률": r["등락률"],
            "거래량": today_vol,
            "평균거래량": round(avg_vol),
            "배율": round(ratio, 1),
        })

        if i % 10 == 0:
            print(f"    진행 {i}/{len(candidates)} ...")

    # ── 4. 배율 내림차순 정렬, 상위 N개 ──────────────────────────────────
    results.sort(key=lambda x: x["배율"], reverse=True)
    return results[:TOP_N]


if __name__ == "__main__":
    print("거래량 급등 종목 스캔 중...\n")
    surge = get_volume_surge()

    if not surge:
        print("\n조건에 맞는 종목 없음.")
    else:
        header = (
            f"{'#':<3} {'종목명':<12} {'코드':<8} {'현재가':>9} "
            f"{'등락률':>7} {'거래량':>12} {'평균거래량':>12} {'배율':>6}"
        )
        print(f"\n{header}")
        print("-" * len(header))
        for rank, s in enumerate(surge, 1):
            print(
                f"{rank:<3} {s['종목명']:<12} {s['종목코드']:<8} "
                f"{s['현재가']:>9,} {s['등락률']:>+6.1f}% "
                f"{s['거래량']:>12,} {s['평균거래량']:>12,} {s['배율']:>5.1f}x"
            )
