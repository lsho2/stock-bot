from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from bs4 import BeautifulSoup
import time

NAVER_FINANCE_URL = "https://finance.naver.com"


def get_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
    except ImportError:
        driver = webdriver.Chrome(options=options)
    return driver


def parse_main_news(soup):
    """div.news_area 안의 뉴스 링크 텍스트 추출"""
    news_div = soup.select_one("div.news_area")
    if not news_div:
        return []
    news = []
    for a in news_div.select("a"):
        title = a.get_text(strip=True)
        if len(title) > 5 and title not in news:
            news.append(title)
    return news[:10]


def parse_ranking_list(ul_tag):
    """ul.list_rank 안에서 이름(strong)과 등락률(em) 추출"""
    items = []
    for li in ul_tag.select("li"):
        name_tag = li.select_one("p.item a strong")
        change_tag = li.select_one("p.item em")
        name = name_tag.get_text(strip=True) if name_tag else ""
        change = change_tag.get_text(strip=True) if change_tag else ""
        if name:
            items.append(f"{name} ({change})" if change else name)
    return items


def crawl_naver_finance():
    print("드라이버 초기화 중...")
    driver = get_driver()
    results = {"주요뉴스": [], "업종상위": [], "테마상위": []}

    try:
        print("네이버 증권 메인 페이지 로딩 중...")
        driver.get(NAVER_FINANCE_URL)
        time.sleep(3)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        results["주요뉴스"] = parse_main_news(soup)

        rank_lists = soup.select("ul.list_rank")
        if len(rank_lists) >= 1:
            results["업종상위"] = parse_ranking_list(rank_lists[0])
        if len(rank_lists) >= 2:
            results["테마상위"] = parse_ranking_list(rank_lists[1])

    except Exception as e:
        print(f"오류 발생: {e}")

    finally:
        driver.quit()

    return results


def print_results(results):
    print("\n" + "=" * 50)
    print("  네이버 증권 크롤링 결과")
    print("=" * 50)

    sections = [
        ("주요 뉴스", results["주요뉴스"]),
        ("업종 상위", results["업종상위"]),
        ("테마 상위", results["테마상위"]),
    ]

    for title, items in sections:
        print(f"\n[{title}]")
        if items:
            for i, item in enumerate(items, 1):
                print(f"  {i}. {item}")
        else:
            print("  데이터 없음")

    print("\n" + "=" * 50)


if __name__ == "__main__":
    results = crawl_naver_finance()
    print_results(results)
