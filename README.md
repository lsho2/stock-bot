# stock-bot

Korean stock market analysis bot using Gemini AI and securities research reports.

## Features

- Parses daily securities research reports (PDF)
- Runs 2-pass analysis via Gemini AI
- Sends morning briefing to Telegram
- Tracks volume surge and news alerts

## Setup

1. Copy `.env.example` to `.env` and fill in your API keys
2. Install dependencies: `pip install -r requirements.txt`
3. Run: `python consensus.py`

## Files

| File | Description |
|------|-------------|
| `consensus.py` | Main analysis script |
| `stock_info.py` | Real-time stock data fetcher |
| `telegram_alert.py` | Telegram message sender |
| `volume_surge.py` | Volume surge detector |
| `news.py` | News scraper |
| `run_morning.bat` | Morning run batch file |

## Requirements

- Python 3.13+
- Gemini API key
- Telegram Bot token
