"""
boom_engine.py  (put next to main.py in /backend)

"Boom radar": finds coins that show EARLY signs of a sudden move (volume surge + breakout out of a quiet range).
It is rule-based and cannot predict the future. Every score comes with the reasons for and against, and
explain_trade() later says, from real candles, what the price actually did after entry.
"""
from typing import Any, Dict, List, Optional

import httpx

try:
    from . import crypto_engine as ce
except ImportError:
    import crypto_engine as ce

MIN_VOL = 2_000_000    # min 24h volume (USDT): thin coins can't be bought/sold at the shown price
LATE_PCT = 30.0        # already up this much in 24h = too late
MAX_AGE_H = 72         # boom trade that did nothing in 72h is closed as EXPIRED


def analyse_boom(base: str, c1h: List[Dict[str, float]], pct_24h: float, btc_24h: float) -> Optional[Dict[str, Any]]:
    c = ce.closed_only(c1h)
    if len(c) < 60:
        return None
    closes = [x["c"] for x in c]
    price = closes[-1]
    prev = c[-25:-1]
    hi24, lo24 = max(x["h"] for x in prev), min(x["l"] for x in prev)
    rng = (hi24 - lo24) / price * 100
    avgv = sum(x["v"] for x in prev) / 24
    rv1 = c[-1]["v"] / avgv if avgv > 0 else 1.0
    rv3 = (sum(x["v"] for x in c[-3:]) / 3) / avgv if avgv > 0 else 1.0
    vmax = max(rv1, rv3)
    e20, e20p = ce.ema(closes, 20), ce.ema(closes[:-3], 20)
    r = ce.rsi(closes) or 50.0
    a = ce.atr(c) or price * 0.02
    broke = price > hi24
    near = (not broke) and price >= hi24 * 0.99
    green = sum(1 for x in c[-3:] if x["c"] >= x["o"])

    s, ok, no = 0.0, [], []
    if vmax >= 5:
        s += 30; ok.append(f"ভলিউম হঠাৎ {vmax:.1f} গুণ বেড়েছে, বড় টাকা ঢুকছে")
    elif vmax >= 3:
        s += 22; ok.append(f"ভলিউম {vmax:.1f} গুণ বেড়েছে")
    elif vmax >= 2:
        s += 12; ok.append(f"ভলিউম {vmax:.1f} গুণ, একটু বেড়েছে")
    else:
        no.append(f"ভলিউম বাড়েনি ({vmax:.1f} গুণ), বুমের জন্য অন্তত ৩ গুণ লাগে")
    if broke:
        s += 20; ok.append("গত ২৪ ঘণ্টার সর্বোচ্চ দাম ভেঙে উঠেছে")
    elif near:
        s += 8; ok.append("২৪ ঘণ্টার সর্বোচ্চের ১% এর মধ্যে, ভাঙার চেষ্টা করছে")
    else:
        no.append("এখনো ২৪ ঘণ্টার সর্বোচ্চের নিচে")
    if rng < 5:
        s += 15; ok.append(f"আগের ২৪ ঘণ্টা মাত্র {rng:.1f}% রেঞ্জে চুপচাপ ছিল (চাপা স্প্রিং)")
    elif rng < 8:
        s += 8; ok.append(f"আগের ২৪ ঘণ্টা {rng:.1f}% রেঞ্জে ছিল")
    else:
        no.append(f"আগেই {rng:.0f}% ওঠানামা করেছে, চাপা স্প্রিং নয়")
    if e20 and e20p and price > e20 and e20 > e20p:
        s += 10; ok.append("দাম EMA20-র ওপরে আর EMA উঠছে")
    else:
        no.append("EMA20-র ওপরে পরিষ্কার ট্রেন্ড নেই")
    if 55 <= r <= 80:
        s += 10; ok.append(f"RSI {r:.0f}: জোর আছে, এখনো অতি-গরম নয়")
    elif r > 80:
        no.append(f"RSI {r:.0f}: অতি-গরম")
    else:
        no.append(f"RSI {r:.0f}: জোর কম")
    if green >= 2:
        s += 5; ok.append("শেষ ৩ ঘণ্টার অন্তত ২টা ক্যান্ডেল সবুজ")
    late = pct_24h > LATE_PCT
    if late:
        s -= 35; no.append(f"২৪ ঘণ্টায় আগেই +{pct_24h:.0f}% উঠে গেছে, দেরি হয়ে গেছে")
    if btc_24h < -4:
        s -= 10; no.append(f"বিটকয়েন আজ {btc_24h:.1f}% পড়েছে, বাজার দুর্বল")
    s = round(max(0.0, min(100.0, s)), 1)

    if late:
        sig = "Late (Wait)"
    elif r > 85:
        sig = "Overheated (Wait)"
    elif s >= 70:
        sig = "Boom Setup"
    elif s >= 55:
        sig = "Warming Up"
    else:
        sig = "Quiet"

    sl = min(max(1.8 * a / price, 0.04), 0.08)
    tp = 2.5 * sl
    if vmax >= 5 and broke:
        hold = "১ থেকে ৬ ঘণ্টা (তীব্র)"
    elif broke or near:
        hold = "১ থেকে ২ দিন"
    else:
        hold = "৩ থেকে ৭ দিন (ধৈর্য লাগবে)"
    return {"usdt": price, "sl": sl, "tp": tp, "hold": hold, "score": s, "signal": sig, "vol_x": round(vmax, 1),
            "ok": ok, "no": no}


async def build_boom_scan(client: httpx.AsyncClient, dcx_inr: Dict[str, float], limit: int = 15) -> Optional[List[Dict[str, Any]]]:
    tick = await ce.bget(client, "/api/v3/ticker/24hr")
    if not tick:
        return None
    have = set(dcx_inr)
    rows = [t for t in tick
            if t["symbol"].endswith("USDT") and not ce.is_pegged_name(t["symbol"][:-4])
            and float(t["quoteVolume"]) >= MIN_VOL
            and (not have or t["symbol"][:-4].lower() in have)]   # only coins you can buy in INR on CoinDCX
    movers = sorted(rows, key=lambda t: float(t["priceChangePercent"]), reverse=True)[:30]
    liquid = sorted(rows, key=lambda t: float(t["quoteVolume"]), reverse=True)[:20]
    chosen = list({t["symbol"]: t for t in movers + liquid}.values())
    btc_24h = next((float(t["priceChangePercent"]) for t in tick if t["symbol"] == "BTCUSDT"), 0.0)
    usdt_inr = await ce.fetch_usdt_inr(client, dcx_inr)

    import asyncio
    sem = asyncio.Semaphore(8)

    async def one(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        base = t["symbol"][:-4]
        async with sem:
            c1h = await ce.fetch_klines(client, t["symbol"], "1h", 100)
        if not c1h:
            return None
        a = analyse_boom(base, c1h, float(t["priceChangePercent"]), btc_24h)
        if not a:
            return None
        dcx = dcx_inr.get(base.lower())
        price_inr = dcx or (a["usdt"] * usdt_inr if usdt_inr else None)
        if not price_inr:
            return None
        d = 4 if price_inr < 100 else 2
        return {"symbol": base, "price_inr": round(price_inr, d),
                "target_inr": round(price_inr * (1 + a["tp"]), d), "stop_loss_inr": round(price_inr * (1 - a["sl"]), d),
                "sl_pct": round(a["sl"] * 100, 1), "hold_label": a["hold"],
                "change_24h_pct": round(float(t["priceChangePercent"]), 1), "vol_x": a["vol_x"],
                "score": a["score"], "signal": a["signal"], "ok": a["ok"], "no": a["no"]}

    res = [r for r in await asyncio.gather(*[one(t) for t in chosen]) if r]
    res.sort(key=lambda x: x["score"], reverse=True)
    return res[:limit]


async def explain_trade(client: httpx.AsyncClient, base: str, buy_inr: float, tp_inr: float, sl_inr: float,
                        created_at: int, status: str) -> List[str]:
    """Plain-language 'why' from the real 15m candles after entry."""
    cs = await ce.fetch_klines(client, f"{base.upper()}USDT", "15m", 1000, start_ms=(int(created_at) - 900) * 1000)
    if not cs or len(cs) < 2 or buy_inr <= 0:
        return ["পুরনো ক্যান্ডেল পাওয়া যায়নি, তাই ব্যাখ্যা দেওয়া গেল না।"]
    ref = cs[0]["o"]
    after = cs[1:]
    hi, lo = max(k["h"] for k in after), min(k["l"] for k in after)
    mfe, mae = (hi / ref - 1) * 100, (lo / ref - 1) * 100
    tp_pct, sl_pct = (tp_inr / buy_inr - 1) * 100, (1 - sl_inr / buy_inr) * 100
    tp_u, sl_u = ref * tp_inr / buy_inr, ref * sl_inr / buy_inr
    hit_h = None
    for i, k in enumerate(after, 1):
        if k["l"] <= sl_u or k["h"] >= tp_u:
            hit_h = i * 0.25
            break
    lines = [f"কেনার পর সর্বোচ্চ {mfe:+.1f}% উঠেছিল, সবচেয়ে বেশি {mae:+.1f}% নেমেছিল (টার্গেট +{tp_pct:.1f}%, স্টপ-লস -{sl_pct:.1f}%)।"]
    if status == "WIN":
        lines.append(f"প্রায় {hit_h:.1f} ঘণ্টায় টার্গেট ছুঁয়েছে। এই ট্রেডে ভলিউম আর ব্রেকআউটের লক্ষণ কাজ করেছে।" if hit_h else "টার্গেট ছুঁয়েছে।")
    elif status == "LOSS":
        if mfe >= tp_pct * 0.5:
            lines.append("টার্গেটের অর্ধেকের বেশি উঠে তারপর উল্টে স্টপ-লসে লেগেছে। মানে বুম শুরু হয়ে নিভে গেছে (ফেক ব্রেকআউট)। ঢুকে কিছুটা লাভে এলে স্টপ-লস কেনা দামে তুলে আনলে লস বাঁচত।")
        else:
            lines.append("কেনার পর দাম প্রায় ওঠেইনি, সরাসরি নেমেছে। মানে ব্রেকআউট আসলে শুরুই হয়নি, সংকেতটা ভুল ছিল।")
    elif status == "EXPIRED":
        lines.append("৭২ ঘণ্টায় টার্গেট বা স্টপ-লস কোনোটাই লাগেনি। বুম আসেনি, শুধু টাকা আটকে ছিল।")
    else:
        lines.append("ট্রেড এখনো চলছে, ফলাফল এখনো হয়নি।")
    return lines
