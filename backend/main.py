import asyncio
import csv
import datetime
import io
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import httpx
import yfinance as yf
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

try:
    from . import crypto_engine  # Render runs: uvicorn backend.main:app (from repo root)
    from . import analysis_engine
    from . import boom_engine
    from . import research_engine
    from . import gemini_pool
    from . import stock_live
    from . import stock_premarket
except ImportError:
    import crypto_engine  # local run: uvicorn main:app (from /backend)
    import analysis_engine
    import boom_engine
    import research_engine
    import gemini_pool
    import stock_live
    import stock_premarket

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_FILE = BASE_DIR.parent / "frontend" / "index.html"
COIN_FILE = BASE_DIR.parent / "frontend" / "coin.html"
LOCAL_TRADES_FILE = BASE_DIR / "trades_backup.json"

_CACHE: Dict[str, Dict[str, Any]] = {}
_LAST_BRIEFING_DATE: str = ""
_LAST_NSE_SCAN_DATE: str = ""
_LAST_RESEARCH_DATE: str = ""
_BG_TASKS: set = set()
_LAST_PREMARKET_DATE: str = ""
_LAST_PREMARKET_MANUAL: float = 0.0
STOCK_RUNNER: Optional[Any] = None
_SSE_CLIENTS: int = 0

NSE_UNIVERSE = [
    ("RELIANCE.NS", "Reliance Industries"),
    ("TCS.NS", "Tata Consultancy"),
    ("HDFCBANK.NS", "HDFC Bank"),
    ("ICICIBANK.NS", "ICICI Bank"),
    ("SBIN.NS", "State Bank of India"),
    ("INFY.NS", "Infosys"),
    ("TATAMOTORS.NS", "Tata Motors"),
    ("BHARTIARTL.NS", "Bharti Airtel"),
    ("ITC.NS", "ITC Ltd"),
    ("LT.NS", "Larsen & Toubro"),
    ("AXISBANK.NS", "Axis Bank"),
    ("BAJFINANCE.NS", "Bajaj Finance"),
    ("SUNPHARMA.NS", "Sun Pharma"),
    ("MARUTI.NS", "Maruti Suzuki"),
    ("TITAN.NS", "Titan Company"),
    ("TRENT.NS", "Trent Ltd"),
    ("HAL.NS", "Hindustan Aeronautics"),
    ("BEL.NS", "Bharat Electronics"),
]


def format_duration(seconds: int) -> str:
    seconds = max(60, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if mins > 0 or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def get_cache(key: str, ttl_sec: int = 300) -> Optional[Any]:
    entry = _CACHE.get(key)
    if entry and (time.time() - entry["ts"]) < ttl_sec:
        return entry["data"]
    return None


def set_cache(key: str, data: Any) -> None:
    _CACHE[key] = {"ts": time.time(), "data": data}


def calc_ema(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) <= period:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = prices[i] - prices[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(prices)):
        diff = prices[i] - prices[i - 1]
        avg_gain = (avg_gain * (period - 1) + (diff if diff > 0 else 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + (-diff if diff < 0 else 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_atr(candles: List[Dict[str, float]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return candles[-1]["c"] * 0.02 if candles else 1.0
    trs = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_adx_and_obv(candles: List[Dict[str, float]], period: int = 14) -> Tuple[float, bool, float]:
    if len(candles) < period + 5:
        return 18.0, False, 1.0
    trs, plus_dms, minus_dms = [], [], []
    obv = [0.0]
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        tr = max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"]))
        up = cur["h"] - prev["h"]
        down = prev["l"] - cur["l"]
        plus_dms.append(up if (up > down and up > 0) else 0.0)
        minus_dms.append(down if (down > up and down > 0) else 0.0)
        trs.append(tr)
        if cur["c"] > prev["c"]:
            obv.append(obv[-1] + cur["v"])
        elif cur["c"] < prev["c"]:
            obv.append(obv[-1] - cur["v"])
        else:
            obv.append(obv[-1])

    tr_s = sum(trs[:period])
    p_s = sum(plus_dms[:period])
    m_s = sum(minus_dms[:period])
    dx_list = []
    for i in range(period, len(trs)):
        tr_s = tr_s - (tr_s / period) + trs[i]
        p_s = p_s - (p_s / period) + plus_dms[i]
        m_s = m_s - (m_s / period) + minus_dms[i]
        if tr_s == 0:
            continue
        p_di = 100.0 * (p_s / tr_s)
        m_di = 100.0 * (m_s / tr_s)
        denom = p_di + m_di
        dx = 100.0 * abs(p_di - m_di) / denom if denom > 0 else 0.0
        dx_list.append(dx)

    adx = sum(dx_list[-period:]) / max(1, min(period, len(dx_list)))
    obv_rising = obv[-1] > obv[-6] if len(obv) >= 6 else False
    avg_vol = sum(c["v"] for c in candles[-21:-1]) / 20.0
    rvol = (candles[-1]["v"] / avg_vol) if avg_vol > 0 else 1.0
    return round(adx, 1), obv_rising, round(rvol, 2)


# Gemini: model name is configurable (GEMINI_MODEL) and falls back through a list, because Google retires models.
GEMINI_MODELS = list(dict.fromkeys(
    m for m in [os.getenv("GEMINI_MODEL", "").strip(), "gemini-3.5-flash", "gemini-2.5-flash"] if m
))


async def gemini_json(prompt: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Ask Gemini for a JSON answer through the multi-key pool. Returns (data, error_text)."""
    obj, _src, err = await gemini_pool.ask(prompt, grounded=False, timeout=30.0)
    return obj, err


async def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
            return r.status_code == 200
    except Exception:
        return False


# ==================== CRYPTO ENGINE ====================
async def fetch_4h_technicals(client: httpx.AsyncClient, symbol: str) -> Optional[Dict[str, Any]]:
    pair = f"{symbol.upper()}USDT"
    try:
        r = await client.get(
            f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=4h&limit=60",
            timeout=6.0,
        )
        if r.status_code != 200:
            return None
        candles = [
            {"o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
            for k in r.json()
        ]
        closes = [c["c"] for c in candles]
        ema20 = calc_ema(closes, 20)
        rsi14 = calc_rsi(closes, 14)
        adx14, obv_rising, rvol = calc_adx_and_obv(candles, 14)
        return {
            "above_ema20": (closes[-1] > ema20) if ema20 else False,
            "rsi_4h": round(rsi14, 1) if rsi14 else 50.0,
            "adx_4h": adx14,
            "obv_rising": obv_rising,
            "rvol": rvol,
        }
    except Exception:
        return None


async def fetch_btc_dominance(client: httpx.AsyncClient) -> float:
    cached = get_cache("btc_dom", ttl_sec=300)
    if cached is not None:
        return cached
    try:
        r = await client.get("https://api.coinpaprika.com/v1/global", timeout=8.0)
        if r.status_code == 200:
            dom = round(float(r.json().get("bitcoin_dominance_percentage")), 2)
            set_cache("btc_dom", dom)
            return dom
    except Exception:
        pass
    return 0.0


async def fetch_coindcx_inr_tickers(client: httpx.AsyncClient) -> Dict[str, float]:
    cached = get_cache("coindcx_inr", ttl_sec=45)
    if cached is not None:
        return cached
    inr_map: Dict[str, float] = {}
    try:
        resp = await client.get("https://api.coindcx.com/exchange/ticker", timeout=8.0)
        if resp.status_code == 200:
            for item in resp.json():
                market = item.get("market", "")
                if market.endswith("INR"):
                    sym = market[:-3].lower()
                    try:
                        inr_map[sym] = float(item.get("last_price", 0))
                    except (TypeError, ValueError):
                        pass
        set_cache("coindcx_inr", inr_map)
    except Exception:
        pass
    return inr_map


async def build_scanner_data(limit: int = 25) -> Dict[str, Any]:
    cache_key = f"scanner_v6_{limit}"
    cached = get_cache(cache_key, ttl_sec=300)
    if cached:
        return cached
    async with httpx.AsyncClient() as client:
        dcx_inr, btc_dom = await asyncio.gather(
            fetch_coindcx_inr_tickers(client),
            fetch_btc_dominance(client),
        )
        coins = await crypto_engine.build_crypto_scan(client, dcx_inr, limit)
    if coins is None:
        stale = _CACHE.get(cache_key)
        if stale:
            return stale["data"]
        return {"btc_dominance": btc_dom, "count": 0, "coins": [], "error": "Binance data unavailable"}
    payload = {"btc_dominance": btc_dom, "count": len(coins), "coins": coins}
    set_cache(cache_key, payload)
    return payload


# ==================== INDIAN STOCKS (NSE) ENGINE ====================
def _sync_build_nse_scanner() -> Dict[str, Any]:
    tickers = ["^NSEI", "^INDIAVIX"] + [s[0] for s in NSE_UNIVERSE]
    df = yf.download(tickers, period="3mo", interval="1d", group_by="ticker", progress=False, threads=True)

    def extract_candles(sym: str) -> List[Dict[str, float]]:
        try:
            sub = df[sym].dropna()
            out = []
            for idx, row in sub.iterrows():
                out.append({
                    "t": int(idx.timestamp()),
                    "o": float(row["Open"]),
                    "h": float(row["High"]),
                    "l": float(row["Low"]),
                    "c": float(row["Close"]),
                    "v": float(row["Volume"] or 0),
                })
            return out
        except Exception:
            return []

    nifty_c = extract_candles("^NSEI")
    vix_c = extract_candles("^INDIAVIX")

    nifty_price = round(nifty_c[-1]["c"], 2) if nifty_c else 0.0
    nifty_ema50 = calc_ema([x["c"] for x in nifty_c], 50) if len(nifty_c) >= 50 else None
    nifty_bullish = (nifty_price > nifty_ema50) if nifty_ema50 else True
    nifty_7d = ((nifty_c[-1]["c"] - nifty_c[-6]["c"]) / nifty_c[-6]["c"] * 100) if len(nifty_c) >= 6 else 0.0
    india_vix = round(vix_c[-1]["c"], 2) if vix_c else 14.5
    regime_ok = nifty_bullish and (india_vix < 22.0)

    stocks = []
    for sym, name in NSE_UNIVERSE:
        c = extract_candles(sym)
        if len(c) < 35:
            continue
        closes = [x["c"] for x in c]
        price = closes[-1]
        ema20 = calc_ema(closes, 20) or price
        ema50 = calc_ema(closes, 50) or ema20
        rsi14 = calc_rsi(closes, 14) or 50.0
        atr14 = calc_atr(c, 14)
        adx14, obv_rising, rvol = calc_adx_and_obv(c, 14)

        pct_24h = ((closes[-1] - closes[-2]) / closes[-2]) * 100
        pct_7d = ((closes[-1] - closes[-6]) / closes[-6]) * 100 if len(closes) >= 6 else 0.0
        pct_10d = ((closes[-1] - closes[-9]) / closes[-9]) * 100 if len(closes) >= 9 else pct_7d
        pct_30d = ((closes[-1] - closes[-22]) / closes[-22]) * 100 if len(closes) >= 22 else pct_7d
        rs_vs_nifty = round(pct_7d - nifty_7d, 2)

        above_ema20 = price > ema20
        uptrend_stack = price > ema20 and ema20 > ema50

        score = 30.0
        if uptrend_stack:
            score += 20.0
        elif above_ema20:
            score += 10.0
        if adx14 >= 20.0:
            score += 15.0
        if obv_rising and rvol >= 1.2:
            score += 15.0
        elif obv_rising or rvol >= 1.2:
            score += 8.0
        if rs_vs_nifty > 1.5:
            score += 10.0
        if 48.0 <= rsi14 <= 68.0:
            score += 10.0

        falling_knife = (not above_ema20) or (pct_30d < -12.0 and not above_ema20)
        if falling_knife:
            score = min(score, 52.0)
        if not regime_ok:
            score = min(score, 64.0)

        score = round(min(100.0, max(0.0, score)), 1)
        signal = (
            "Falling Knife (Wait)" if falling_knife
            else "Confirmed Breakout" if (score >= 75 and uptrend_stack)
            else "Trend Swing" if score >= 65
            else "Accumulation" if score >= 55
            else "Watchlist"
        )

        sl = round(max(price * 0.85, price - atr14 * 1.5), 2)
        tp1 = round(price + atr14 * 2.25, 2)
        tp2 = round(price + atr14 * 4.0, 2)
        hold_label = "2-4 Days (Momentum)" if (adx14 >= 25 and rvol >= 1.3) else ("5-10 Days (Positional)" if uptrend_stack else "3-5 Days (Swing)")

        stocks.append({
            "symbol": sym,
            "short_symbol": sym.replace(".NS", ""),
            "name": name,
            "price_inr": round(price, 2),
            "target_inr": tp1,
            "target2_inr": tp2,
            "stop_loss_inr": sl,
            "hold_label": hold_label,
            "change_24h_pct": round(pct_24h, 2),
            "change_7d_pct": round(pct_7d, 2),
            "change_10d_pct": round(pct_10d, 2),
            "change_30d_pct": round(pct_30d, 2),
            "rsi_14": round(rsi14, 1),
            "adx_14": adx14,
            "rvol": rvol,
            "rs_vs_nifty": rs_vs_nifty,
            "swing_score": score,
            "signal": signal,
        })

    stocks.sort(key=lambda x: x["swing_score"], reverse=True)
    return {
        "nifty_50": nifty_price,
        "nifty_bullish": nifty_bullish,
        "india_vix": india_vix,
        "regime": "Bullish (Safe)" if regime_ok else "Caution (Nifty/VIX)",
        "count": len(stocks),
        "stocks": stocks,
    }


async def build_nse_scanner() -> Dict[str, Any]:
    cached = get_cache("nse_scanner_v5", ttl_sec=300)
    if cached:
        return cached
    data = await asyncio.to_thread(_sync_build_nse_scanner)
    set_cache("nse_scanner_v5", data)
    return data


# ==================== TRADE PERSISTENCE & MONITORS ====================
def _load_local_trades() -> List[Dict[str, Any]]:
    if LOCAL_TRADES_FILE.exists():
        try:
            return json.loads(LOCAL_TRADES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_local_trades(trades: List[Dict[str, Any]]) -> None:
    LOCAL_TRADES_FILE.write_text(json.dumps(trades, indent=2), encoding="utf-8")


async def fetch_all_trades() -> Tuple[str, List[Dict[str, Any]]]:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{SUPABASE_URL}/rest/v1/paper_trades?select=*&order=created_at.desc",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                    timeout=8.0,
                )
                if r.status_code == 200:
                    return "Supabase", r.json()
        except Exception:
            pass
    return "Local JSON", _load_local_trades()


async def insert_paper_trade(row: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{SUPABASE_URL}/rest/v1/paper_trades",
                    headers={
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "return=representation",
                    },
                    json=row,
                    timeout=8.0,
                )
                if r.status_code in (200, 201):
                    return "Supabase", r.json()[0]
                base_row = {k: row[k] for k in ["id", "symbol", "budget_inr", "buy_price_inr", "target_inr", "stop_loss_inr", "status", "created_at"]}
                r2 = await client.post(
                    f"{SUPABASE_URL}/rest/v1/paper_trades",
                    headers={
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "return=representation",
                    },
                    json=base_row,
                    timeout=8.0,
                )
                if r2.status_code in (200, 201):
                    return "Supabase", r2.json()[0]
        except Exception:
            pass
    trades = _load_local_trades()
    trades.insert(0, row)
    _save_local_trades(trades[:100])
    return "Local JSON", row


async def update_trade_status(trade_id: str, new_status: str, exit_price: float = 0.0, duration_str: str = "") -> None:
    now_ts = int(time.time())
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.patch(
                    f"{SUPABASE_URL}/rest/v1/paper_trades?id=eq.{trade_id}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
                    json={"status": new_status, "exit_price_inr": round(exit_price, 2), "closed_at": now_ts, "duration_str": duration_str},
                    timeout=8.0,
                )
                if r.status_code in (200, 204):
                    return
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/paper_trades?id=eq.{trade_id}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
                    json={"status": new_status},
                    timeout=8.0,
                )
                return
        except Exception:
            pass
    trades = _load_local_trades()
    for t in trades:
        if t.get("id") == trade_id:
            t["status"] = new_status
            t["exit_price_inr"] = round(exit_price, 2)
            t["closed_at"] = now_ts
            t["duration_str"] = duration_str
    _save_local_trades(trades)


async def delete_trade(trade_id: str) -> bool:
    """Remove one paper trade from Supabase (if configured) or the local JSON backup."""
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.delete(
                    f"{SUPABASE_URL}/rest/v1/paper_trades?id=eq.{trade_id}",
                    headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                             "Prefer": "return=representation"},
                    timeout=8.0,
                )
                if r.status_code in (200, 204) and (r.status_code == 204 or r.json()):
                    return True
        except Exception:
            pass
    trades = _load_local_trades()
    kept = [t for t in trades if t.get("id") != trade_id]
    if len(kept) != len(trades):
        _save_local_trades(kept)
        return True
    return False


async def build_boom_data(limit: int = 15) -> Dict[str, Any]:
    cache_key = f"boom_v1_{limit}"
    cached = get_cache(cache_key, ttl_sec=180)
    if cached:
        return cached
    async with httpx.AsyncClient() as client:
        dcx_inr = await fetch_coindcx_inr_tickers(client)
        coins = await boom_engine.build_boom_scan(client, dcx_inr, limit)
    if coins is None:
        stale = _CACHE.get(cache_key)
        if stale:
            return stale["data"]
        return {"count": 0, "coins": [], "error": "Binance data unavailable"}
    payload = {"count": len(coins), "coins": coins}
    set_cache(cache_key, payload)
    return payload


async def _research_gate(symbol: str, mode: str) -> Tuple[bool, str, Dict[str, Any]]:
    """Research a coin and say whether an auto paper-trade may be taken. Never raises."""
    try:
        async with httpx.AsyncClient() as client:
            dcx = await fetch_coindcx_inr_tickers(client)
        rep = await asyncio.wait_for(research_engine.research_coin(symbol, mode, dcx), timeout=120)
    except Exception as exc:
        rep = {"verdict": "Unknown", "error": type(exc).__name__}
    ok, note = research_engine.allows_entry(rep)
    return ok, note, rep


async def run_research_prefetch(limit: int = 10) -> Dict[str, Any]:
    """Deep research for today's best crypto picks, so the grades are ready in the morning."""
    data = await build_scanner_data(limit=25)
    coins = sorted([c for c in data.get("coins", []) if "(Wait)" not in c.get("signal", "")],
                   key=lambda c: c.get("swing_score", 0), reverse=True)[:limit]
    done = []
    for c in coins:
        try:
            async with httpx.AsyncClient() as client:
                dcx = await fetch_coindcx_inr_tickers(client)
            rep = await asyncio.wait_for(research_engine.research_coin(c["symbol"], "deep", dcx), timeout=150)
            done.append(f"{c['symbol']}:{rep.get('grade') or rep.get('verdict')}")
        except Exception:
            pass
        await asyncio.sleep(3)
    return {"researched": done}


async def _safe_prefetch() -> None:
    try:
        await run_research_prefetch()
    except Exception as e:
        print("research prefetch error:", repr(e))


async def run_boom_cycle(min_score: float = 70.0, max_open: int = 3) -> Dict[str, Any]:
    data = await build_boom_data()
    _, existing = await fetch_all_trades()
    holding = [t for t in existing if t.get("status") == "HOLDING"]
    held = {t.get("symbol", "").upper() for t in holding}
    slots = max(0, max_open - len([t for t in holding if t.get("market") == "BOOM"]))
    cands = [c for c in data["coins"] if c["signal"] == "Boom Setup" and c["score"] >= min_score
             and c["symbol"] not in held][:slots]
    gates = await asyncio.gather(*[_research_gate(c["symbol"], "quick") for c in cands]) if cands else []
    logged, blocked = [], []
    for c, (ok, note, _rep) in zip(cands, gates):
        if not ok:
            blocked.append(f"{c['symbol']}: {note}")
            continue
        row = {
            "id": str(uuid.uuid4())[:8], "symbol": c["symbol"], "market": "BOOM", "budget_inr": 1000.0,
            "buy_price_inr": c["price_inr"], "target_inr": c["target_inr"], "stop_loss_inr": c["stop_loss_inr"],
            "hold_label": c["hold_label"], "status": "HOLDING", "created_at": int(time.time()),
        }
        await insert_paper_trade(row)
        logged.append(c["symbol"])
        await send_telegram_message(
            f"🚀 <b>BOOM RADAR (Paper Logged)</b>\n<b>{c['symbol']}</b> | Score <b>{c['score']}</b> | Volume {c['vol_x']}x\n"
            f"Buy: ₹{c['price_inr']:,.4f} | Target: ₹{c['target_inr']:,.4f} | SL: ₹{c['stop_loss_inr']:,.4f}\n{note}"
        )
    return {"checked": len(data["coins"]), "auto_logged": logged, "blocked_by_research": blocked}


async def check_open_trades_tp_sl():
    _, trades = await fetch_all_trades()
    holding = [t for t in trades if t.get("status") == "HOLDING"]
    if not holding:
        return

    nse_prices: Dict[str, float] = {}
    if any((t.get("market") == "NSE" or ".NS" in str(t.get("symbol", ""))) for t in holding):
        nse_data = await build_nse_scanner()
        for s in nse_data["stocks"]:
            nse_prices[s["short_symbol"]] = s["price_inr"]
            nse_prices[s["symbol"]] = s["price_inr"]

    async with httpx.AsyncClient() as client:
        for t in holding:
            sym = t.get("symbol", "").upper()
            tp = float(t.get("target_inr") or 0)
            sl = float(t.get("stop_loss_inr") or 0)
            buy = float(t.get("buy_price_inr") or 1)
            budget = float(t.get("budget_inr") or 1000)
            created = int(t.get("created_at") or time.time())
            is_nse = (t.get("market") == "NSE" or ".NS" in sym)
            dur_str = format_duration(int(time.time()) - created)
            planned = t.get("hold_label") or "1-2 Days"

            outcome, exit_p = None, 0.0
            is_short = False
            if is_nse:
                live_p = nse_prices.get(sym)
                if not live_p:
                    continue
                is_short = (tp > 0 and tp < buy)
                hit_win = (live_p <= tp) if is_short else (tp > 0 and live_p >= tp)
                hit_loss = (sl > 0 and live_p >= sl) if is_short else (sl > 0 and live_p <= sl)
                if hit_win:
                    outcome, exit_p = "WIN", live_p
                elif hit_loss:
                    outcome, exit_p = "LOSS", live_p
            else:
                # Crypto: check every 15m candle since entry (catches spikes between polls)
                res = await crypto_engine.eval_crypto_trade(client, sym, buy, tp, sl, created)
                if res is None:
                    continue
                if res[0] is None:
                    if t.get("market") == "BOOM" and time.time() - created > boom_engine.MAX_AGE_H * 3600:
                        outcome, exit_p = "EXPIRED", res[1]
                    else:
                        continue
                else:
                    outcome, exit_p = res

            if not outcome:
                continue

            cost = 0.0025 if is_nse else crypto_engine.CRYPTO_COST
            raw_ret = ((buy - exit_p) / buy) if is_short else ((exit_p - buy) / buy)
            pnl = budget * (raw_ret - cost)
            side = "SELL" if is_short else "BUY"
            await update_trade_status(t["id"], outcome, exit_p, dur_str)
            if outcome == "WIN":
                await send_telegram_message(
                    f"🎯 <b>Target Hit! ({sym} {side})</b>\n"
                    f"Time Taken: <b>{dur_str}</b> (Planned: {planned})\n"
                    f"Exit: ₹{exit_p:,.2f} | Est. Net Profit (after fees/TDS): <b>{pnl:+,.2f}</b>"
                )
            elif outcome == "EXPIRED":
                await send_telegram_message(
                    f"⌛ <b>Boom expired ({sym})</b> after {dur_str}: no target/stop hit. Exit ~₹{exit_p:,.4f} | Est. Net: <b>{pnl:+,.2f}</b>"
                )
            else:
                await send_telegram_message(
                    f"🛑 <b>Stop-Loss Hit ({sym} {side})</b>\n"
                    f"Time Taken: <b>{dur_str}</b> (Planned: {planned})\n"
                    f"Exit: ₹{exit_p:,.2f} | Est. Net P&L (after fees/TDS): <b>{pnl:+,.2f}</b>"
                )


async def run_auto_signal_cycle(min_score: float = 68.0) -> Dict[str, Any]:
    data = await build_scanner_data(limit=25)
    _, existing = await fetch_all_trades()
    holding_syms = {t.get("symbol", "").upper() for t in existing if t.get("status") == "HOLDING"}

    candidates = [
        c for c in data["coins"]
        if c["swing_score"] >= min_score and c["symbol"] not in holding_syms and "(Wait)" not in c["signal"]
    ][:2]
    gates = await asyncio.gather(*[_research_gate(c["symbol"], "deep") for c in candidates]) if candidates else []

    logged, blocked = [], []
    for c, (ok, note, _rep) in zip(candidates, gates):
        if not ok:
            blocked.append(f"{c['symbol']}: {note}")
            continue
        row = {
            "id": str(uuid.uuid4())[:8],
            "symbol": c["symbol"],
            "market": "CRYPTO",
            "budget_inr": 1000.0,
            "buy_price_inr": c["price_inr"],
            "target_inr": c["target_inr"],
            "stop_loss_inr": c["stop_loss_inr"],
            "hold_label": c["hold_label"],
            "status": "HOLDING",
            "created_at": int(time.time()),
        }
        await insert_paper_trade(row)
        logged.append(c["symbol"])
        await send_telegram_message(
            f"🟢 <b>CRYPTO AUTO BUY (Paper Logged)</b>\n"
            f"Coin: <b>{c['name']} ({c['symbol']})</b> | Score: <b>{c['swing_score']}</b>\n"
            f"Buy: ₹{c['price_inr']:,.2f} | Hold: <b>{c['hold_label']}</b>\n"
            f"Target: ₹{c['target_inr']:,.2f} | SL: ₹{c['stop_loss_inr']:,.2f}\n{note}"
        )
    return {"checked": len(data["coins"]), "auto_logged": logged, "blocked_by_research": blocked}


async def run_nse_auto_cycle(min_score: float = 70.0) -> Dict[str, Any]:
    data = await build_nse_scanner()
    _, existing = await fetch_all_trades()
    holding_syms = {t.get("symbol", "").upper() for t in existing if t.get("status") == "HOLDING"}

    candidates = [
        s for s in data["stocks"]
        if s["swing_score"] >= min_score and s["short_symbol"] not in holding_syms and "(Wait)" not in s["signal"]
    ][:2]

    logged = []
    for s in candidates:
        row = {
            "id": str(uuid.uuid4())[:8],
            "symbol": s["short_symbol"],
            "market": "NSE",
            "budget_inr": 5000.0,
            "buy_price_inr": s["price_inr"],
            "target_inr": s["target_inr"],
            "stop_loss_inr": s["stop_loss_inr"],
            "hold_label": s["hold_label"],
            "status": "HOLDING",
            "created_at": int(time.time()),
        }
        await insert_paper_trade(row)
        logged.append(s["short_symbol"])
        await send_telegram_message(
            f"📈 <b>NSE STOCK AUTO BUY (Paper Logged)</b>\n"
            f"Stock: <b>{s['name']} ({s['short_symbol']})</b> | Score: <b>{s['swing_score']}</b>\n"
            f"Buy: ₹{s['price_inr']:,.2f} | Hold: <b>{s['hold_label']}</b>\n"
            f"Target: ₹{s['target_inr']:,.2f} | SL: ₹{s['stop_loss_inr']:,.2f}\n"
            f"Nifty 50: {data['nifty_50']} | VIX: {data['india_vix']}"
        )
    return {"regime": data["regime"], "auto_logged": logged}


async def build_and_send_briefing() -> Dict[str, Any]:
    data = await build_scanner_data(limit=25)
    news = await get_news_and_macro()
    fg = news.get("fear_and_greed", {})
    gem = news.get("gemini_analysis") or {}
    macro_line = gem.get("summary") or "Check panel for latest US Fed & ETF headlines."
    top_picks = [c for c in data["coins"] if c["swing_score"] >= 60][:3]
    picks_txt = "\n".join(
        f"• <b>{c['symbol']}</b>: ₹{c['price_inr']:,.2f} | Score <b>{c['swing_score']}</b> ({c['signal']})"
        for c in top_picks
    ) or "• No safe 60+ setups right now (Capital Protection Mode)."

    msg = (
        f"☀️ <b>ApexQuant 8 AM Crypto Briefing</b>\n"
        f"Fear &amp; Greed: <b>{fg.get('value', '--')} ({fg.get('classification', 'N/A')})</b> | BTC.D: <b>{data['btc_dominance']}%</b>\n\n"
        f"<b>US Macro &amp; News:</b>\n{macro_line}\n\n"
        f"<b>Top Safe INR Swing Picks:</b>\n{picks_txt}"
    )
    ok = await send_telegram_message(msg)
    return {"sent": ok, "picks": len(top_picks)}


async def background_monitor_loop():
    global _LAST_BRIEFING_DATE, _LAST_NSE_SCAN_DATE, _LAST_RESEARCH_DATE
    last_scan_ts = 0.0
    last_boom_ts = 0.0
    while True:
        try:
            await check_open_trades_tp_sl()
            ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            today_str = ist_now.strftime("%Y-%m-%d")

            if ist_now.hour == 8 and ist_now.minute < 15 and _LAST_BRIEFING_DATE != today_str:
                _LAST_BRIEFING_DATE = today_str
                await build_and_send_briefing()

            # Mon-Fri 9:30 AM IST NSE Market Open Scan
            if ist_now.weekday() < 5 and ist_now.hour == 9 and 30 <= ist_now.minute < 45 and _LAST_NSE_SCAN_DATE != today_str:
                _LAST_NSE_SCAN_DATE = today_str
                await run_nse_auto_cycle(min_score=70.0)

            if ist_now.hour == 3 and _LAST_RESEARCH_DATE != today_str:
                _LAST_RESEARCH_DATE = today_str
                _t = asyncio.create_task(_safe_prefetch())            # background: does not delay TP/SL checks
                _BG_TASKS.add(_t)
                _t.add_done_callback(_BG_TASKS.discard)

            if time.time() - last_boom_ts >= 1800:
                last_boom_ts = time.time()
                try:
                    await run_boom_cycle()
                except Exception as e:
                    print("boom cycle error:", repr(e))

            if time.time() - last_scan_ts >= 7200:
                last_scan_ts = time.time()
                await run_auto_signal_cycle()
        except Exception as e:
            print("bot loop error:", repr(e))
        await asyncio.sleep(300)


async def stock_schedule_loop():
    """Mon-Fri 9:00 AM IST (15 min before the NSE open): detailed pre-market report to Telegram."""
    global _LAST_PREMARKET_DATE
    while True:
        try:
            ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
            today = ist.strftime("%Y-%m-%d")
            if ist.weekday() < 5 and ist.hour == 9 and ist.minute < 10 and _LAST_PREMARKET_DATE != today:
                _LAST_PREMARKET_DATE = today
                await stock_premarket.send_premarket(send_telegram_message)
        except Exception as e:
            print("premarket error:", repr(e))
        await asyncio.sleep(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STOCK_RUNNER
    STOCK_RUNNER = stock_live.StockLiveRunner(send_telegram_message)
    tasks = [asyncio.create_task(background_monitor_loop()),
             asyncio.create_task(STOCK_RUNNER.run_forever()),
             asyncio.create_task(stock_schedule_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="ApexQuant Dual-Market v5 (Crypto + NSE)", version="5.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    if FRONTEND_FILE.exists():
        return FileResponse(FRONTEND_FILE)
    return {"status": "ApexQuant v5 API running. Visit /docs"}


@app.get("/coin.html", include_in_schema=False)
async def serve_coin_page():
    if COIN_FILE.exists():
        return FileResponse(COIN_FILE)
    raise HTTPException(status_code=404, detail="coin.html not found")


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "5.2.0", "gemini_models": GEMINI_MODELS, "ts": int(time.time())}


@app.get("/api/scanner")
async def scan_rising_coins(limit: int = Query(25, ge=5, le=100)):
    return await build_scanner_data(limit=limit)


@app.get("/api/stocks/scanner")
async def get_nse_scanner():
    data = await build_nse_scanner()
    r = STOCK_RUNNER
    if r is None or not r.streaming:
        return data
    stocks, live = [], 0
    for s in data["stocks"]:                       # swap the delayed Yahoo price for the live Angel One price
        p = r.live_price(s["short_symbol"])
        if p:
            live += 1
            s = {**s, "price_inr": round(p, 2), "live": True}
        stocks.append(s)
    out = {**data, "stocks": stocks, "live_count": live}
    if r.engine.nifty["ltp"]:
        out["nifty_50"], out["nifty_live"] = round(r.engine.nifty["ltp"], 2), True
    return out


@app.get("/api/stocks/live/status")
async def stock_live_status():
    return STOCK_RUNNER.status() if STOCK_RUNNER else {"configured": False, "streaming": False}


@app.get("/api/stocks/live/stream")
async def stock_live_stream(request: Request):
    """Server-Sent Events: the live screen (prices, alerts, P&L) is pushed every second, no refresh needed."""
    global _SSE_CLIENTS
    if STOCK_RUNNER is None:
        raise HTTPException(status_code=503, detail="Live radar not started")
    if _SSE_CLIENTS >= 6:
        raise HTTPException(status_code=429, detail="Too many live viewers")

    async def gen():
        global _SSE_CLIENTS
        _SSE_CLIENTS += 1
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    data = json.dumps(STOCK_RUNNER.snapshot(), ensure_ascii=False, separators=(",", ":"))
                    yield f"data: {data}\n\n"
                except Exception as exc:
                    print("live stream error:", repr(exc))
                await asyncio.sleep(1.0 if STOCK_RUNNER.streaming else 5.0)
        finally:
            _SSE_CLIENTS -= 1

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/stocks/live/journal")
async def stock_live_journal():
    return STOCK_RUNNER.engine.journal() if STOCK_RUNNER else {"date": "", "summary": {}, "alerts": []}


@app.api_route("/api/stocks/premarket/send", methods=["GET", "POST"])
async def stock_premarket_send(force: bool = False):
    global _LAST_PREMARKET_MANUAL
    if time.time() - _LAST_PREMARKET_MANUAL < 600:
        raise HTTPException(status_code=429, detail="Wait 10 minutes between manual reports")
    _LAST_PREMARKET_MANUAL = time.time()
    return await stock_premarket.send_premarket(send_telegram_message, force)


@app.get("/api/stocks/chart/{symbol}")
async def get_nse_chart(symbol: str, tf: str = Query("1d")):
    sym = symbol if symbol.endswith(".NS") else f"{symbol}.NS"
    period_map = {"15m": ("5d", "15m"), "1h": ("1mo", "1h"), "4h": ("4mo", "1d"), "1d": ("6mo", "1d")}
    period, interval = period_map.get(tf, ("4mo", "1d"))
    cache_key = f"nse_chart_{sym}_{interval}"
    cached = get_cache(cache_key, ttl_sec=180)
    if cached:
        return cached

    def _fetch():
        t = yf.Ticker(sym)
        hist = t.history(period=period, interval=interval)
        if hist.empty:
            return None
        candles = []
        for idx, row in hist.iterrows():
            candles.append({
                "time": int(idx.timestamp()),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "v": float(row["Volume"] or 0),
            })
        return candles

    raw_candles = await asyncio.to_thread(_fetch)
    if not raw_candles:
        raise HTTPException(status_code=404, detail=f"NSE symbol {sym} not found")

    closes = [c["close"] for c in raw_candles]
    ema20 = calc_ema(closes, 20) or closes[-1]
    rsi14 = calc_rsi(closes, 14) or 50.0
    atr14 = calc_atr([{"o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"], "v": c["v"]} for c in raw_candles], 14)

    # Intraday / Swing Call (BUY / SELL / HOLD)
    price = closes[-1]
    if price > ema20 and rsi14 >= 52 and rsi14 <= 72:
        call = "BUY"
        sl = round(price - atr14 * 1.2, 2)
        tp1 = round(price + atr14 * 1.8, 2)
        tp2 = round(price + atr14 * 3.0, 2)
    elif price < ema20 and rsi14 <= 45 and rsi14 >= 25:
        call = "SELL (Intraday)" if interval in ("15m", "1h") else "WAIT / AVOID"
        sl = round(price + atr14 * 1.2, 2)
        tp1 = round(price - atr14 * 1.8, 2)
        tp2 = round(price - atr14 * 3.0, 2)
    else:
        call = "HOLD / WAIT"
        sl = round(price - atr14 * 1.5, 2)
        tp1 = round(price + atr14 * 2.0, 2)
        tp2 = round(price + atr14 * 3.5, 2)

    candles = [{"time": c["time"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]} for c in raw_candles]
    payload = {
        "symbol": sym, "interval": interval, "call": call,
        "price_inr": round(price, 2), "rsi_14": round(rsi14, 1),
        "sl": sl, "tp1": tp1, "tp2": tp2, "candles": candles
    }
    set_cache(cache_key, payload)
    return payload


@app.get("/api/stocks/news")
async def get_indian_stock_news():
    cached = get_cache("nse_news_v5", ttl_sec=600)
    if cached:
        return cached
    headlines: List[str] = []
    async with httpx.AsyncClient() as client:
        for url in [
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            "https://www.moneycontrol.com/rss/marketreports.xml",
        ]:
            try:
                r = await client.get(url, timeout=8.0)
                if r.status_code == 200:
                    parsed = feedparser.parse(r.text)
                    for e in parsed.entries[:6]:
                        headlines.append(e.title)
            except Exception:
                pass

    ai_summary = None
    if GEMINI_API_KEY and headlines:
        prompt = (
            "You are a pro Indian NSE equity swing analyst. Summarize these headlines focusing on Nifty 50, "
            "RBI/macro, FII/DII flows, and top Indian sectors. Return valid JSON with keys: "
            "macro_bias (Bullish/Bearish/Neutral), summary (2-3 sentences).\n\nHeadlines:\n"
            + "\n".join(f"- {h}" for h in headlines[:8])
        )
        ai_summary, g_err = await gemini_json(prompt)
        if not ai_summary:
            ai_summary = {"error": g_err}
    elif not GEMINI_API_KEY:
        ai_summary = {"note": "Set GEMINI_API_KEY for Indian market AI summary."}

    if ai_summary is None:
        ai_summary = {"note": "No headlines available for AI summary right now."}
    payload = {"headlines": headlines[:10], "gemini_analysis": ai_summary}
    if "error" not in ai_summary:
        set_cache("nse_news_v5", payload)
    return payload


@app.post("/api/alerts/test")
async def test_telegram_alert():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"sent": False, "detail": "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"}
    data = await build_scanner_data(limit=15)
    top = data["coins"][0] if data["coins"] else None
    if not top:
        return {"sent": False, "detail": "No coins returned"}
    msg = (
        f"✅ <b>ApexQuant v5 Test Alert</b>\n"
        f"Top Pick: <b>{top['name']} ({top['symbol']})</b>\n"
        f"Price: ₹{top['price_inr']:,.2f} | Score: <b>{top['swing_score']}</b> ({top['signal']})"
    )
    ok = await send_telegram_message(msg)
    return {"sent": ok, "coin": top["symbol"]}


@app.post("/api/alerts/briefing")
async def trigger_morning_briefing():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"sent": False, "detail": "Telegram not configured"}
    return await build_and_send_briefing()


@app.post("/api/alerts/auto-cycle")
async def trigger_auto_cycle():
    return await run_auto_signal_cycle(min_score=65.0)


@app.post("/api/stocks/auto-cycle")
async def trigger_nse_auto_cycle():
    return await run_nse_auto_cycle(min_score=65.0)


ANALYSIS_TFS = {"15m", "1h", "4h", "1d"}


@app.get("/api/analysis/{symbol}")
async def coin_analysis(symbol: str, tf: str = Query("4h")):
    """Rule-based learning analysis for one coin (used by frontend/coin.html)."""
    base = symbol.upper().replace("USDT", "").strip()
    if not base.isalnum() or len(base) > 12:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    if tf not in ANALYSIS_TFS:
        tf = "4h"

    cache_key = f"analysis_{base}_{tf}"
    cached = get_cache(cache_key, ttl_sec=60)
    if cached:
        return cached

    pair = f"{base}USDT"
    async with httpx.AsyncClient() as client:
        c_tf, c4, c1d, btc_d, tick, dcx_inr = await asyncio.gather(
            crypto_engine.fetch_klines(client, pair, tf, 200),
            crypto_engine.fetch_klines(client, pair, "4h", 100),
            crypto_engine.fetch_klines(client, pair, "1d", 40),
            crypto_engine.fetch_klines(client, "BTCUSDT", "1d", 40),
            crypto_engine.bget(client, "/api/v3/ticker/24hr", {"symbol": pair}),
            fetch_coindcx_inr_tickers(client),
        )
        if not c_tf or not c4 or not c1d:
            raise HTTPException(status_code=404, detail=f"{base}/USDT Binance-এ পাওয়া যায়নি")
        usdt_inr = await crypto_engine.fetch_usdt_inr(client, dcx_inr)

    btc_closed = crypto_engine.closed_only(btc_d)
    btc_7d = (btc_closed[-1]["c"] / btc_closed[-8]["c"] - 1) * 100 if len(btc_closed) >= 8 else 0.0
    pct_24h = float(tick["priceChangePercent"]) if isinstance(tick, dict) and tick.get("priceChangePercent") else 0.0

    result = await asyncio.to_thread(
        analysis_engine.build_analysis, base, tf, c_tf, c4, c1d, pct_24h, btc_7d, usdt_inr
    )
    if not result:
        raise HTTPException(status_code=404, detail="বিশ্লেষণের জন্য যথেষ্ট ক্যান্ডেল নেই")
    set_cache(cache_key, result)
    return result


@app.get("/api/news-macro")
async def get_news_and_macro():
    cached = get_cache("news_macro", ttl_sec=300)
    if cached:
        return cached
    fear_greed = {}
    headlines: List[str] = []
    btc_dom = 58.0

    async with httpx.AsyncClient() as client:
        btc_dom = await fetch_btc_dominance(client)
        try:
            fg_resp = await client.get("https://api.alternative.me/fng/?limit=1", timeout=8.0)
            if fg_resp.status_code == 200:
                fg_data = fg_resp.json().get("data", [{}])[0]
                fear_greed = {"value": int(fg_data.get("value", 50)), "classification": fg_data.get("value_classification", "Neutral")}
        except Exception:
            fear_greed = {"value": 50, "classification": "Unavailable"}

        for rss_url in ["https://www.coindesk.com/arc/outboundfeeds/rss/", "https://cointelegraph.com/rss"]:
            try:
                r = await client.get(rss_url, timeout=8.0)
                if r.status_code == 200:
                    for entry in feedparser.parse(r.text).entries[:6]:
                        headlines.append(entry.title)
            except Exception:
                pass

    ai_summary = None
    if GEMINI_API_KEY and headlines:
        prompt = (
            "You are a pro crypto spot swing analyst. Summarize these latest headlines focusing on "
            "US Fed/macro policy, ETF flows, and top coin catalysts. Return valid JSON with keys: "
            "macro_bias (Bullish/Bearish/Neutral), summary (2-3 sentences).\n\nHeadlines:\n"
            + "\n".join(f"- {h}" for h in headlines[:10])
        )
        ai_summary, g_err = await gemini_json(prompt)
        if not ai_summary:
            ai_summary = {"error": g_err}
    elif not GEMINI_API_KEY:
        ai_summary = {"note": "Set GEMINI_API_KEY for live AI summary."}

    if ai_summary is None:
        ai_summary = {"note": "No headlines available for AI summary right now."}
    payload = {"btc_dominance": btc_dom, "fear_and_greed": fear_greed, "headlines": headlines[:10], "gemini_analysis": ai_summary}
    if "error" not in ai_summary:  # retry Gemini on the next request instead of caching a failure
        set_cache("news_macro", payload)
    return payload


class PaperTradeIn(BaseModel):
    symbol: str
    market: str = "CRYPTO"
    budget_inr: float
    buy_price_inr: float
    target_inr: float
    stop_loss_inr: float
    hold_label: str = "1-2 Days (Swing)"


@app.get("/api/trades")
async def list_trades():
    storage, trades = await fetch_all_trades()
    return {"storage": storage, "trades": trades}


@app.post("/api/trades")
async def create_trade(trade: PaperTradeIn):
    row = {
        "id": str(uuid.uuid4())[:8],
        "symbol": trade.symbol.upper(),
        "market": trade.market.upper(),
        "budget_inr": round(trade.budget_inr, 2),
        "buy_price_inr": round(trade.buy_price_inr, 2),
        "target_inr": round(trade.target_inr, 2),
        "stop_loss_inr": round(trade.stop_loss_inr, 2),
        "hold_label": trade.hold_label,
        "status": "HOLDING",
        "created_at": int(time.time()),
    }
    storage, saved = await insert_paper_trade(row)
    return {"storage": storage, "trade": saved}


@app.delete("/api/trades/{trade_id}")
async def remove_trade(trade_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", trade_id):
        raise HTTPException(status_code=400, detail="Invalid trade id")
    if not await delete_trade(trade_id):
        raise HTTPException(status_code=404, detail="Trade not found (or delete not allowed on Supabase)")
    return {"deleted": trade_id}


@app.get("/api/gemini-check")
async def gemini_check():
    """Open this URL to see whether Gemini works and, if not, exactly why."""
    data, err = await gemini_json('Reply with this JSON only: {"ok": true}')
    return {"ok": bool(data), "key_set": bool(gemini_pool.load_keys()), "keys": len(gemini_pool.load_keys()), "models_tried": GEMINI_MODELS, "error": err or None}


@app.get("/api/boom")
async def boom_radar(limit: int = Query(15, ge=5, le=30)):
    return await build_boom_data(limit=limit)


@app.get("/api/research")
async def research_list():
    return {"items": await research_engine.summaries()}


@app.get("/api/research/{symbol}")
async def research_one(symbol: str, mode: str = Query("quick", pattern="^(quick|deep)$"), refresh: bool = False):
    if not re.fullmatch(r"[A-Za-z0-9]{1,15}", symbol):
        raise HTTPException(status_code=400, detail="Invalid symbol")
    async with httpx.AsyncClient() as client:
        dcx = await fetch_coindcx_inr_tickers(client)
    return await research_engine.research_coin(symbol, mode, dcx, refresh)


@app.post("/api/boom/auto-cycle")
async def trigger_boom_cycle():
    return await run_boom_cycle(min_score=65.0)


@app.get("/api/trades/{trade_id}/why")
async def trade_why(trade_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", trade_id):
        raise HTTPException(status_code=400, detail="Invalid trade id")
    _, trades = await fetch_all_trades()
    t = next((x for x in trades if x.get("id") == trade_id), None)
    if not t or t.get("market") == "NSE":
        raise HTTPException(status_code=404, detail="Crypto trade not found")
    async with httpx.AsyncClient() as client:
        lines = await boom_engine.explain_trade(
            client, t.get("symbol", ""), float(t.get("buy_price_inr") or 0), float(t.get("target_inr") or 0),
            float(t.get("stop_loss_inr") or 0), int(t.get("created_at") or 0), t.get("status", "HOLDING"))
    return {"id": trade_id, "why": lines}


@app.get("/api/trades/export")
async def export_trades_csv():
    _, trades = await fetch_all_trades()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "Market", "Symbol", "Budget (INR)", "Buy (INR)", "Target (INR)", "SL (INR)", "Hold", "Status", "Duration"])
    for t in trades:
        writer.writerow([
            t.get("id"), t.get("market", "CRYPTO"), t.get("symbol"), t.get("budget_inr"),
            t.get("buy_price_inr"), t.get("target_inr"), t.get("stop_loss_inr"),
            t.get("hold_label"), t.get("status"), t.get("duration_str", "")
        ])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=apexquant_trades.csv"})
