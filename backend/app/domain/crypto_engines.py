"""
TRADEIQ Crypto v1.0 — 4-Engine Crypto Quantitative Suite + Daily Boom Radar.
- Engine 0: BTC Regime & Funding Crowding Gate (50/200 EMA + ATR + Aggregate Funding)
- Engine 1: Intraday Session Breakout (15m/1h Range + VWAP + Volume Surge + L2 Depth) -> 2-12h
- Engine 2: Multi-Day Donchian Trend (4h/1d 20-bar Breakout + ATR Vol Sizing) -> 3-14d
- Engine 3: Funding Flush & RSI(2) Pullback Reversion (above 200-EMA) -> 1-4d
- Boom Radar: Dedicated Small/Mid-Cap Explosive Mover Scanner (Strict 1x Micro-Size)
100% deterministic Decimal math.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..providers.crypto_provider import CryptoCandle, CryptoTicker


@dataclass(frozen=True)
class CryptoRegime:
    regime: str  # "BULL_TRENDING" | "BEAR_TRENDING" | "RANGE_BOUND" | "HIGH_RISK_CROWDED"
    allow_longs: bool
    allow_shorts: bool
    btc_price: Decimal
    btc_funding_pct: Decimal
    summary: str


@dataclass(frozen=True)
class CryptoSignal:
    symbol: str
    engine_code: str  # "ENGINE_1_BREAKOUT" | "ENGINE_2_DONCHIAN" | "ENGINE_3_FUNDING_RSI2" | "BOOM_RADAR" | "CATALYST_MOMENTUM"
    horizon_bucket: str  # "intraday" | "reversion" | "swing" | "boom"
    hold_horizon: str
    direction: str  # "LONG" | "SHORT"
    status: str  # "CONFIRMED" | "WATCHLIST" | "REJECTED"
    score: int  # 0..100
    entry_price: Decimal
    stop_loss: Decimal
    target_price: Decimal
    expected_net_rr: Decimal
    recommended_leverage: Decimal
    passed_rules: List[str]
    failed_rules: List[str]
    rationale: str


def _ema(values: List[Decimal], period: int) -> Optional[Decimal]:
    if len(values) < period or period <= 0:
        return None
    k = Decimal("2") / Decimal(str(period + 1))
    val = sum(values[:period], Decimal("0")) / Decimal(str(period))
    for v in values[period:]:
        val = v * k + val * (Decimal("1") - k)
    return val


def _atr(candles: List[CryptoCandle], period: int = 14) -> Decimal:
    if len(candles) < period + 1:
        return (candles[-1].high - candles[-1].low) if candles else Decimal("0")
    trs: List[Decimal] = []
    for i in range(-period, 0):
        c = candles[i]
        prev_c = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_c), abs(c.low - prev_c))
        trs.append(tr)
    return sum(trs, Decimal("0")) / Decimal(str(len(trs)))


def _rsi(closes: List[Decimal], period: int = 2) -> Decimal:
    if len(closes) < period + 1:
        return Decimal("50")
    gains = Decimal("0")
    losses = Decimal("0")
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        elif diff < 0:
            losses += abs(diff)
    if losses == 0:
        return Decimal("100") if gains > 0 else Decimal("50")
    rs = (gains / Decimal(str(period))) / (losses / Decimal(str(period)))
    return (Decimal("100") - (Decimal("100") / (Decimal("1") + rs))).quantize(Decimal("0.01"))


def _vwap(candles: List[CryptoCandle]) -> Decimal:
    if not candles:
        return Decimal("0")
    num = Decimal("0")
    den = Decimal("0")
    for c in candles:
        tp = (c.high + c.low + c.close) / Decimal("3")
        v = max(Decimal("1"), c.volume)
        num += tp * v
        den += v
    return num / den if den > 0 else candles[-1].close


def _quant(px: Decimal) -> Decimal:
    if px < Decimal("0.01"):
        return px.quantize(Decimal("0.00000001"))
    if px < Decimal("10"):
        return px.quantize(Decimal("0.0001"))
    return px.quantize(Decimal("0.01"))


def evaluate_btc_regime(btc_4h: List[CryptoCandle], btc_ticker: Optional[CryptoTicker]) -> CryptoRegime:
    if not btc_4h or len(btc_4h) < 50:
        px = btc_ticker.ltp if btc_ticker else Decimal("0")
        return CryptoRegime("RANGE_BOUND", True, True, px, Decimal("0.01"), "Warming up BTC 4h history — normal balanced regime.")

    closes = [c.close for c in btc_4h]
    ltp = closes[-1]
    ema50 = _ema(closes, 50) or ltp
    ema200 = _ema(closes, min(len(closes), 200)) or ema50
    funding_pct = ((btc_ticker.funding_rate if btc_ticker else Decimal("0.0001")) * Decimal("100")).quantize(Decimal("0.001"))

    if funding_pct >= Decimal("0.050"):
        return CryptoRegime("HIGH_RISK_CROWDED", False, True, ltp, funding_pct, f"BTC 8h Funding crowded ({funding_pct}%) — blocking blind Longs to avoid long-squeeze.")
    if ltp > ema50 and ema50 >= ema200 * Decimal("0.98"):
        return CryptoRegime("BULL_TRENDING", True, True, ltp, funding_pct, f"BTC above 50/200 EMA (${ltp:.2f}) with healthy funding ({funding_pct}%) — Trend Longs & Boom Radar active.")
    if ltp < ema50 and ltp < ema200:
        return CryptoRegime("BEAR_TRENDING", False, True, ltp, funding_pct, f"BTC below 50 & 200 EMA (${ltp:.2f}) — favoring Shorts & defensive sizing.")
    return CryptoRegime("RANGE_BOUND", True, True, ltp, funding_pct, f"BTC consolidating near 50-EMA (${ltp:.2f}) — favoring Reversion & selective breakouts.")


def evaluate_engine_1_breakout(
    symbol: str,
    c15: List[CryptoCandle],
    depth: Dict[str, Any],
    c1h: Optional[List[CryptoCandle]] = None,
    footprint: Optional[Dict[str, Any]] = None,
    change_24h_pct: Decimal = Decimal("0"),
) -> CryptoSignal:
    passed: List[str] = []
    failed: List[str] = []
    if len(c15) < 20:
        return CryptoSignal(symbol, "ENGINE_1_BREAKOUT", "intraday", "Intraday • 2–12 hrs (Session Breakout)", "LONG", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("2"), [], ["Insufficient 15m candles"], "Not enough 15m candles.")

    # v2.1 Closed-Candle Rule: evaluate c15[-2] (fully closed), never live forming c15[-1]
    last = c15[-2]
    prior = c15[-18:-2]
    range_hi = max(c.high for c in prior)
    range_lo = min(c.low for c in prior)
    vwap = _vwap(c15[-24:])
    avg_vol = sum((c.volume for c in prior), Decimal("0")) / Decimal(str(len(prior)))
    rel_vol = (last.volume / max(Decimal("1"), avg_vol)).quantize(Decimal("0.01"))
    atr = _atr(c15, 14)
    buy_pct = float(depth.get("buy_pct", 50.0))

    direction = "LONG" if last.close >= vwap else "SHORT"
    score = 45

    if direction == "LONG":
        if last.close >= range_hi * Decimal("0.998"):
            passed.append(f"4h Range Breakout (>${_quant(range_hi)})")
            score += 22
        else:
            failed.append("Inside 4h Session Range")
        if last.close > vwap:
            passed.append("Above Session VWAP")
            score += 10
        else:
            failed.append("Below VWAP")
        if buy_pct >= 56.0:
            passed.append(f"L2 Depth {buy_pct:.0f}% Buy")
            score += 12
        else:
            failed.append(f"L2 Depth Neutral ({buy_pct:.0f}%)")
    else:
        if last.close <= range_lo * Decimal("1.002"):
            passed.append(f"4h Range Breakdown (<${_quant(range_lo)})")
            score += 22
        else:
            failed.append("Inside 4h Session Range")
        if last.close < vwap:
            passed.append("Below Session VWAP")
            score += 10
        else:
            failed.append("Above VWAP")
        if buy_pct <= 44.0:
            passed.append(f"L2 Depth {100 - buy_pct:.0f}% Sell")
            score += 12
        else:
            failed.append("L2 Sell Imbalance Weak")

    if rel_vol >= Decimal("1.50"):
        passed.append(f"Vol Surge {rel_vol}x")
        score += 15
    else:
        failed.append(f"Vol {rel_vol}x (<1.5x)")

    # 1h Multi-Timeframe Trend Alignment (eliminates 15m noise whipsaws)
    if c1h and len(c1h) >= 22:
        ema20_1h = _ema([c.close for c in c1h], 20) or last.close
        if (direction == "LONG" and last.close >= ema20_1h) or (direction == "SHORT" and last.close <= ema20_1h):
            passed.append("1h 20-EMA Trend Aligned")
            score += 8
        else:
            failed.append("Counter to 1h 20-EMA Trend")
            score -= 10

    # OKX Tick Footprint / CVD Alignment
    dpct = float((footprint or {}).get("delta_pct", 0.0))
    if (direction == "LONG" and dpct >= 5.0) or (direction == "SHORT" and dpct <= -5.0):
        passed.append(f"Footprint CVD {dpct:+.1f}% Confirmed")
        score += 8
    elif (direction == "LONG" and dpct <= -15.0) or (direction == "SHORT" and dpct >= 15.0):
        failed.append(f"Footprint Absorption Against ({dpct:+.1f}%)")
        score -= 15

    # Exhaustion / Bottom-Short & Top-Long Chase Guard
    if direction == "SHORT" and change_24h_pct <= Decimal("-3.5"):
        failed.append(f"Exhausted Dump ({change_24h_pct}% 24h) — No Bottom Short")
        score -= 25
    elif direction == "LONG" and change_24h_pct >= Decimal("8.0"):
        failed.append(f"Overextended Pump (+{change_24h_pct}% 24h) — No Top Chase")
        score -= 20

    entry = _quant(last.close)
    # Crypto-grade stop-loss floor (1.6% min / 1.85x ATR) so normal 15m noise wicks don't hunt SL
    risk = max(atr * Decimal("1.85"), entry * Decimal("0.016"))
    sl = _quant(entry - risk if direction == "LONG" else entry + risk)
    target = _quant(entry + risk * Decimal("2.4") if direction == "LONG" else entry - risk * Decimal("2.4"))
    net_rr = Decimal("2.25")
    status = "CONFIRMED" if (score >= 84 and len(failed) <= 1) else ("WATCHLIST" if score >= 68 else "REJECTED")
    return CryptoSignal(
        symbol=symbol, engine_code="ENGINE_1_BREAKOUT", horizon_bucket="intraday",
        hold_horizon="Intraday • 2–12 hrs (Session Breakout)", direction=direction,
        status=status, score=min(98, score), entry_price=entry, stop_loss=sl, target_price=target,
        expected_net_rr=net_rr, recommended_leverage=Decimal("2"),
        passed_rules=passed, failed_rules=failed,
        rationale=f"Session {direction} on {symbol}: Vol {rel_vol}x, L2 Buy {buy_pct:.0f}%, VWAP ${_quant(vwap)}.",
    )


def evaluate_engine_2_donchian(symbol: str, c4h: List[CryptoCandle]) -> CryptoSignal:
    passed: List[str] = []
    failed: List[str] = []
    if len(c4h) < 55:
        return CryptoSignal(symbol, "ENGINE_2_DONCHIAN", "swing", "Trend Swing • 3–14 Days (Donchian)", "LONG", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1.5"), [], ["Insufficient 4h candles"], "Need 55+ 4h candles.")

    closes = [c.close for c in c4h]
    last = c4h[-1]
    ema50 = _ema(closes, 50) or last.close
    d20_hi = max(c.high for c in c4h[-21:-1])
    atr = _atr(c4h, 14)
    avg_vol = sum((c.volume for c in c4h[-21:-1]), Decimal("0")) / Decimal("20")
    rel_vol = (last.volume / max(Decimal("1"), avg_vol)).quantize(Decimal("0.01"))

    score = 44
    if last.close > ema50:
        passed.append("Above 4h 50-EMA")
        score += 16
    else:
        failed.append("Below 4h 50-EMA")

    if last.close >= d20_hi * Decimal("0.996"):
        passed.append(f"20-Bar Donchian Breakout (>${_quant(d20_hi)})")
        score += 24
    else:
        failed.append("Below 20-Bar Donchian High")

    if rel_vol >= Decimal("1.30"):
        passed.append(f"Breakout Vol {rel_vol}x")
        score += 14
    else:
        failed.append(f"Vol {rel_vol}x (<1.3x)")

    entry = _quant(last.close)
    risk = max(atr * Decimal("2.2"), entry * Decimal("0.025"))
    sl = _quant(entry - risk)
    target = _quant(entry + risk * Decimal("2.8"))
    status = "CONFIRMED" if (score >= 82 and len(failed) <= 1) else ("WATCHLIST" if score >= 68 else "REJECTED")
    return CryptoSignal(
        symbol=symbol, engine_code="ENGINE_2_DONCHIAN", horizon_bucket="swing",
        hold_horizon="Trend Swing • 3–14 Days (Donchian)", direction="LONG",
        status=status, score=min(98, score), entry_price=entry, stop_loss=sl, target_price=target,
        expected_net_rr=Decimal("2.65"), recommended_leverage=Decimal("1.5"),
        passed_rules=passed, failed_rules=failed,
        rationale=f"4h Donchian Trend LONG on {symbol} above 50-EMA, Vol {rel_vol}x.",
    )


def evaluate_engine_3_funding_rsi2(symbol: str, c4h: List[CryptoCandle], ticker: Optional[CryptoTicker]) -> CryptoSignal:
    passed: List[str] = []
    failed: List[str] = []
    if len(c4h) < 55:
        return CryptoSignal(symbol, "ENGINE_3_FUNDING_RSI2", "reversion", "Short Reversion • 1–4 Days", "LONG", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("2"), [], ["Insufficient 4h candles"], "Need 55+ 4h candles.")

    closes = [c.close for c in c4h]
    last = c4h[-1]
    ema_trend = _ema(closes, min(len(closes), 100)) or last.close
    rsi2 = _rsi(closes, 2)
    fr_pct = ((ticker.funding_rate if ticker else Decimal("0.0001")) * Decimal("100")).quantize(Decimal("0.001"))
    atr = _atr(c4h, 14)

    score = 45
    if last.close >= ema_trend * Decimal("0.98"):
        passed.append("Primary Uptrend Intact")
        score += 15
    else:
        failed.append("Below Trend EMA")

    if rsi2 <= Decimal("15"):
        passed.append(f"RSI(2) Oversold ({rsi2})")
        score += 24
    else:
        failed.append(f"RSI(2) Not Oversold ({rsi2})")

    if fr_pct <= Decimal("0.015"):
        passed.append(f"Funding Reset ({fr_pct}%)")
        score += 14
    else:
        failed.append(f"Funding Elevated ({fr_pct}%)")

    entry = _quant(last.close)
    risk = max(atr * Decimal("1.8"), entry * Decimal("0.018"))
    sl = _quant(entry - risk)
    target = _quant(entry + risk * Decimal("2.2"))
    status = "CONFIRMED" if (score >= 82 and len(failed) == 0) else ("WATCHLIST" if score >= 68 else "REJECTED")
    return CryptoSignal(
        symbol=symbol, engine_code="ENGINE_3_FUNDING_RSI2", horizon_bucket="reversion",
        hold_horizon="Short Reversion • 1–4 Days", direction="LONG",
        status=status, score=min(96, score), entry_price=entry, stop_loss=sl, target_price=target,
        expected_net_rr=Decimal("2.10"), recommended_leverage=Decimal("2"),
        passed_rules=passed, failed_rules=failed,
        rationale=f"Funding + RSI(2)={rsi2} Pullback Reversion on {symbol} (Funding {fr_pct}%).",
    )


def evaluate_boom_radar(
    symbol: str,
    c15: List[CryptoCandle],
    ticker: Optional[CryptoTicker],
    depth: Dict[str, Any],
    footprint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Dedicated Daily Boom Radar for volatile small/mid-cap USDT coins.
    Strictly enforces 1x micro-sizing to protect core capital.
    """
    ltp = ticker.ltp if ticker else (c15[-1].close if c15 else Decimal("0"))
    chg24 = ticker.change_24h_pct if ticker else Decimal("0")
    vol_usdt = ticker.volume_24h_usdt if ticker else Decimal("0")
    buy_pct = float(depth.get("buy_pct", 50.0))

    rel_vol = Decimal("1.0")
    breakout = False
    if len(c15) >= 16:
        prior = c15[-16:-1]
        avg_v = sum((c.volume for c in prior), Decimal("0")) / Decimal(str(len(prior)))
        rel_vol = (c15[-1].volume / max(Decimal("1"), avg_v)).quantize(Decimal("0.01"))
        breakout = c15[-1].close >= max(c.high for c in prior) * Decimal("0.996")

    score = 40
    checks: List[str] = []
    if chg24 >= Decimal("5.0"):
        score += 20
        checks.append(f"24h Momentum +{chg24}%")
    if rel_vol >= Decimal("2.0"):
        score += 20
        checks.append(f"15m Vol Explosion {rel_vol}x")
    if breakout:
        score += 12
        checks.append("Tight Range Breakout")
    if buy_pct >= 58.0:
        score += 8
        checks.append(f"L2 Buy {buy_pct:.0f}%")

    # Anti-Dump Footprint Protection: block boom trigger if big sellers are already dumping!
    dpct = float((footprint or {}).get("delta_pct", 0.0))
    if dpct >= 8.0:
        score += 10
        checks.append(f"Footprint Buy Flow ({dpct:+.1f}%)")
    elif dpct <= -10.0:
        score -= 22
        checks.append(f"⚠️ Seller Dump Detected ({dpct:+.1f}%)")
    stage = "BOOM TRIGGERED" if (score >= 82 and dpct > -5.0) else ("EARLY EXPANSION" if score >= 68 else "WATCHING BASE")
    entry = _quant(ltp)
    sl = _quant(entry * Decimal("0.96"))
    target = _quant(entry * Decimal("1.12"))
    return {
        "symbol": symbol,
        "ltp": str(entry),
        "change_24h_pct": str(chg24),
        "volume_24h_usdt": str(vol_usdt),
        "rel_vol_15m": str(rel_vol),
        "buy_pct": buy_pct,
        "stage": stage,
        "score": min(98, score),
        "recommended_leverage": "1x",
        "sizing_guard": "Strict 1x Micro-Size (50% Allocation)",
        "entry_price": str(entry),
        "stop_loss": str(sl),
        "target_price": str(target),
        "checks": checks or ["Monitoring accumulation base"],
    }


def evaluate_engine_4_shadow_sweep(
    symbol: str,
    c15: List[CryptoCandle],
    footprint: Optional[Dict[str, Any]] = None,
    crowd: Optional[Dict[str, Any]] = None,
) -> CryptoSignal:
    """
    Engine 4: Institutional Shadow Sweep + Footprint CVD Absorption Detector.
    Catches retail stop-loss hunts ('Through & Back In' liquidity sweeps) confirmed by OKX tick delta:
    - Sell-Side Sweep (Bullish Reversal): wick pierces below 20-bar Swing Low AND closes back above it
      with >= 40% lower wick + positive Footprint Buy Absorption -> LONG.
    - Buy-Side Sweep (Bearish Reversal): wick pierces above 20-bar Swing High AND closes back below it
      with >= 40% upper wick + negative Footprint Sell Absorption -> SHORT.
    """
    if len(c15) < 25:
        return CryptoSignal(symbol, "ENGINE_4_SHADOW_SWEEP", "intraday", "Shadow Sweep • 2–12 hrs", "LONG", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1.5"), [], ["Insufficient 15m candles"], "Need 25+ 15m candles.")

    # v2.1 Closed-Candle Rule: prevent mid-candle fake sweep wicks
    last = c15[-2]
    prior = c15[-23:-2]
    swing_lo = min(c.low for c in prior)
    swing_hi = max(c.high for c in prior)
    rng = max(Decimal("0.00000001"), last.high - last.low)
    lower_wick_pct = (min(last.open, last.close) - last.low) / rng
    upper_wick_pct = (last.high - max(last.open, last.close)) / rng
    atr = _atr(c15, 14)
    dpct = float((footprint or {}).get("delta_pct", 0.0))
    crowd_state = str((crowd or {}).get("crowd_state", "BALANCED"))

    passed: List[str] = []
    failed: List[str] = []
    direction = "LONG"
    score = 40

    sell_side_sweep = last.low < swing_lo * Decimal("0.999") and last.close > swing_lo and lower_wick_pct >= Decimal("0.40")
    buy_side_sweep = last.high > swing_hi * Decimal("1.001") and last.close < swing_hi and upper_wick_pct >= Decimal("0.40")

    if sell_side_sweep:
        direction = "LONG"
        score += 32
        passed.append(f"Sell-Side Stop-Hunt Sweep (<${_quant(swing_lo)} & Closed Inside)")
        if dpct >= 6.0:
            score += 18
            passed.append(f"Footprint Buy Absorption ({dpct:+.1f}% Delta)")
        else:
            failed.append(f"Weak Buy Delta ({dpct:+.1f}%)")
        if crowd_state == "CROWDED_SHORTS":
            score += 8
            passed.append("Crowded Retail Shorts Trapped")
    elif buy_side_sweep:
        direction = "SHORT"
        score += 32
        passed.append(f"Buy-Side Stop-Hunt Sweep (>${_quant(swing_hi)} & Closed Inside)")
        if dpct <= -6.0:
            score += 18
            passed.append(f"Footprint Sell Absorption ({dpct:+.1f}% Delta)")
        else:
            failed.append(f"Weak Sell Delta ({dpct:+.1f}%)")
        if crowd_state == "CROWDED_LONGS":
            score += 8
            passed.append("Crowded Retail Longs Trapped")
    else:
        failed.append("No Active Liquidity Sweep Wick")

    entry = _quant(last.close)
    risk = max(atr * Decimal("1.8"), entry * Decimal("0.016"))
    sl = _quant(min(last.low, entry - risk) if direction == "LONG" else max(last.high, entry + risk))
    target = _quant(entry + risk * Decimal("2.5") if direction == "LONG" else entry - risk * Decimal("2.5"))
    status = "CONFIRMED" if (score >= 86 and len(failed) == 0) else ("WATCHLIST" if score >= 70 else "REJECTED")
    return CryptoSignal(
        symbol=symbol, engine_code="ENGINE_4_SHADOW_SWEEP", horizon_bucket="intraday",
        hold_horizon="Shadow Sweep Reversal • 2–12 hrs", direction=direction,
        status=status, score=min(98, score), entry_price=entry, stop_loss=sl, target_price=target,
        expected_net_rr=Decimal("2.40"), recommended_leverage=Decimal("1.5"),
        passed_rules=passed, failed_rules=failed,
        rationale=f"Institutional Stop-Hunt {direction} Sweep on {symbol}: Footprint Delta {dpct:+.1f}%, Crowd {crowd_state}.",
    )


def evaluate_engine_5_crash_short(
    symbol: str,
    c15: List[CryptoCandle],
    c1h: List[CryptoCandle],
    depth: Dict[str, Any],
    footprint: Optional[Dict[str, Any]] = None,
    change_24h_pct: Decimal = Decimal("0"),
    btc_chg_24h: Decimal = Decimal("0"),
    macro_reason: str = "",
) -> CryptoSignal:
    """
    Engine 5: Institutional Bear-Crash Momentum & Dead-Cat Bounce Rejection SHORT.
    Activates when BTC crashes <= -4.0% (or bearish macro catalyst fires) to profit from downside momentum:
    - 1h trend strictly bearish (close < 1h EMA-20)
    - 15m Dead-Cat Bounce Rejection at EMA-20 OR fresh 6-bar breakdown (avoids blind bottom-tick shorting)
    - Confirmed by OKX Live Footprint Sell Flow (delta <= -8%) & L2 Sell Pressure (buy_pct <= 48%)
    """
    if len(c15) < 25 or len(c1h) < 20:
        return CryptoSignal(symbol, "ENGINE_5_CRASH_SHORT", "intraday", "Crash Momentum • 2–12 hrs", "SHORT", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1.5"), [], ["Insufficient candles"], "Need 15m+1h candles.")

    if btc_chg_24h > Decimal("-4.0") and not macro_reason:
        return CryptoSignal(symbol, "ENGINE_5_CRASH_SHORT", "intraday", "Crash Momentum • 2–12 hrs", "SHORT", "REJECTED", 20, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1.5"), [], ["No active BTC crash regime"], "BTC not in crash regime.")

    # v2.1 Closed-Candle Rule: evaluate fully closed 15m/1h bars
    last15 = c15[-2]
    closes15 = [c.close for c in c15[:-1]]
    closes1h = [c.close for c in c1h[:-1]]
    ema20_15 = _ema(closes15, 20) or last15.close
    ema20_1h = _ema(closes1h, 20) or c1h[-1].close
    atr15 = _atr(c15, 14)

    passed: List[str] = []
    failed: List[str] = []
    score = 35

    if c1h[-1].close < ema20_1h:
        passed.append("1h Downtrend Confirmed")
        score += 18
    else:
        failed.append("1h Above EMA-20")

    # Dead-cat bounce rejection off 15m EMA-20 OR fresh 6-bar breakdown
    prior6_low = min(c.low for c in c15[-8:-2])
    bounce_reject = (last15.high >= ema20_15 * Decimal("0.994")) and (last15.close < last15.open) and (last15.close < ema20_15 * Decimal("1.002"))
    fresh_breakdown = last15.close < prior6_low and last15.close < last15.open
    if bounce_reject:
        passed.append("15m Dead-Cat Bounce Rejected at EMA-20")
        score += 22
    elif fresh_breakdown:
        passed.append("15m Consolidation Breakdown")
        score += 20
    else:
        failed.append("No Bounce Rejection / Breakdown Yet")

    buy_pct = float((depth or {}).get("buy_pct", 50.0))
    dpct = float((footprint or {}).get("delta_pct", 0.0))
    if dpct <= -8.0 and buy_pct <= 49.0:
        passed.append(f"Footprint Sell Flow ({dpct:+.1f}%) • L2 {buy_pct:.0f}% Buy")
        score += 20
    else:
        failed.append(f"Weak Sell Flow (CVD {dpct:+.1f}%, L2 {buy_pct:.0f}%)")

    if btc_chg_24h <= Decimal("-4.0"):
        passed.append(f"BTC Crash Momentum ({btc_chg_24h}%)")
        score += 8
    if macro_reason:
        passed.append("Bearish Macro Catalyst Active")
        score += 6

    entry = _quant(last15.close)
    risk = max(atr15 * Decimal("1.65"), entry * Decimal("0.017"))
    risk = min(risk, entry * Decimal("0.028"))  # Hard 2.8% SL cap so crash shorts never blow up
    sl = _quant(entry + risk)
    target = _quant(entry - risk * Decimal("2.6"))
    status = "CONFIRMED" if (score >= 82 and len(failed) == 0) else ("WATCHLIST" if score >= 68 else "REJECTED")
    why_macro = f" • Cause: {macro_reason[:85]}" if macro_reason else f" • BTC {btc_chg_24h}% Crash"
    return CryptoSignal(
        symbol=symbol,
        engine_code="ENGINE_5_CRASH_SHORT",
        horizon_bucket="intraday",
        hold_horizon="Crash Momentum • 2–12 hrs",
        direction="SHORT",
        status=status,
        score=min(97, score),
        entry_price=entry,
        stop_loss=sl,
        target_price=target,
        expected_net_rr=Decimal("2.50"),
        recommended_leverage=Decimal("1.5"),
        passed_rules=passed,
        failed_rules=failed,
        rationale=f"Crash Momentum SHORT on {symbol} (CVD {dpct:+.1f}%){why_macro}",
    )
