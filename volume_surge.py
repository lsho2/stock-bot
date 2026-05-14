#!/usr/bin/env python3
"""KIS API로 거래량 급등 종목 탐색."""

import os
import time
import json
import requests
from dotenv import load_dotenv
from stock_info import get_token, get_chart_data, _kis_headers, KIS_REAL_URL

load_dotenv()

DEBUG = os.getenv("DEBUG", "")

VOLUME_RATIO_MIN = 3.0  # 20일 평균 대비 최소 배율
MIN_PRICE = 1000         # 최소 현재가 (원)
MIN_MARKET_CAP = 1000    # 최소 시가총액 (억원)
TOP_N = 10               # 최종 출력 건수
CANDIDATES = 50          # 일봉 조회할 후보 수 (거래량 순위 상위 N개)


def _fetch_volume_rank(blng_cls: str) -> list[dict]:
    """KIS 거래량 순위 API (FHPST01720000).

    blng_cls: '1'=코스피, '2'=코스닥
    """
    token = get_token()
    url = f"{KIS_REAL_URL}/uapi/domestic-stock/v1/ranking/volume"
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_cond_scr_div_code": "20171",
        "fid_input_iscd": "0000",
        "fid_div_cls_code": "0",
        "fid_blng_cls_code": blng_cls,
        "fid_trgt_cls_code": "111111111",
        "fid_trgt_exls_cls_code": "000000",
        "fid_input_price_1": str(MIN_PRICE),
        "fid_input_price_2": "",
        "fid_vol_cnt": "",
        "fid_input_date_1": "",
    }
    resp = requests.get(
        url,
        headers=_kis_headers(token, "FHPST01720000"),
        params=params,
        timeout=15,
    )
    data = resp.json()
    if DEBUG:
        label = "코스피" if blng_cls == "1" else "코스닥"
        print(f"  [DEBUG] 거래량 순위({label}) 응답:\n"
              f"{json.dumps(data, ensure_ascii=False, indent=2)[:3000]}")
    return data.get("output", []) or []


def _avg_volume(chart: list[dict]) -> float:
    """일봉 리스트에서 20일 평균 거래량 (당일 제외)."""
    vols = [r["거래량"] for r in chart if r["거래량"] > 0]
    # chart[0]이 가장 최근(오늘)이므로 제외하고 나머지로 평균
    past = vols[1:] if len(vols) > 1 else vols
    return sum(past) / len(past) if past else 0.0


def _parse_int(val) -> int:
    try:
        return int(val or 0)
    except (ValueError, TypeError):
        return 0


def _parse_float(val) -> float:
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


def get_volume_surge() -> list[dict]:
    """오늘 거래량이 20일 평균 대비 3배 이상인 종목 반환.

    반환:
        [{"종목명": str, "종목코드": str, "현재가": int, "등락률": float,
          "거래량": int, "평균거래량": int, "배율": float}, ...]
    """
    # ── 1. 코스피 + 코스닥 거래량 순위 수집 ──────────────────────────────
    rows: list[dict] = []
    for blng, label in (("1", "코스피"), ("2", "코스닥")):
        try:
            batch = _fetch_volume_rank(blng)
            print(f"  {label} 후보: {len(batch)}개")
            rows.extend(batch)
        except Exception as exc:
            print(f"  {label} 거래량 순위 오류: {exc}")

    if not rows:
        return []

    # ── 2. 1차 필터: 현재가, 상한가 제외 ──────────────────────────────────
    candidates = []
    for r in rows:
        price = _parse_int(r.get("stck_prpr"))
        if price < MIN_PRICE:
            continue
        # 상한가: 대비 부호 '1' 또는 등락률 29% 이상
        sign = str(r.get("prdy_vrss_sign", ""))
        rate = _parse_float(r.get("prdy_ctrt"))
        if sign == "1" or rate >= 29.0:
            continue
        candidates.append(r)

    print(f"  1차 필터 통과: {len(candidates)}개 → 상위 {CANDIDATES}개 일봉 조회")
    candidates = candidates[:CANDIDATES]

    # ── 3. 2차: 20일 평균 거래량 대비 배율 계산 ───────────────────────────
    results = []
    for i, r in enumerate(candidates, 1):
        code = str(r.get("mksc_shrn_iscd", "") or "").strip()
        if not code:
            continue

        today_vol = _parse_int(r.get("acml_vol"))

        # 응답에 평균 거래량 필드가 있으면 우선 사용, 없으면 일봉으로 직접 계산
        avrg_raw = str(r.get("avrg_vol", "") or "").strip()
        if avrg_raw:
            avg_vol = _parse_float(avrg_raw)
        else:
            try:
                chart = get_chart_data(code)
                avg_vol = _avg_volume(chart)
                time.sleep(0.12)  # KIS API rate limit
            except Exception as exc:
                print(f"    일봉 오류 ({code}): {exc}")
                continue

        if avg_vol <= 0 or today_vol <= 0:
            continue

        ratio = today_vol / avg_vol
        if ratio < VOLUME_RATIO_MIN:
            continue

        # 시가총액 필터 (stck_avls 필드가 억원 단위일 경우)
        mktcap_raw = str(r.get("stck_avls", "") or "").strip()
        if mktcap_raw:
            mktcap = _parse_float(mktcap_raw)
            if mktcap > 0 and mktcap < MIN_MARKET_CAP:
                continue

        results.append({
            "종목명": str(r.get("hts_kor_isnm", "") or "").strip(),
            "종목코드": code,
            "현재가": _parse_int(r.get("stck_prpr")),
            "등락률": _parse_float(r.get("prdy_ctrt")),
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
        header = f"{'#':<3} {'종목명':<12} {'코드':<8} {'현재가':>9} {'등락률':>7} {'거래량':>12} {'평균거래량':>12} {'배율':>6}"
        print(f"\n{header}")
        print("-" * len(header))
        for rank, s in enumerate(surge, 1):
            print(
                f"{rank:<3} {s['종목명']:<12} {s['종목코드']:<8} "
                f"{s['현재가']:>9,} {s['등락률']:>+6.1f}% "
                f"{s['거래량']:>12,} {s['평균거래량']:>12,} {s['배율']:>5.1f}x"
            )
