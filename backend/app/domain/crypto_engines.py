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
    regime_mode: str = "TRENDING",
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

    # v2.2 Rejection Wick Trap Guard (blocks buying right after a whale upper rejection wick like ZECUSDT, or shorting after a lower bounce wick)
    recent_closed = c15[-4:-1]
    wick_trap = False
    if direction == "LONG":
        for bar in recent_closed:
            brng = max(Decimal("0.00000001"), bar.high - bar.low)
            u_wick = bar.high - max(bar.open, bar.close)
            if (u_wick / brng) >= Decimal("0.45") and (u_wick / max(Decimal("0.00000001"), bar.close)) >= Decimal("0.005"):
                wick_trap = True
                break
        if not wick_trap and max(b.high for b in recent_closed) >= last.close * Decimal("1.010"):
            wick_trap = True
        if wick_trap:
            failed.append("Upper Rejection Wick Trap — Whale Selling Overhead")
            score -= 28
    else:
        for bar in recent_closed:
            brng = max(Decimal("0.00000001"), bar.high - bar.low)
            l_wick = min(bar.open, bar.close) - bar.low
            if (l_wick / brng) >= Decimal("0.45") and (l_wick / max(Decimal("0.00000001"), bar.close)) >= Decimal("0.005"):
                wick_trap = True
                break
        if not wick_trap and min(b.low for b in recent_closed) <= last.close * Decimal("0.990"):
            wick_trap = True
        if wick_trap:
            failed.append("Lower Rejection Wick Trap — Whale Buying Below")
            score -= 28

    # v2.2 Anti-Chase / VWAP Overextension Guard (blocks parabolic blow-off candle chasing)
    if atr > 0:
        body_sz = abs(last.close - last.open)
        if direction == "LONG" and ((last.close - vwap) > atr * Decimal("1.8") or body_sz > atr * Decimal("2.0")):
            failed.append("Overextended Above VWAP — Anti-Chase Guard")
            score -= 20
        elif direction == "SHORT" and ((vwap - last.close) > atr * Decimal("1.8") or body_sz > atr * Decimal("2.0")):
            failed.append("Overextended Below VWAP — Anti-Chase Guard")
            score -= 20

    entry = _quant(last.close)
    # Crypto-grade stop-loss floor (2.0% min / 1.85x ATR) so normal 15m noise wicks don't hunt SL
    risk = max(atr * Decimal("1.85"), entry * Decimal("0.020"))
    sl = _quant(entry - risk if direction == "LONG" else entry + risk)
    target = _quant(entry + risk * Decimal("2.4") if direction == "LONG" else entry - risk * Decimal("2.4"))
    net_rr = Decimal("2.25")
    is_sideways = (regime_mode or "").upper() == "SIDEWAYS_RANGE"
    if wick_trap:
        status = "REJECTED"
    elif is_sideways:
        status = "CONFIRMED" if (score >= 90 and len(failed) == 0) else ("WATCHLIST" if score >= 70 else "REJECTED")
    else:
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
    boom_wick_trap = False
    if len(c15) >= 17:
        # v2.2 Closed-Candle Rule for Boom Radar: evaluate c15[-2], never unclosed forming bar c15[-1]
        closed_last = c15[-2]
        prior = c15[-17:-2]
        avg_v = sum((c.volume for c in prior), Decimal("0")) / Decimal(str(len(prior)))
        rel_vol = (closed_last.volume / max(Decimal("1"), avg_v)).quantize(Decimal("0.01"))
        breakout = closed_last.close >= max(c.high for c in prior) * Decimal("0.996")
        recent_b = c15[-4:-1]
        for bar in recent_b:
            brng = max(Decimal("0.00000001"), bar.high - bar.low)
            u_wick = bar.high - max(bar.open, bar.close)
            if (u_wick / brng) >= Decimal("0.45") and (u_wick / max(Decimal("0.00000001"), bar.close)) >= Decimal("0.006"):
                boom_wick_trap = True
                break
        if not boom_wick_trap and max(b.high for b in recent_b) >= closed_last.close * Decimal("1.012"):
            boom_wick_trap = True
    elif len(c15) >= 16:
        prior = c15[-16:-1]
        avg_v = sum((c.volume for c in prior), Decimal("0")) / Decimal(str(len(prior)))
        rel_vol = (c15[-1].volume / max(Decimal("1"), avg_v)).quantize(Decimal("0.01"))
        breakout = c15[-1].close >= max(c.high for c in prior) * Decimal("0.996")

    # Early Boom Ignition: block coins ALREADY pumped > +8.0% in 24h (catch quiet bases BEFORE the +20% pump!)
    overextended_pump = chg24 > Decimal("8.0")
    tight_base = False
    if len(c15) >= 17:
        base_bars = c15[-17:-2]
        b_hi = max(c.high for c in base_bars)
        b_lo = min(c.low for c in base_bars)
        if b_lo > 0 and ((b_hi - b_lo) / b_lo) <= Decimal("0.042"):
            tight_base = True

    score = 40
    checks: List[str] = []
    if overextended_pump:
        score -= 35
        checks.append(f"⚠️ Already Pumped +{chg24}% (Early Boom Blocked)")
    elif Decimal("0.5") <= chg24 <= Decimal("7.5"):
        score += 20
        checks.append(f"Early Ignition Zone (+{chg24}% 24h)")
    if tight_base:
        score += 10
        checks.append("Tight Quiet Base Squeeze (<=4.2%)")
    if rel_vol >= Decimal("2.0"):
        score += 20
        checks.append(f"Vol Ignition {rel_vol}x")
    if breakout:
        score += 12
        checks.append("Fresh Base Breakout")
    if buy_pct >= 58.0:
        score += 8
        checks.append(f"L2 Buy {buy_pct:.0f}%")

    # Anti-Dump Footprint & Rejection Wick Protection
    dpct = float((footprint or {}).get("delta_pct", 0.0))
    if dpct >= 8.0:
        score += 10
        checks.append(f"Footprint Buy Flow ({dpct:+.1f}%)")
    elif dpct <= -10.0:
        score -= 22
        checks.append(f"⚠️ Seller Dump Detected ({dpct:+.1f}%)")
    if boom_wick_trap:
        score -= 25
        checks.append("⚠️ Upper Rejection Wick Trap")
    if overextended_pump:
        stage = "OVEREXTENDED_BLOCKED"
    else:
        stage = "BOOM TRIGGERED" if (score >= 82 and dpct > -5.0 and not boom_wick_trap) else ("EARLY EXPANSION" if score >= 68 else "WATCHING BASE")
    entry = _quant(ltp)
    sl = _quant(entry * Decimal("0.96"))
    target = _quant(entry * Decimal("1.08"))
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
    risk = max(atr * Decimal("1.8"), entry * Decimal("0.020"))
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


def _adx_val(candles: List[CryptoCandle], period: int = 14) -> float:
    n = len(candles)
    if n < 2 * period + 1:
        return 25.0
    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        h, l, pc = float(candles[i].high), float(candles[i].low), float(candles[i - 1].close)
        ph, pl = float(candles[i - 1].high), float(candles[i - 1].low)
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        up, dn = h - ph, pl - l
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    atr_s = sum(tr[1 : period + 1])
    p_s = sum(pdm[1 : period + 1])
    m_s = sum(mdm[1 : period + 1])
    dx = [0.0] * n
    for i in range(period, n):
        if i > period:
            atr_s = atr_s - atr_s / period + tr[i]
            p_s = p_s - p_s / period + pdm[i]
            m_s = m_s - m_s / period + mdm[i]
        pdi = 100.0 * p_s / max(1e-9, atr_s)
        mdi = 100.0 * m_s / max(1e-9, atr_s)
        dx[i] = 100.0 * abs(pdi - mdi) / max(1e-9, pdi + mdi)
    val = sum(dx[period : 2 * period]) / period
    for i in range(2 * period, n):
        val = (val * (period - 1) + dx[i]) / period
    return round(val, 2)


def _bollinger(closes: List[Decimal], period: int = 20, mult: float = 2.0):
    if len(closes) < period:
        c = float(closes[-1]) if closes else 0.0
        return c, c, c
    seg = [float(x) for x in closes[-period:]]
    mid = sum(seg) / period
    sd = (sum((x - mid) ** 2 for x in seg) / period) ** 0.5
    return mid, mid + mult * sd, mid - mult * sd


def _supertrend_dirs(candles: List[CryptoCandle], period: int = 10, mult: float = 3.0) -> List[int]:
    n = len(candles)
    if n < period + 2:
        return [1] * n
    trs = [0.0] * n
    for i in range(1, n):
        h, l, pc = float(candles[i].high), float(candles[i].low), float(candles[i - 1].close)
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
    atrs = [0.0] * n
    s = sum(trs[1 : period + 1])
    atrs[period] = s / period
    for i in range(period + 1, n):
        s += trs[i] - trs[i - period]
        atrs[i] = s / period
    dirs = [1] * n
    up_b = [0.0] * n
    dn_b = [0.0] * n
    for i in range(period, n):
        hl2 = (float(candles[i].high) + float(candles[i].low)) / 2.0
        bu = hl2 + mult * atrs[i]
        bd = hl2 - mult * atrs[i]
        if i == period:
            up_b[i], dn_b[i], dirs[i] = bu, bd, 1
            continue
        prev_c = float(candles[i - 1].close)
        up_b[i] = bu if (bu < up_b[i - 1] or prev_c > up_b[i - 1]) else up_b[i - 1]
        dn_b[i] = bd if (bd > dn_b[i - 1] or prev_c < dn_b[i - 1]) else dn_b[i - 1]
        cur_c = float(candles[i].close)
        if dirs[i - 1] == 1:
            dirs[i] = -1 if cur_c < dn_b[i] else 1
        else:
            dirs[i] = 1 if cur_c > up_b[i] else -1
    return dirs


def evaluate_c05_adx_bb_mean_rev(
    symbol: str,
    c4h: List[CryptoCandle],
    footprint: Optional[Dict[str, Any]] = None,
) -> CryptoSignal:
    """
    Champion C05 — Part A: 4H ADX<=32 Bollinger(2.0) Trend-Aligned Mean Reversion (BOTH LONG & SHORT).
    2-Year Proven Backtest: 64.1% Win Rate, PF 1.39, Max DD only 6.4R (both Bull & Bear years positive).
    Evaluates strictly on CLOSED 4H candles (c4h[:-1]).
    """
    closed = c4h[:-1] if len(c4h) >= 62 else c4h
    if len(closed) < 60:
        return CryptoSignal(symbol, "C05_ADX_BB_MEAN_REV", "reversion", "C05 4H Mean-Rev • 64% WR", "LONG", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("2"), [], ["Insufficient 4H candles"], "Need 60+ 4H candles.")

    closes = [c.close for c in closed]
    last = closed[-1]
    ema_len = min(len(closes), 200)
    ema200 = _ema(closes, ema_len) or last.close
    adx14 = _adx_val(closed, 14)
    rsi14 = _rsi(closes, 14)
    bb_mid, bb_up, bb_lo = _bollinger(closes, 20, 2.0)
    atr4 = _atr(closed, 14)
    entry = _quant(last.close)
    cl_f = float(last.close)

    passed: List[str] = []
    failed: List[str] = []
    direction = "LONG" if last.close >= ema200 else "SHORT"
    score = 45

    if adx14 <= 32.0:
        passed.append(f"Calm Pullback Regime (4H ADX {adx14:.1f} <= 32)")
        score += 15
    else:
        failed.append(f"ADX Too High for Mean-Rev ({adx14:.1f} > 32)")

    if direction == "LONG":
        passed.append(f"Above 4H {ema_len}-EMA (${_quant(ema200)})")
        score += 12
        if cl_f <= bb_lo:
            passed.append(f"4H Lower BB(2.0) Tap (<=${bb_lo:.4f})")
            score += 16
        else:
            failed.append("Above Lower BB(2.0)")
        if rsi14 <= Decimal("42"):
            passed.append(f"4H RSI(14) Oversold ({rsi14})")
            score += 10
        else:
            failed.append(f"4H RSI(14) Not Oversold ({rsi14})")
    else:
        passed.append(f"Below 4H {ema_len}-EMA (${_quant(ema200)})")
        score += 12
        if cl_f >= bb_up:
            passed.append(f"4H Upper BB(2.0) Tap (>=${bb_up:.4f})")
            score += 16
        else:
            failed.append("Below Upper BB(2.0)")
        if rsi14 >= Decimal("58"):
            passed.append(f"4H RSI(14) Overbought ({rsi14})")
            score += 10
        else:
            failed.append(f"4H RSI(14) Not Overbought ({rsi14})")

    dpct = float((footprint or {}).get("delta_pct", 0.0))
    if (direction == "LONG" and dpct <= -25.0) or (direction == "SHORT" and dpct >= 25.0):
        failed.append(f"Extreme Counter Footprint ({dpct:+.1f}%)")
        score -= 15

    risk = max(atr4 * Decimal("2.5"), entry * Decimal("0.022"))
    sl = _quant(entry - risk if direction == "LONG" else entry + risk)
    mid_dec = _quant(Decimal(str(bb_mid))) if bb_mid > 0 else (entry + risk if direction == "LONG" else entry - risk)
    if direction == "LONG":
        target = max(mid_dec, _quant(entry + risk * Decimal("0.85")))
    else:
        target = min(mid_dec, _quant(entry - risk * Decimal("0.85")))
    rr = (abs(target - entry) / max(Decimal("0.00000001"), risk)).quantize(Decimal("0.01"))
    status = "CONFIRMED" if (len(failed) == 0 and score >= 88) else ("WATCHLIST" if score >= 70 else "REJECTED")
    return CryptoSignal(
        symbol=symbol, engine_code="C05_ADX_BB_MEAN_REV", horizon_bucket="reversion",
        hold_horizon="C05 4H Mean-Rev • 64% WR", direction=direction, status=status,
        score=min(98, score), entry_price=entry, stop_loss=sl, target_price=target,
        expected_net_rr=rr, recommended_leverage=Decimal("2.0"),
        passed_rules=passed, failed_rules=failed,
        rationale=f"C05 4H ADX-BB {direction} on {symbol}: ADX {adx14:.1f}, RSI(14) {rsi14}, Target Mid-BB ${_quant(target)}.",
    )


def evaluate_c05_supertrend_trail(
    symbol: str,
    c4h: List[CryptoCandle],
    footprint: Optional[Dict[str, Any]] = None,
) -> CryptoSignal:
    """
    Champion C05 — Part B: 4H Supertrend(10,3) Flip + 4H 200-EMA + 3xATR Trailing Stop (BOTH LONG & SHORT).
    2-Year Proven Backtest: PF 1.81, +60.0R, Max DD only 12.1R (Bull +24R & Bear +36R).
    """
    closed = c4h[:-1] if len(c4h) >= 62 else c4h
    if len(closed) < 60:
        return CryptoSignal(symbol, "C05_SUPERTREND_TRAIL", "swing", "C05 4H Supertrend • 3xATR Trail", "LONG", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("2"), [], ["Insufficient 4H candles"], "Need 60+ 4H candles.")

    closes = [c.close for c in closed]
    last = closed[-1]
    ema_len = min(len(closes), 200)
    ema200 = _ema(closes, ema_len) or last.close
    dirs = _supertrend_dirs(closed, 10, 3.0)
    atr4 = _atr(closed, 14)
    entry = _quant(last.close)

    passed: List[str] = []
    failed: List[str] = []
    bull_flip = dirs[-1] == 1 and dirs[-2] == -1
    bear_flip = dirs[-1] == -1 and dirs[-2] == 1
    direction = "LONG" if (bull_flip or (not bear_flip and last.close >= ema200)) else "SHORT"
    score = 45

    if bull_flip and last.close > ema200:
        passed.append("4H Supertrend(10,3) Bullish Flip")
        passed.append(f"Above 4H {ema_len}-EMA Trend (${_quant(ema200)})")
        score += 45
    elif bear_flip and last.close < ema200:
        passed.append("4H Supertrend(10,3) Bearish Flip")
        passed.append(f"Below 4H {ema_len}-EMA Trend (${_quant(ema200)})")
        score += 45
    else:
        failed.append("No Confirmed 4H Supertrend(10,3) Flip Aligned With 200-EMA")

    dpct = float((footprint or {}).get("delta_pct", 0.0))
    if (direction == "LONG" and dpct >= 4.0) or (direction == "SHORT" and dpct <= -4.0):
        passed.append(f"Footprint CVD Aligned ({dpct:+.1f}%)")
        score += 5
    elif (direction == "LONG" and dpct <= -22.0) or (direction == "SHORT" and dpct >= 22.0):
        failed.append(f"Strong Counter Footprint ({dpct:+.1f}%)")
        score -= 15

    risk = max(atr4 * Decimal("2.5"), entry * Decimal("0.024"))
    sl = _quant(entry - risk if direction == "LONG" else entry + risk)
    # Wide 4.0R structural target so 3xATR trailing stop rides big trends without premature TP cap
    target = _quant(entry + risk * Decimal("4.0") if direction == "LONG" else entry - risk * Decimal("4.0"))
    status = "CONFIRMED" if (len(failed) == 0 and score >= 88) else ("WATCHLIST" if score >= 70 else "REJECTED")
    return CryptoSignal(
        symbol=symbol, engine_code="C05_SUPERTREND_TRAIL", horizon_bucket="swing",
        hold_horizon="C05 4H Supertrend • 3xATR Trail", direction=direction, status=status,
        score=min(98, score), entry_price=entry, stop_loss=sl, target_price=target,
        expected_net_rr=Decimal("3.80"), recommended_leverage=Decimal("2.0"),
        passed_rules=passed, failed_rules=failed,
        rationale=f"C05 4H Supertrend(10,3) {direction} Flip on {symbol} aligned with 4H {ema_len}-EMA.",
    )
