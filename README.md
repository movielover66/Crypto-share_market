# ApexQuant INR — Spot Swing & Macro Crypto Panel

Paper-trading **crypto (INR)** + **NSE stock** swing scanner with a macro/news panel and a Bengali
"learn" screen per coin. Backend: **FastAPI**. Data: **Binance public market data**, **CoinDCX INR**,
**CoinPaprika**, **Alternative.me Fear & Greed**, **yfinance (NSE)**, **Gemini** (news summary), optional
**Telegram** alerts and **Supabase** storage. No real orders are placed.

## Structure
```text
.
├── .gitignore
├── README.md
├── backend/
│   ├── main.py             # API, NSE scanner, trades, background bot
│   ├── crypto_engine.py    # crypto indicators + scoring + trade evaluation
│   ├── analysis_engine.py  # rule-based analysis for coin.html
│   ├── boom_engine.py      # boom radar (volume surge + breakout)
│   ├── research_engine.py  # AI coin research: verdict Pass/Caution/Block + grade A-D
│   ├── data_sources.py     # hard facts: CoinPaprika, GitHub, DefiLlama, Google News RSS
│   ├── angel_feed.py       # Angel One SmartAPI: login, live websocket ticks, daily candles
│   ├── stock_live.py       # live intraday momentum radar + Telegram alerts + outcome journal
│   ├── stock_premarket.py  # 9:00 AM IST pre-market report (global cues, news, picks)
│   ├── universe.py         # ~170 liquid NSE stocks watched live
│   ├── gemini_pool.py      # multi-key Gemini caller (grounded search, cooldowns)
│   ├── supabase_coin_research.sql  # optional table for research cache
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── index.html          # main dashboard
    └── coin.html           # per-coin learning screen (/coin.html?symbol=BTC)
```

## Run locally
```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate | Mac/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add the keys you want
uvicorn main:app --reload --port 8000
```
Dashboard: `http://127.0.0.1:8000` · Coin screen: `http://127.0.0.1:8000/coin.html` · API docs: `/docs`

## Deploy (Render)
Start command from repo root: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`.
Set `SUPABASE_URL` / `SUPABASE_KEY` so paper trades survive restarts.

## Coin research (Gemini + live search)
- `GET /api/research/{SYMBOL}?mode=quick|deep` (quick = 7-day risk check, deep = fundamentals, grade A-D). `GET /api/research` lists cached verdicts.
- Boom auto-trades run a *quick* check, crypto auto-trades a *deep* check. **Block** stops the paper trade; **Unknown** stops it only if `RESEARCH_STRICT=1`.
- A daily 03:00 IST job pre-researches the top 10 crypto picks.
- Env: `GEMINI_API_KEYS=k1,k2,...` (Google counts limits per *project*, so keys from one project share a quota), optional `GITHUB_TOKEN`,
  `RESEARCH_MAX_PER_HOUR` (default 30), `RESEARCH_STRICT`.
- AI research can be wrong or out of date. It lowers the chance of scams/rug-pulls, it does not remove it. Not financial advice.

## Live NSE momentum radar + 9:00 AM pre-market report
- **9:00 AM IST (Mon-Fri):** Telegram report: global cues, GIFT Nifty/FII-DII/news (Gemini + Google Search), intraday watchlist, swing picks, long-term watch (Mondays). A pick with a HIGH news-risk flag is dropped. Manual: `GET/POST /api/stocks/premarket/send?force=true` (10 min cooldown).
- **9:15-3:30:** live Angel One ticks for ~170 stocks. Boom-style momentum (volume surge + opening-range/previous-day-high breakout + above VWAP) goes to Telegram with entry, SL, T1, T2. Each alert is tracked to its result (SL first = LOSS, T1 first = WIN); a summary is sent at 3:20 PM. `GET /api/stocks/live/journal`, `/api/stocks/live/status`.
- The Stocks tab shows live prices and the radar status.
- **Env (Render):** `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET` (the TOTP secret text, not the 6-digit code). Optional: `INTRADAY_RISK_INR` (default 1000), `STOCK_ALERTS_PER_HOUR` (default 8).
- Needs an always-on server (a sleeping free instance has no live feed). Rule-based signals can be wrong; start by watching the journal for a few weeks.

### Live radar v6: selective alerts, research gate, live P&L
- **Grades:** A (score >= 80, volume surge >= 2.5x) is sent to Telegram; B (70-79, or A blocked by a rule) is a silent *shadow* signal kept only for statistics.
- **A rules:** only 9:30-11:30 and 1:45-2:45 PM; max 5 per day, 2 per hour, 3 open at once; one per stock per day; none if Nifty is down 0.8%+; 1 hour pause after 2 stop-losses in a row; stop for the day at -3R.
- **Research gate:** before an A alert is sent, Gemini + Google Search checks that stock's last 3 days of news/events. A HIGH risk flag cancels it; the entry/SL/targets are re-based on the price at send time.
- **Live screen:** `GET /api/stocks/live/stream` (Server-Sent Events, one update per second) feeds the Stocks tab: P&L, prices and alerts update without refresh. Max 6 viewers at once.
- **Env (all optional):** `STOCK_MIN_SCORE` (80), `STOCK_DAILY_CAP` (5), `STOCK_MAX_OPEN` (3), `INTRADAY_RISK_INR` (1000, risk per trade, sets the share count), `STOCK_RESEARCH=0` (turn the news check off).
