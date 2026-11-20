"""
crypto_engine.py  (put next to main.py in /backend)

Replaces the CoinGecko-based crypto scanner. Uses only Binance public market data
(no API key, no account) + CoinDCX INR prices that main.py already fetches.
"""
import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

# data-api.binance.vision = Binance public market-data mirror (works from cloud servers)
BASES = ["https://data-api.binance.vision", "https://api.binance.com"]
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "AEUR", "EUR", "USDE", "XUSD", "BUSD", "UST", "USD1"}
MIN_QUOTE_VOL = 20_000_000   # min 24h volume (USDT): liquidity filter
RSI_MAX = 80.0               # skip overheated coins
CRYPTO_COST = 0.012          # approx round-trip cost: exchange fees + India 1% TDS


PEGGED_EXTRA = {"WBTC", "WBETH", "WETH", "STETH", "PAXG", "XAUT", "EURI", "GBP", "TRY", "BRL"}


def is_pegged_name(base: str) -> bool:
    """Stablecoins / wrapped / gold-pegged tokens: they can't 'boom', so skip them."""
    b = base.upper()
    blocked = {x.strip().upper() for x in os.getenv("COIN_BLOCKLIST", "NVDAB,SOXLB").split(",") if x.strip()}
    return b in STABLES or b in PEGGED_EXTRA or b in blocked or b.startswith("USD") or b.endswith("USD")


# ------------------------------------------------------------ indicators
def ema(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(vals: List[float], n: int = 14) -> Optional[float]:
    if len(vals) <= n:
        return None
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    ag, al = gain / n, loss / n
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(c: List[Dict[str, float]], n: int = 14) -> Optional[float]:
    if len(c) < n + 1:
        return None
    trs = [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
           for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx(c: List[Dict[str, float]], n: int = 14) -> Optional[float]:
    if len(c) < 2 * n + 1:
        return None
    tr, pdm, mdm = [], [], []
    for i in range(1, len(c)):
        h, l, pc = c[i]["h"], c[i]["l"], c[i - 1]["c"]
        up, dn = h - c[i - 1]["h"], c[i - 1]["l"] - l
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    a_tr, p, m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dx = []
    for i in range(n, len(tr) + 1):
        if i > n:
            a_tr = a_tr - a_tr / n + tr[i - 1]
            p = p - p / n + pdm[i - 1]
            m = m - m / n + mdm[i - 1]
        if a_tr == 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * p / a_tr, 100 * m / a_tr
        s = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / s if s else 0.0)
    if len(dx) < n:
        return None
    a = sum(dx[:n]) / n
    for d in dx[n:]:
        a = (a * (n - 1) + d) / n
    return a


def obv_rising(c: List[Dict[str, float]], look: int = 5) -> bool:
    obv = [0.0]
    for i in range(1, len(c)):
        if c[i]["c"] > c[i - 1]["c"]:
            obv.append(obv[-1] + c[i]["v"])
        elif c[i]["c"] < c[i - 1]["c"]:
            obv.append(obv[-1] - c[i]["v"])
        else:
            obv.append(obv[-1])
    return len(obv) > look and obv[-1] > obv[-1 - look]


def rvol(c: List[Dict[str, float]], n: int = 20) -> float:
    if len(c) < n + 1:
        return 1.0
    avg = sum(x["v"] for x in c[-n - 1:-1]) / n
    return c[-1]["v"] / avg if avg > 0 else 1.0


def closed_only(c: Optional[List[Dict[str, float]]]) -> List[Dict[str, float]]:
    """Drop the still-forming last candle."""
    if not c:
        return []
    return c[:-1] if c[-1]["ct"] > time.time() * 1000 else c


# ------------------------------------------------------------ data
async def bget(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> Any:
    for base in BASES:
        try:
            r = await client.get(base + path, params=params, timeout=12.0)
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


async def fetch_klines(client: httpx.AsyncClient, pair: str, interval: str, limit: int = 100,
                       start_ms: Optional[int] = None) -> Optional[List[Dict[str, float]]]:
    params: Dict[str, Any] = {"symbol": pair, "interval": interval, "limit": limit}
    if start_ms:
        params["startTime"] = int(start_ms)
    data = await bget(client, "/api/v3/klines", params)
    if not data:
        return None
    return [{"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
             "c": float(k[4]), "v": float(k[5]), "ct": k[6]} for k in data]


async def fetch_usdt_inr(client: httpx.AsyncClient, dcx_inr: Dict[str, float]) -> Optional[float]:
    """USDT->INR from CoinDCX (already fetched by main.py), fallback CoinPaprika."""
    if dcx_inr.get("usdt"):
        return dcx_inr["usdt"]
    try:
        r = await client.get("https://api.coinpaprika.com/v1/tickers/usdt-tether?quotes=INR", timeout=8.0)
        if r.status_code == 200:
            return float(r.json()["quotes"]["INR"]["price"])
    except Exception:
        pass
    return None


# ------------------------------------------------------------ analysis
def analyse(base: str, c4: List[Dict[str, float]], c1d: List[Dict[str, float]],
            pct_24h: float, btc_7d: float) -> Optional[Dict[str, Any]]:
    c4, c1d = closed_only(c4), closed_only(c1d)
    if len(c4) < 60 or len(c1d) < 31:
        return None
    closes = [x["c"] for x in c4]
    price = closes[-1]
    e20, a14, atr14 = ema(closes, 20), adx(c4), atr(c4)
    if None in (e20, a14, atr14):
        return None
    r14 = rsi(closes) or 50.0
    rv, obv_up, above = rvol(c4), obv_rising(c4), price > e20

    dc = [x["c"] for x in c1d]
    p7 = (dc[-1] / dc[-8] - 1) * 100
    p10 = (dc[-1] / dc[-11] - 1) * 100
    p30 = (dc[-1] / dc[-31] - 1) * 100
    rs_btc = p7 - btc_7d

    score = 35.0
    if above:
        score += 20
    if a14 >= 20:
        score += 15
    if obv_up or rv >= 1.25:
        score += 15
    if rs_btc > 1.5:
        score += 10
    if p7 > 0 and p30 > 0:
        score += 10
    knife = (not above) and (p30 < -15 or p7 < -7)
    if knife:
        score = min(score, 54.0)
    score = round(min(100.0, score), 1)

    if knife:
        signal = "Falling Knife (Wait)"
    elif r14 > RSI_MAX:
        signal = "Overheated (Wait)"
    elif score >= 75 and above and a14 >= 20:
        signal = "Confirmed Breakout"
    elif score >= 65 and above:
        signal = "Trend Reversal"
    elif score >= 58:
        signal = "Accumulation"
    else:
        signal = "Watchlist"

    # ATR-based stop (clamped 2%-8%), target = 2x risk
    sl_dist = min(max(1.5 * atr14 / price, 0.02), 0.08)
    tp_dist = 2 * sl_dist
    if a14 >= 28 and rv >= 1.3:
        label = "4-12 Hours (Fast)"
    elif p30 > 0 and above:
        label = "3-5 Days (Trend)"
    else:
        label = "1-2 Days (Swing)"

    return {
        "usdt": price, "sl_dist": sl_dist, "tp_dist": tp_dist, "label": label,
        "pct_24h": pct_24h, "p7": p7, "p10": p10, "p30": p30, "rs_btc": rs_btc,
        "adx": a14, "rsi": r14, "rvol": rv, "above": above, "score": score, "signal": signal,
        # extra fields used by analysis_engine.build_analysis (coin.html screen)
        "atr": atr14, "ema20": e20, "obv_up": obv_up, "knife": knife,
    }


async def build_crypto_scan(client: httpx.AsyncClient, dcx_inr: Dict[str, float],
                            limit: int = 25) -> Optional[List[Dict[str, Any]]]:
    tick = await bget(client, "/api/v3/ticker/24hr")
    if not tick:
        return None
    rows = [t for t in tick
            if t["symbol"].endswith("USDT") and not is_pegged_name(t["symbol"][:-4])
            and float(t["quoteVolume"]) >= MIN_QUOTE_VOL]
    by_vol = sorted(rows, key=lambda t: float(t["quoteVolume"]), reverse=True)[:max(15, min(limit, 40))]
    gainers = sorted(rows, key=lambda t: float(t["priceChangePercent"]), reverse=True)[:10]
    chosen = list({t["symbol"]: t for t in by_vol + gainers}.values())

    usdt_inr = await fetch_usdt_inr(client, dcx_inr)

    btc_d = await fetch_klines(client, "BTCUSDT", "1d", 40)
    btc_d = closed_only(btc_d)
    btc_7d = (btc_d[-1]["c"] / btc_d[-8]["c"] - 1) * 100 if len(btc_d) >= 8 else 0.0

    sem = asyncio.Semaphore(8)

    async def one(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with sem:
            c4 = await fetch_klines(client, t["symbol"], "4h", 100)
            c1d = await fetch_klines(client, t["symbol"], "1d", 40)
        if not c4 or not c1d:
            return None
        a = analyse(t["symbol"][:-4], c4, c1d, float(t["priceChangePercent"]), btc_7d)
        if not a:
            return None
        base = t["symbol"][:-4]
        dcx = dcx_inr.get(base.lower())
        if dcx:
            price_inr, src = dcx, "CoinDCX INR"
        elif usdt_inr:
            price_inr, src = a["usdt"] * usdt_inr, "Binance x USDT/INR"
        else:
            return None
        return {
            "id": base.lower(), "symbol": base, "name": base,
            "price_inr": round(price_inr, 4 if price_inr < 100 else 2),
            "target_inr": round(price_inr * (1 + a["tp_dist"]), 4 if price_inr < 100 else 2),
            "stop_loss_inr": round(price_inr * (1 - a["sl_dist"]), 4 if price_inr < 100 else 2),
            "hold_label": a["label"], "price_source": src,
            "change_24h_pct": round(a["pct_24h"], 2), "change_7d_pct": round(a["p7"], 2),
            "change_10d_pct": round(a["p10"], 2), "change_30d_pct": round(a["p30"], 2),
            "adx_4h": round(a["adx"], 1), "rsi_4h": round(a["rsi"], 1), "rvol": round(a["rvol"], 2),
            "above_ema20": a["above"], "rs_vs_btc": round(a["rs_btc"], 2),
            "swing_score": a["score"], "signal": a["signal"], "sl_pct": round(a["sl_dist"] * 100, 2),
        }

    res = [r for r in await asyncio.gather(*[one(t) for t in chosen]) if r]
    res.sort(key=lambda x: x["swing_score"], reverse=True)
    return res[:limit]


# ------------------------------------------------------------ trade evaluation
async def eval_crypto_trade(client: httpx.AsyncClient, base: str, buy_inr: float, tp_inr: float,
                            sl_inr: float, created_at: int) -> Optional[Tuple[Optional[str], float]]:
    """
    Walk 15-minute candles since entry. Returns (outcome, exit_price_inr) where outcome is
    "WIN" / "LOSS" / None (still open). Returns None if candles could not be fetched.
    Trade levels are in INR, candles in USDT, so levels are mapped by % from the entry candle
    (small approximation: entry ref = open of the 15m candle containing the entry time).
    If SL and TP are inside the same candle, SL is assumed first (pessimistic).
    """
    cs = await fetch_klines(client, f"{base.upper()}USDT", "15m", 1000, start_ms=(int(created_at) - 900) * 1000)
    if not cs or len(cs) < 2 or buy_inr <= 0:
        return None
    ref = cs[0]["o"]
    tp_u, sl_u = ref * (tp_inr / buy_inr), ref * (sl_inr / buy_inr)
    for k in cs[1:]:
        if sl_inr > 0 and k["l"] <= sl_u:
            return "LOSS", sl_inr
        if tp_inr > 0 and k["h"] >= tp_u:
            return "WIN", tp_inr
    return None, buy_inr * (cs[-1]["c"] / ref)
