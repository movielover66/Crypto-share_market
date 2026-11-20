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


def evaluate_engine_1_breakout(symbol: str, c15: List[CryptoCandle], depth: Dict[str, Any]) -> CryptoSignal:
    passed: List[str] = []
    failed: List[str] = []
    if len(c15) < 20:
        return CryptoSignal(symbol, "ENGINE_1_BREAKOUT", "intraday", "Intraday • 2–12 hrs (Session Breakout)", "LONG", "REJECTED", 0, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("2"), [], ["Insufficient 15m candles"], "Not enough 15m candles.")

    last = c15[-1]
    prior = c15[-17:-1]
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

    entry = _quant(last.close)
    risk = max(atr * Decimal("1.35"), entry * Decimal("0.008"))
    sl = _quant(entry - risk if direction == "LONG" else entry + risk)
    target = _quant(entry + risk * Decimal("2.4") if direction == "LONG" else entry - risk * Decimal("2.4"))
    net_rr = Decimal("2.25")
    status = "CONFIRMED" if (score >= 82 and len(failed) <= 1) else ("WATCHLIST" if score >= 68 else "REJECTED")
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


def evaluate_boom_radar(symbol: str, c15: List[CryptoCandle], ticker: Optional[CryptoTicker], depth: Dict[str, Any]) -> Dict[str, Any]:
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

    stage = "BOOM TRIGGERED" if score >= 82 else ("EARLY EXPANSION" if score >= 68 else "WATCHING BASE")
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
