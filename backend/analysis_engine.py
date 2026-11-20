"""
analysis_engine.py  (put next to main.py in /backend)

Rule-based (no AI guessing) analysis for the "learn" coin screen:
trend name + reasons, candle pattern names, support/resistance, trade plan, score checklist.
Every sentence is built from real numbers, so nothing is made up.
"""
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import crypto_engine as ce
except ImportError:
    import crypto_engine as ce

CAPITAL = 100000.0   # virtual paper capital
RISK_PCT = 1.0       # risk per trade (% of capital)
MAX_POS_PCT = 25.0   # max position size (% of capital)
MIN_SCORE = 70.0     # bot's entry threshold


def fp(x: float) -> str:
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.6f}".rstrip("0")


# ------------------------------------------------------------ helpers
def ema_series(vals: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def pivots(c: List[Dict[str, float]], k: int = 3) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs, lows = [], []
    for i in range(k, len(c) - k):
        win = c[i - k:i + k + 1]
        if c[i]["h"] == max(x["h"] for x in win):
            highs.append((i, c[i]["h"]))
        if c[i]["l"] == min(x["l"] for x in win):
            lows.append((i, c[i]["l"]))
    return highs, lows


def sr_levels(c: List[Dict[str, float]], price: float) -> Tuple[List[float], List[float]]:
    highs, lows = pivots(c[-150:], 3)
    res = sorted({round(h, 8) for _, h in highs if h > price * 1.002})[:2]
    sup = sorted({round(l, 8) for _, l in lows if l < price * 0.998}, reverse=True)[:2]
    return res, sup


# ------------------------------------------------------------ trend
def classify_trend(c: List[Dict[str, float]]) -> Dict[str, Any]:
    closes = [x["c"] for x in c]
    price = closes[-1]
    e20 = ema_series(closes, 20)[-1]
    e50 = ema_series(closes, 50)[-1]
    a = ce.adx(c) or 0.0
    highs, lows = pivots(c[-120:], 3)
    hh = hl = lh = ll = None
    if len(highs) >= 2:
        hh, lh = highs[-1][1] > highs[-2][1], highs[-1][1] < highs[-2][1]
    if len(lows) >= 2:
        hl, ll = lows[-1][1] > lows[-2][1], lows[-1][1] < lows[-2][1]

    reasons: List[str] = []
    if e20 is None or e50 is None:
        return {"key": "unknown", "name": "Unknown", "name_bn": "অজানা", "reasons": ["ক্যান্ডেল কম, ট্রেন্ড বলা যাচ্ছে না"],
                "meaning": "", "adx": round(a, 1)}

    reasons.append(f"দাম {fp(price)} — EMA20 ({fp(e20)}) এর {'ওপরে ✅' if price > e20 else 'নিচে ❌'}")
    reasons.append(f"EMA20 ({fp(e20)}) বনাম EMA50 ({fp(e50)}): {'দ্রুত গড় ওপরে, ঊর্ধ্বমুখী ✅' if e20 > e50 else 'দ্রুত গড় নিচে, নিম্নমুখী ❌'}")
    if a >= 28:
        reasons.append(f"ADX {a:.0f}: ট্রেন্ড খুব শক্তিশালী")
    elif a >= 20:
        reasons.append(f"ADX {a:.0f}: ট্রেন্ড আছে, মাঝারি শক্তি")
    else:
        reasons.append(f"ADX {a:.0f}: ট্রেন্ড দুর্বল, দাম এদিক-ওদিক ঘুরছে")
    if hh is not None and hl is not None:
        if hh and hl:
            reasons.append("সর্বশেষ দুটো উঁচু (High) ও নিচু (Low) দুটোই আগেরটার চেয়ে বড় ✅ (Higher High + Higher Low)")
        elif lh and ll:
            reasons.append("সর্বশেষ High ও Low দুটোই আগেরটার চেয়ে ছোট ❌ (Lower High + Lower Low)")
        else:
            reasons.append("High/Low মিশ্র: পরিষ্কার ধারা নেই")

    up = price > e20 > e50
    down = price < e20 < e50
    tangled = abs(e20 - e50) / price < 0.004
    if up and a >= 20 and (hh or hl):
        key, nm, bn = ("uptrend", "Uptrend", "শক্তিশালী আপট্রেন্ড" if a >= 28 else "আপট্রেন্ড")
        meaning = "বাজার ওপরের দিকে। ট্রেন্ডের দিকে ট্রেড নেওয়া তুলনামূলক সহজ; পুলব্যাকে (ছোট নামায়) ঢোকা ভালো, উঁচুতে তাড়াহুড়ো নয়।"
    elif down and a >= 20 and (lh or ll):
        key, nm, bn = ("downtrend", "Downtrend", "শক্তিশালী ডাউনট্রেন্ড" if a >= 28 else "ডাউনট্রেন্ড")
        meaning = "বাজার নিচের দিকে। স্পট মার্কেটে কেনা ঝুঁকিপূর্ণ (Falling Knife)। ঘুরে দাঁড়ানোর প্রমাণের জন্য অপেক্ষা ভালো।"
    elif up:
        key, nm, bn = ("uptrend_weak", "Weak Uptrend", "দুর্বল আপট্রেন্ড")
        meaning = "ওপরের দিকে ঝোঁক আছে কিন্তু জোর কম। ভলিউম আর ADX বাড়ছে কি না দেখো।"
    elif down:
        key, nm, bn = ("downtrend_weak", "Weak Downtrend", "দুর্বল ডাউনট্রেন্ড")
        meaning = "নিচের দিকে ঝোঁক, তবে জোর কম। এখনই কেনার সংকেত নয়।"
    elif tangled or a < 20:
        key, nm, bn = ("sideways", "Sideways / Range", "সাইডওয়েজ (রেঞ্জ)")
        meaning = "দাম একটা সীমার মধ্যে ঘুরছে। রেঞ্জের মাঝখানে ট্রেড ঝুঁকিপূর্ণ; ব্রেকআউটের অপেক্ষা বা রেঞ্জের নিচে সাপোর্টে কেনা।"
    elif price > e20 and e20 <= e50:
        key, nm, bn = ("reversal_up", "Possible Reversal Up", "নিচ থেকে ঘুরার চেষ্টা")
        meaning = "দাম EMA20 ছাড়িয়েছে কিন্তু বড় ট্রেন্ড (EMA50) এখনো নিচে। ঘুরে দাঁড়ানো কনফার্ম হতে সময় লাগে।"
    elif price < e20 and e20 >= e50:
        key, nm, bn = ("pullback", "Pullback", "আপট্রেন্ডে কারেকশন")
        meaning = "বড় ট্রেন্ড ওপরে, কিন্তু দাম সাময়িক নামছে। EMA50 বা সাপোর্টে থামে কি না দেখো।"
    else:
        key, nm, bn = ("mixed", "Mixed", "মিশ্র / অস্পষ্ট")
        meaning = "সংকেত পরস্পরবিরোধী। এমন সময়ে ট্রেড না নেওয়াই সাধারণত ভালো।"
    return {"key": key, "name": nm, "name_bn": bn, "reasons": reasons, "meaning": meaning,
            "adx": round(a, 1), "ema20": e20, "ema50": e50}


# ------------------------------------------------------------ candle patterns
PATTERNS = {
    "hammer": ("Hammer", "হ্যামার", "bullish",
               "নিচে বিক্রির চাপ এসেছিল কিন্তু ক্রেতারা দাম ঠেলে ওপরে তুলে এনেছে। ডাউনট্রেন্ডের শেষে ঘুরে দাঁড়ানোর ইঙ্গিত।",
               "পরের ক্যান্ডেল হ্যামারের হাই ছাড়ালে কনফার্ম।"),
    "hanging_man": ("Hanging Man", "হ্যাঙ্গিং ম্যান", "bearish",
                    "আপট্রেন্ডের মাথায় হ্যামারের মতো আকৃতি। বিক্রেতারা শক্তি দেখিয়েছে, ঊর্ধ্বমুখী গতি দুর্বল হতে পারে।",
                    "পরের ক্যান্ডেল এর লো ভাঙলে সতর্কতা কনফার্ম।"),
    "inverted_hammer": ("Inverted Hammer", "ইনভার্টেড হ্যামার", "bullish",
                        "ডাউনট্রেন্ডে ক্রেতারা ওপরে ঠেলার চেষ্টা করেছে। সম্ভাব্য ঘুরে দাঁড়ানো, তবে দুর্বল সংকেত।",
                        "পরের ক্যান্ডেল সবুজ হয়ে এর বডির ওপরে বন্ধ হলে ভালো।"),
    "shooting_star": ("Shooting Star", "শুটিং স্টার", "bearish",
                      "আপট্রেন্ডে দাম ওপরে উঠে আবার ফিরে এসেছে। ওপরে বিক্রির চাপ, উঠার গতি থামতে পারে।",
                      "পরের ক্যান্ডেল এর লো ভাঙলে কনফার্ম।"),
    "bull_engulf": ("Bullish Engulfing", "বুলিশ এনগালফিং", "bullish",
                    "সবুজ ক্যান্ডেল আগের লাল ক্যান্ডেলের পুরো বডি গিলে ফেলেছে। ক্রেতারা নিয়ন্ত্রণ নিচ্ছে।",
                    "ভলিউম বেশি থাকলে সংকেত শক্তিশালী।"),
    "bear_engulf": ("Bearish Engulfing", "বেয়ারিশ এনগালফিং", "bearish",
                    "লাল ক্যান্ডেল আগের সবুজ ক্যান্ডেলের পুরো বডি গিলে ফেলেছে। বিক্রেতারা নিয়ন্ত্রণ নিচ্ছে।",
                    "ভলিউম বেশি থাকলে সংকেত শক্তিশালী।"),
    "morning_star": ("Morning Star", "মর্নিং স্টার", "bullish",
                     "৩ ক্যান্ডেলের প্যাটার্ন: বড় লাল, ছোট দ্বিধার ক্যান্ডেল, তারপর বড় সবুজ। নিচ থেকে ঘুরে দাঁড়ানোর ভালো ইঙ্গিত।",
                     "সাপোর্টের কাছে হলে বেশি নির্ভরযোগ্য।"),
    "evening_star": ("Evening Star", "ইভনিং স্টার", "bearish",
                     "৩ ক্যান্ডেলের প্যাটার্ন: বড় সবুজ, ছোট দ্বিধার ক্যান্ডেল, তারপর বড় লাল। ওপর থেকে নামার ইঙ্গিত।",
                     "রেজিস্ট্যান্সের কাছে হলে বেশি নির্ভরযোগ্য।"),
    "doji": ("Doji", "ডোজি", "neutral",
             "খোলা আর বন্ধ প্রায় একই দামে। ক্রেতা-বিক্রেতার টানাটানি, বাজার দ্বিধায়।",
             "ডোজি একা কিছু বলে না; পরের ক্যান্ডেলের দিক দেখো।"),
    "bull_marubozu": ("Bullish Marubozu", "বুলিশ মারুবোজু", "bullish",
                      "প্রায় ছায়াহীন বড় সবুজ ক্যান্ডেল। পুরো সময় ক্রেতাদের দখল।",
                      "খুব বড় হলে অতি-উত্তেজনা (চেজ) এড়াও।"),
    "bear_marubozu": ("Bearish Marubozu", "বেয়ারিশ মারুবোজু", "bearish",
                      "প্রায় ছায়াহীন বড় লাল ক্যান্ডেল। পুরো সময় বিক্রেতাদের দখল।",
                      "ধারাবাহিক হলে ডাউনট্রেন্ড শক্তিশালী।"),
    "inside_bar": ("Inside Bar", "ইনসাইড বার", "neutral",
                   "ক্যান্ডেল পুরোটাই আগের ক্যান্ডেলের ভেতরে। বাজার থেমে দম নিচ্ছে, ব্রেকআউট আসতে পারে।",
                   "যেদিকে ব্রেক করে সেদিকে লক্ষ্য রাখো।"),
}


def _pre_trend(c: List[Dict[str, float]], i: int, atr14: float) -> str:
    if i < 6:
        return "flat"
    move = c[i - 1]["c"] - c[i - 6]["c"]
    if move > 1.5 * atr14:
        return "up"
    if move < -1.5 * atr14:
        return "down"
    return "flat"


def patterns_at(c: List[Dict[str, float]], i: int, atr14: float) -> List[str]:
    k = c[i]
    o, cl, h, l = k["o"], k["c"], k["h"], k["l"]
    rng = h - l
    if rng <= 0:
        return []
    body = abs(cl - o)
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    pre = _pre_trend(c, i, atr14)
    out: List[str] = []

    if body <= 0.1 * rng:
        out.append("doji")
    elif body >= 0.9 * rng:
        out.append("bull_marubozu" if cl > o else "bear_marubozu")

    if lower >= 0.6 * rng and upper <= 0.15 * rng:
        if pre == "down":
            out.append("hammer")
        elif pre == "up":
            out.append("hanging_man")
    if upper >= 0.6 * rng and lower <= 0.15 * rng:
        if pre == "down":
            out.append("inverted_hammer")
        elif pre == "up":
            out.append("shooting_star")

    if i >= 1:
        p = c[i - 1]
        if p["c"] < p["o"] and cl > o and o <= p["c"] and cl >= p["o"] and body > abs(p["c"] - p["o"]):
            out.append("bull_engulf")
        if p["c"] > p["o"] and cl < o and o >= p["c"] and cl <= p["o"] and body > abs(p["c"] - p["o"]):
            out.append("bear_engulf")
        if h < p["h"] and l > p["l"]:
            out.append("inside_bar")

    if i >= 2:
        a, b = c[i - 2], c[i - 1]
        ra, rb = a["h"] - a["l"], b["h"] - b["l"]
        if ra > 0 and rb > 0:
            if (a["c"] < a["o"] and abs(a["c"] - a["o"]) >= 0.6 * ra and abs(b["c"] - b["o"]) <= 0.3 * rb
                    and cl > o and cl > (a["o"] + a["c"]) / 2 and pre != "up"):
                out.append("morning_star")
            if (a["c"] > a["o"] and abs(a["c"] - a["o"]) >= 0.6 * ra and abs(b["c"] - b["o"]) <= 0.3 * rb
                    and cl < o and cl < (a["o"] + a["c"]) / 2 and pre != "down"):
                out.append("evening_star")
    return out


def pattern_info(key: str) -> Dict[str, str]:
    n, bn, bias, meaning, confirm = PATTERNS[key]
    return {"key": key, "name": n, "name_bn": bn, "bias": bias, "meaning": meaning, "confirm": confirm}


# ------------------------------------------------------------ main builder
def build_analysis(base: str, tf: str, c_tf: List[Dict[str, float]], c4: List[Dict[str, float]],
                   c1d: List[Dict[str, float]], pct_24h: float, btc_7d: float,
                   inr_rate: Optional[float]) -> Optional[Dict[str, Any]]:
    closed = ce.closed_only(c_tf)
    if len(closed) < 60:
        return None

    closes = [x["c"] for x in c_tf]
    e20s, e50s = ema_series(closes, 20), ema_series(closes, 50)
    candles = [{"time": int(k["t"] / 1000), "open": k["o"], "high": k["h"], "low": k["l"], "close": k["c"],
                "volume": k["v"]} for k in c_tf]
    ema20 = [{"time": candles[i]["time"], "value": v} for i, v in enumerate(e20s) if v is not None]
    ema50 = [{"time": candles[i]["time"], "value": v} for i, v in enumerate(e50s) if v is not None]

    trend = classify_trend(closed)

    atr_tf = ce.atr(closed) or (closed[-1]["c"] * 0.01)
    markers = []
    for i in range(max(2, len(closed) - 40), len(closed)):
        for key in patterns_at(closed, i, atr_tf):
            info = PATTERNS[key]
            markers.append({"time": int(closed[i]["t"] / 1000), "key": key, "name": info[0], "text": info[1], "bias": info[2]})
    markers = markers[-10:]
    last_keys = patterns_at(closed, len(closed) - 1, atr_tf)
    prev_keys = patterns_at(closed, len(closed) - 2, atr_tf) if len(closed) > 2 else []
    last_c = closed[-1]
    rng = last_c["h"] - last_c["l"]
    shape = {
        "open": last_c["o"], "high": last_c["h"], "low": last_c["l"], "close": last_c["c"],
        "body_pct": round(abs(last_c["c"] - last_c["o"]) / rng * 100) if rng else 0,
        "upper_pct": round((last_c["h"] - max(last_c["o"], last_c["c"])) / rng * 100) if rng else 0,
        "lower_pct": round((min(last_c["o"], last_c["c"]) - last_c["l"]) / rng * 100) if rng else 0,
        "color": "green" if last_c["c"] >= last_c["o"] else "red",
    }
    candle = {
        "latest": [pattern_info(k) for k in last_keys],
        "previous": [pattern_info(k) for k in prev_keys],
        "shape": shape,
        "disclaimer": "ক্যান্ডেল প্যাটার্ন একা নির্ভরযোগ্য সংকেত নয়। ট্রেন্ড, সাপোর্ট/রেজিস্ট্যান্স আর ভলিউমের সাথে মিলিয়ে দেখো।",
    }

    # ---- plan + score from the bot's own 4h engine (so the screen matches what the bot trades)
    a = ce.analyse(base, c4, c1d, pct_24h, btc_7d)
    plan = checklist = risks = None
    levels = {"resistance": [], "support": []}
    c4c = ce.closed_only(c4)
    if c4c:
        res, sup = sr_levels(c4c, c4c[-1]["c"])
        levels = {"resistance": res, "support": sup}

    if a:
        price = a["usdt"]
        sl = price * (1 - a["sl_dist"])
        tp = price * (1 + a["tp_dist"])
        eq = CAPITAL
        size = min(eq * RISK_PCT / 100 / a["sl_dist"], eq * MAX_POS_PCT / 100)
        gross_gain, gross_loss = size * a["tp_dist"], size * a["sl_dist"]
        cost = size * ce.CRYPTO_COST
        would_take = a["score"] >= MIN_SCORE and "(Wait)" not in a["signal"]
        plan = {
            "entry": price, "sl": sl, "tp": tp,
            "sl_pct": round(a["sl_dist"] * 100, 2), "tp_pct": round(a["tp_dist"] * 100, 2), "rr": 2.0,
            "atr": a["atr"], "atr_pct": round(a["atr"] / price * 100, 2),
            "hold_label": a["label"],
            "size_inr": round(size), "max_loss_inr": round(gross_loss + cost), "max_gain_inr": round(gross_gain - cost),
            "cost_inr": round(cost),
            "score": a["score"], "signal": a["signal"], "bot_would_trade": would_take,
            "basis": "৪ ঘণ্টার চার্ট",
            "sl_why": f"এন্ট্রি থেকে ১.৫×ATR নিচে (ATR = দৈনিক/৪ঘণ্টার গড় ওঠানামা {fp(a['atr'])}), সর্বনিম্ন ২%, সর্বোচ্চ ৮%",
            "tp_why": "ঝুঁকির দ্বিগুণ লাভ (Risk:Reward = 1:2)",
        }
        checklist = [
            {"label": "দাম EMA20-র ওপরে", "ok": a["above"], "points": 20,
             "why": f"দাম {fp(price)} বনাম EMA20 {fp(a['ema20'])}"},
            {"label": "ট্রেন্ডের শক্তি (ADX ≥ 20)", "ok": a["adx"] >= 20, "points": 15,
             "why": f"ADX = {a['adx']:.0f}"},
            {"label": "ভলিউমে সমর্থন (OBV উঠছে বা RVOL ≥ 1.25)", "ok": bool(a["obv_up"] or a["rvol"] >= 1.25), "points": 15,
             "why": f"RVOL = {a['rvol']:.2f}, OBV {'উঠছে' if a['obv_up'] else 'ওঠেনি'}"},
            {"label": "BTC-র চেয়ে শক্তিশালী (৭ দিনে +১.৫%)", "ok": a["rs_btc"] > 1.5, "points": 10,
             "why": f"BTC-র তুলনায় {a['rs_btc']:+.1f}%"},
            {"label": "৭ ও ৩০ দিন দুটোতেই লাভে", "ok": a["p7"] > 0 and a["p30"] > 0, "points": 10,
             "why": f"৭ দিন {a['p7']:+.1f}%, ৩০ দিন {a['p30']:+.1f}%"},
        ]
        risks = []
        if a["knife"]:
            risks.append("Falling Knife: দাম EMA20-র নিচে আর জোরে পড়ছে। ঘুরে না দাঁড়ানো পর্যন্ত অপেক্ষা।")
        if a["rsi"] > ce.RSI_MAX:
            risks.append(f"অতি গরম: RSI {a['rsi']:.0f}। এই উচ্চতায় কেনা মানে 'চেজ' করা, বট এড়ায়।")
        elif a["rsi"] < 30:
            risks.append(f"RSI {a['rsi']:.0f}: অতিরিক্ত বিক্রি হয়েছে। বাউন্স আসতে পারে, কিন্তু ছুরি ধরা ঝুঁকিপূর্ণ।")
        if a["rvol"] < 0.8:
            risks.append(f"ভলিউম কম (RVOL {a['rvol']:.2f})। ব্রেকআউট ফাঁকা হতে পারে।")
        if a["rs_btc"] < 0:
            risks.append(f"BTC-র চেয়ে দুর্বল ({a['rs_btc']:+.1f}%)।")
        nearest = [r for r in levels["resistance"] if r > price]
        if nearest and nearest[0] < tp:
            risks.append(f"টার্গেট ({fp(tp)}) পৌঁছানোর আগে রেজিস্ট্যান্স ({fp(nearest[0])}) আছে। দাম সেখানে আটকাতে পারে।")
        if cost > 0:
            risks.append(f"ফি ও ১% TDS ধরে প্রায় {round(ce.CRYPTO_COST * 100, 1)}% খরচ: লাভের হিসাবে এটা বাদ গেছে।")

    return {
        "symbol": base, "tf": tf, "price": closed[-1]["c"], "inr_rate": inr_rate,
        "candles": candles, "ema20": ema20, "ema50": ema50,
        "trend": trend, "candle": candle, "markers": markers, "levels": levels,
        "plan": plan, "checklist": checklist, "risks": risks,
        "score_base": 35,
    }
