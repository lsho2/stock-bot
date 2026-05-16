# 📊 Stock Auto-Trading Bot

> **한국 주식 스윙 트레이딩 자동화 봇** — KIS API + Google Gemini AI 기반  
> Korean swing-trading bot powered by KIS Open API and Google Gemini.

---

## 🎯 주요 기능 / Features

| 기능 | 설명 |
|------|------|
| 🤖 AI 추천 분석 | 한국경제 PDF 파싱 → Gemini 2차 분석 → 최종 추천 종목 선정 |
| 📈 자동 매매 | 손절(-5%), 트레일링 스탑(-3%), 목표가(+10%) 자동 실행 |
| 🔄 잔고 자동 동기화 | 첫 실행 시 증권사 잔고와 positions.json 자동 정합 |
| 📊 일간 보고서 | 매일 15:35 당일 손익 + 보유 종목 평가 텔레그램 전송 |
| 💾 거래 기록 | 모든 매수/매도 자동 기록 (trades.json, daily_pnl.json) |
| 📡 텔레그램 알림 | 매매 체결·추천·보고서 실시간 알림 |
| 🛡️ 펀더멘털 필터 | 홍진채 '거인의 어깨' 기준 재무 스크리닝 |

---

## 🛠 기술 스택 / Tech Stack

- **Python 3.13**
- **KIS Open API** — 한국투자증권 모의투자 API
- **Google Gemini API** — AI 종목 분석 (gemini-2.5-flash)
- **Telegram Bot API** — 실시간 알림
- **BeautifulSoup4** — 한경 PDF 텍스트 파싱
- **Requests** — HTTP 클라이언트

---

## 📁 프로젝트 구조 / Project Structure

```
stock-bot/
├── consensus.py          # AI 추천 분석 (메인 파이프라인)
├── monitor.py            # 자동 매매 감시 (3분 간격)
├── buy.py                # 반자동 매수 CLI
├── daily_report.py       # 일간 보고서 생성·전송
├── tracker.py            # 거래 기록 (trades / daily_pnl / recommendations)
├── stock_info.py         # KIS API + 네이버 금융 시세
├── telegram_alert.py     # 텔레그램 알림 (개인 / 브로드캐스트)
├── fundamental_filter.py # 펀더멘털 필터
├── positions.json        # 보유 포지션 (gitignore 처리)
├── .env                  # API 키 (gitignore 처리)
├── .env.example          # 환경변수 템플릿
└── data/                 # 거래 데이터 (gitignore 처리)
    ├── trades.json
    ├── daily_pnl.json
    └── recommendations.json
```

---

## ⚙️ 설정 / Setup

### 1. 환경변수 설정
```bash
cp .env.example .env
# .env 파일에 실제 API 키 입력
```

### 2. 패키지 설치
```bash
pip install requests python-dotenv beautifulsoup4 google-generativeai
```

### 3. positions.json 초기화
```json
{
  "seed_money": 10000000,
  "positions": [],
  "last_known_balance": null
}
```

### 4. KIS 모의투자 신청
- [한국투자증권 Open API](https://apiportal.koreainvestment.com) 접속
- 모의투자 앱키/시크릿 발급

---

## 🚀 실행 / Usage

### AI 추천 분석 (매일 아침 장 시작 전)
```bash
python consensus.py
```
한경 PDF 파싱 → 종목 풀 구성 → Gemini 1차 분석 → 펀더멘털 필터 → Gemini 2차 분석 → 최종 추천 텔레그램 전송

### 자동 매매 감시 (장중 상시 실행)
```bash
python monitor.py
```
- 3분마다 보유 종목 현재가 조회
- 손절(-5%) / 트레일링 스탑(-3%) / 목표가(+10%) 자동 매도
- **첫 실행 시** 증권사 잔고 ↔ positions.json 자동 동기화

```bash
# 실매매 없이 로직만 확인
DRY_RUN=true python monitor.py
```

### 반자동 매수
```bash
python buy.py [종목코드] [수량]
# 예: python buy.py 035900 30
```
현재가 미리보기 → 확인 프롬프트 → KIS 시장가 주문 → 체결가 기록 → positions.json 자동 등록

### 일간 보고서 (Windows 작업 스케줄러 자동 등록)
```bash
python daily_report.py
```
당일 매매 내역 + 보유 종목 평가손익 → 텔레그램 전송  
*(평일 15:35 자동 실행)*

---

## 📈 첫 운영 결과 / Early Results

- 운영 기간: 2026년 5월~
- 전략: 한경 AI 추천 + 모멘텀 필터 + 트레일링 스탑
- 매매 원칙: `data/principles/trading_principles.txt` 참고

> *실계좌 운영 전 모의투자(VTS)로 전략 검증 권장*

---

## ⚠️ Disclaimer

이 프로젝트는 **개인 학습 및 자동화 실험 목적**으로 제작되었습니다.

- 투자 추천이 아니며, 실손실에 대한 책임은 사용자 본인에게 있습니다.
- KIS 모의투자 API 기반으로 작성되었으나 실계좌 사용 시 충분한 검증이 필요합니다.
- API 키, 계좌번호 등 민감 정보는 절대 공개 저장소에 커밋하지 마세요.

> This project is for **personal learning and automation research only**.  
> Not financial advice. Use at your own risk.

---

## 📄 License

MIT
