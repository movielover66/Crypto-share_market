"""
TRADEIQ Crypto Pro v2.0 — Native Free LuxAlgo-Inspired Smart Money Concepts (SMC) Filter.
Zero paid subscriptions required; runs 100% natively on free OKX/Bybit/Kraken OHLCV candles:
  1. Premium / Discount / Equilibrium Zone (recent 30-bar swing range)
  2. Fair Value Gap (FVG) — 3-candle institutional imbalance detection
  3. Order Block (OB) — last opposing candle before impulsive displacement (>= 1.5x avg body)
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SMCEvaluation:
    zone: str                        # "DISCOUNT" | "EQUILIBRIUM" | "PREMIUM"
    range_pos_pct: float             # 0.0 (swing low) to 100.0 (swing high)
    bullish_fvg: Optional[Tuple[float, float]] = None
    bearish_fvg: Optional[Tuple[float, float]] = None
    bullish_ob: Optional[Tuple[float, float]] = None
    bearish_ob: Optional[Tuple[float, float]] = None
    in_bullish_zone: bool = False
    in_bearish_zone: bool = False
    score_delta: int = 0
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "zone": self.zone,
            "range_pos_pct": self.range_pos_pct,
            "bullish_fvg": list(self.bullish_fvg) if self.bullish_fvg else None,
            "bearish_fvg": list(self.bearish_fvg) if self.bearish_fvg else None,
            "bullish_ob": list(self.bullish_ob) if self.bullish_ob else None,
            "bearish_ob": list(self.bearish_ob) if self.bearish_ob else None,
            "in_bullish_zone": self.in_bullish_zone,
            "in_bearish_zone": self.in_bearish_zone,
            "score_delta": self.score_delta,
            "tags": self.tags,
        }


def evaluate_smc_context(candles: List[Any], direction: str = "LONG") -> SMCEvaluation:
    """
    Evaluates Premium/Discount Zone, Fair Value Gaps (FVG), and Order Blocks (OB)
    on OHLCV candles and computes an alignment score bonus/penalty for `direction`.
    """
    if not candles or len(candles) < 10:
        return SMCEvaluation(zone="EQUILIBRIUM", range_pos_pct=50.0, score_delta=0, tags=[])

    window = candles[-30:] if len(candles) >= 30 else candles
    highs = [float(c.high) for c in window]
    lows = [float(c.low) for c in window]
    opens = [float(c.open) for c in window]
    closes = [float(c.close) for c in window]
    ltp = closes[-1]

    swing_high = max(highs)
    swing_low = min(lows)
    span = swing_high - swing_low
    range_pos = round(((ltp - swing_low) / span) * 100.0, 1) if span > 1e-12 else 50.0

    if range_pos <= 45.0:
        zone = "DISCOUNT"
    elif range_pos >= 55.0:
        zone = "PREMIUM"
    else:
        zone = "EQUILIBRIUM"

    # 1. Detect most recent Bullish & Bearish Fair Value Gaps (3-candle imbalance)
    bull_fvg: Optional[Tuple[float, float]] = None
    bear_fvg: Optional[Tuple[float, float]] = None
    for i in range(len(window) - 1, 1, -1):
        c0_h, c0_l = highs[i - 2], lows[i - 2]
        c2_h, c2_l = highs[i], lows[i]
        if bull_fvg is None and c2_l > c0_h and (c2_l - c0_h) / max(ltp, 1e-9) >= 0.0012:
            bull_fvg = (round(c0_h, 6), round(c2_l, 6))
        if bear_fvg is None and c2_h < c0_l and (c0_l - c2_h) / max(ltp, 1e-9) >= 0.0012:
            bear_fvg = (round(c2_h, 6), round(c0_l, 6))
        if bull_fvg and bear_fvg:
            break

    # 2. Detect most recent Bullish & Bearish Order Blocks (opposing candle before >=1.5x displacement)
    bodies = [abs(closes[i] - opens[i]) for i in range(len(window))]
    avg_body = sum(bodies) / max(1, len(bodies))
    bull_ob: Optional[Tuple[float, float]] = None
    bear_ob: Optional[Tuple[float, float]] = None
    for i in range(len(window) - 1, 0, -1):
        disp = bodies[i] >= (avg_body * 1.5) and avg_body > 0
        if not disp:
            continue
        is_bull_disp = closes[i] > opens[i]
        is_bear_disp = closes[i] < opens[i]
        prev_bear = closes[i - 1] < opens[i - 1]
        prev_bull = closes[i - 1] > opens[i - 1]
        if bull_ob is None and is_bull_disp and prev_bear:
            bull_ob = (round(lows[i - 1], 6), round(highs[i - 1], 6))
        if bear_ob is None and is_bear_disp and prev_bull:
            bear_ob = (round(lows[i - 1], 6), round(highs[i - 1], 6))
        if bull_ob and bear_ob:
            break

    tol = ltp * 0.008  # 0.8% proximity tolerance
    near_bull_fvg = bool(bull_fvg and (bull_fvg[0] - tol) <= ltp <= (bull_fvg[1] + tol))
    near_bear_fvg = bool(bear_fvg and (bear_fvg[0] - tol) <= ltp <= (bear_fvg[1] + tol))
    near_bull_ob = bool(bull_ob and (bull_ob[0] - tol) <= ltp <= (bull_ob[1] + tol))
    near_bear_ob = bool(bear_ob and (bear_ob[0] - tol) <= ltp <= (bear_ob[1] + tol))

    in_bull = zone == "DISCOUNT" or near_bull_fvg or near_bull_ob
    in_bear = zone == "PREMIUM" or near_bear_fvg or near_bear_ob

    delta = 0
    tags: List[str] = []
    d = (direction or "LONG").upper()

    if d == "LONG":
        if zone == "DISCOUNT":
            delta += 4
            tags.append(f"SMC Discount Zone ({range_pos:.0f}%)")
        elif range_pos >= 90.0:
            delta -= 4
            tags.append(f"SMC Extreme Premium ({range_pos:.0f}%)")
        if near_bull_fvg:
            delta += 4
            tags.append("SMC Bullish FVG Tap")
        if near_bull_ob:
            delta += 3
            tags.append("SMC Demand Order Block")
    else:  # SHORT
        if zone == "PREMIUM":
            delta += 4
            tags.append(f"SMC Premium Zone ({range_pos:.0f}%)")
        elif range_pos <= 10.0:
            delta -= 4
            tags.append(f"SMC Extreme Discount ({range_pos:.0f}%)")
        if near_bear_fvg:
            delta += 4
            tags.append("SMC Bearish FVG Tap")
        if near_bear_ob:
            delta += 3
            tags.append("SMC Supply Order Block")

    delta = max(-6, min(10, delta))
    return SMCEvaluation(
        zone=zone,
        range_pos_pct=range_pos,
        bullish_fvg=bull_fvg,
        bearish_fvg=bear_fvg,
        bullish_ob=bull_ob,
        bearish_ob=bear_ob,
        in_bullish_zone=in_bull,
        in_bearish_zone=in_bear,
        score_delta=delta,
        tags=tags,
    )
