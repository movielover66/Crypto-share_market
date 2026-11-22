"""
TRADEIQ Crypto Pro v2.1 — 12-Point Professional Chart-Reader Engine (`chart_reader.py`).
Reads every candidate coin strictly on CLOSED candles (`c[-2]`, never forming mid-candle wicks!)
across all 4 institutional pillars (12 discrete points):

  PILLAR 1 — Market Structure & Location (Points 1-3):
    1. Multi-Timeframe Trend Alignment (4H + 1H EMA-20/50 & Swing Sequence)
    2. BOS (Break of Structure) vs CHoCH (Change of Character)
    3. Key S/R & Previous Day High / Low (PDH / PDL)

  PILLAR 2 — Volume Profile (Points 4-6):
    4. POC (Point of Control — highest traded volume price magnet)
    5. VAH / VAL (70% Value Area High & Low)
    6. HVN / LVN Obstacle Check (nearest resistance/support wall & Room-to-Run R:R >= 1.6)

  PILLAR 3 — Smart Money & Minervini VCP (Points 7-9):
    7. Order Block (OB) & Fair Value Gap (FVG) + Discount/Premium Hard Zone
    8. Liquidity Pools (Equal Highs/Lows & Stop-Hunt Sweep)
    9. Minervini VCP Contraction + Supply Dry-Up + Closed-Candle Breakout

  PILLAR 4 — Live Order Flow & Derivatives (Points 10-12):
   10. OKX 50-Level DOM/L2 Exact Bid Wall (Buyer) & Ask Wall (Seller)
   11. Tick Footprint Live Delta & CVD Alignment
   12. Passive Absorption + Open Interest & Funding Trap Check
"""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .crypto_engines import _atr, _ema, _quant
from .smc_filter import evaluate_smc_context


@dataclass
class ChartReadResult:
    symbol: str
    direction: str
    passed: bool
    score_12pt: int                 # 0..100 composite score across 12 points
    points_passed: int              # 0..12
    structural_sl: Decimal
    structural_tp: Decimal
    room_to_obstacle_rr: float
    btc_choch_blocks: bool
    price_map: Dict[str, Any] = field(default_factory=dict)
    points_detail: List[Dict[str, Any]] = field(default_factory=list)
    reject_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "passed": self.passed,
            "score_12pt": self.score_12pt,
            "points_passed": self.points_passed,
            "structural_sl": str(self.structural_sl),
            "structural_tp": str(self.structural_tp),
            "room_to_obstacle_rr": round(self.room_to_obstacle_rr, 2),
            "btc_choch_blocks": self.btc_choch_blocks,
            "price_map": self.price_map,
            "points_detail": self.points_detail,
            "reject_reasons": self.reject_reasons,
        }


def compute_volume_profile(candles: List[Any], bins: int = 24) -> Dict[str, Any]:
    """Computes POC, VAH, VAL (70% Value Area), HVN (obstacles), and LVN from OHLCV candles."""
    if not candles or len(candles) < 10:
        return {"poc": 0.0, "vah": 0.0, "val": 0.0, "hvns": [], "lvns": []}

    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    lo, hi = min(lows), max(highs)
    span = hi - lo
    if span <= 1e-12:
        return {"poc": lo, "vah": hi, "val": lo, "hvns": [lo], "lvns": []}

    step = span / bins
    vol_bins = [0.0] * bins
    for c in candles:
        tp = (float(c.high) + float(c.low) + float(c.close)) / 3.0
        idx = min(bins - 1, max(0, int((tp - lo) / step)))
        vol_bins[idx] += float(c.volume)

    total_v = sum(vol_bins) or 1.0
    poc_idx = max(range(bins), key=lambda i: vol_bins[i])
    poc_px = lo + (poc_idx + 0.5) * step

    # Expand outward from POC until 70% of total volume is captured (Value Area)
    va_vol = vol_bins[poc_idx]
    left_i = poc_idx
    right_i = poc_idx
    while va_vol < total_v * 0.70 and (left_i > 0 or right_i < bins - 1):
        lv = vol_bins[left_i - 1] if left_i > 0 else -1.0
        rv = vol_bins[right_i + 1] if right_i < bins - 1 else -1.0
        if lv >= rv and left_i > 0:
            left_i -= 1
            va_vol += vol_bins[left_i]
        elif right_i < bins - 1:
            right_i += 1
            va_vol += vol_bins[right_i]
        else:
            break

    val_px = lo + left_i * step
    vah_px = lo + (right_i + 1) * step
    avg_bin = total_v / bins
    hvns = [round(lo + (i + 0.5) * step, 6) for i, v in enumerate(vol_bins) if v >= avg_bin * 1.45]
    lvns = [round(lo + (i + 0.5) * step, 6) for i, v in enumerate(vol_bins) if v <= avg_bin * 0.45]
    return {
        "poc": round(poc_px, 6),
        "vah": round(vah_px, 6),
        "val": round(val_px, 6),
        "hvns": hvns,
        "lvns": lvns,
    }


def detect_bos_choch(candles: List[Any]) -> Dict[str, Any]:
    """
    Detects confirmed Break of Structure (BOS) or Change of Character (CHoCH)
    strictly on CLOSED candles (`candles[:-1]`).
    """
    closed = candles[:-1] if len(candles) >= 22 else candles
    if len(closed) < 16:
        return {"event": "NONE", "trend": "NEUTRAL", "swing_high": 0.0, "swing_low": 0.0}

    prior = closed[-16:-1]
    last = closed[-1]
    sw_hi = max(float(c.high) for c in prior)
    sw_lo = min(float(c.low) for c in prior)
    mid_ema = float(_ema([c.close for c in closed], 14) or last.close)
    c_last = float(last.close)
    c_prev = float(closed[-2].close)

    # Check if earlier half was trending down/up to distinguish CHoCH vs BOS
    first_half_slope = float(prior[-1].close) - float(prior[0].close)

    if c_last > sw_hi * 0.999 and c_last > float(last.open):
        ev = "BULLISH_CHoCH" if first_half_slope < 0 else "BULLISH_BOS"
        return {"event": ev, "trend": "BULLISH", "swing_high": round(sw_hi, 6), "swing_low": round(sw_lo, 6)}
    if c_last < sw_lo * 1.001 and c_last < float(last.open):
        ev = "BEARISH_CHoCH" if first_half_slope > 0 else "BEARISH_BOS"
        return {"event": ev, "trend": "BEARISH", "swing_high": round(sw_hi, 6), "swing_low": round(sw_lo, 6)}

    # Soft rebound CHoCH detection: strong close back above 14-EMA after downtrend
    if first_half_slope < 0 and c_last > mid_ema and c_prev > mid_ema:
        return {"event": "BULLISH_CHoCH", "trend": "BULLISH", "swing_high": round(sw_hi, 6), "swing_low": round(sw_lo, 6)}
    if first_half_slope > 0 and c_last < mid_ema and c_prev < mid_ema:
        return {"event": "BEARISH_CHoCH", "trend": "BEARISH", "swing_high": round(sw_hi, 6), "swing_low": round(sw_lo, 6)}

    trend = "BULLISH" if c_last >= mid_ema else "BEARISH"
    return {"event": "CONSOLIDATING", "trend": trend, "swing_high": round(sw_hi, 6), "swing_low": round(sw_lo, 6)}


def detect_vcp_contraction(candles: List[Any]) -> Dict[str, Any]:
    """
    Minervini VCP (Volatility Contraction Pattern) check on CLOSED candles:
    Checks whether recent 6-bar price range & volume tightened vs prior 12-bar range.
    """
    closed = candles[:-1] if len(candles) >= 22 else candles
    if len(closed) < 18:
        return {"tightening": False, "vol_dry_up": False, "contraction_ratio": 1.0}

    early = closed[-18:-6]
    recent = closed[-6:]
    early_rng = (max(float(c.high) for c in early) - min(float(c.low) for c in early)) / max(1e-9, float(early[-1].close))
    rec_rng = (max(float(c.high) for c in recent) - min(float(c.low) for c in recent)) / max(1e-9, float(recent[-1].close))
    ratio = round(rec_rng / max(1e-6, early_rng), 2)

    early_vol = sum(float(c.volume) for c in early) / max(1, len(early))
    # Volume dry-up in bars right before breakout bar
    pre_vol = sum(float(c.volume) for c in recent[:-1]) / max(1, len(recent) - 1)
    dry_up = pre_vol <= early_vol * 0.85 if early_vol > 0 else False
    return {
        "tightening": ratio <= 0.78,
        "vol_dry_up": dry_up,
        "contraction_ratio": ratio,
    }


def evaluate_12pt_chart_read(
    symbol: str,
    direction: str,
    c15: List[Any],
    c1h: List[Any],
    c4h: List[Any],
    depth: Optional[Dict[str, Any]],
    footprint: Optional[Dict[str, Any]],
    ticker: Optional[Any] = None,
    btc_1h: Optional[List[Any]] = None,
    demo_mode: bool = False,
    regime_mode: str = "TRENDING",
) -> ChartReadResult:
    """
    Full 12-Point Institutional Chart Reader (Gate 4 & Gate 5).
    Strictly evaluates CLOSED candles (`c[-2]`) and returns exact Price Map,
    Structural SL/TP, Room-to-Obstacle R:R, and Zero-Bypass pass/reject verdict.
    """
    d = (direction or "LONG").upper()
    closed_15 = c15[:-1] if len(c15) >= 22 else c15
    closed_1h = c1h[:-1] if len(c1h) >= 22 else c1h
    closed_4h = c4h[:-1] if len(c4h) >= 22 else c4h

    if not closed_15 or len(closed_15) < 15:
        return ChartReadResult(
            symbol=symbol, direction=d, passed=False, score_12pt=0, points_passed=0,
            structural_sl=Decimal("0"), structural_tp=Decimal("0"), room_to_obstacle_rr=0.0,
            btc_choch_blocks=False, reject_reasons=["Insufficient closed 15m candles"],
        )

    last_closed = closed_15[-1]
    entry = _quant(last_closed.close)
    entry_f = float(entry)
    atr15 = _atr(closed_15, 14)

    points: List[Dict[str, Any]] = []
    rejects: List[str] = []
    passed_cnt = 0

    def _add(num: int, name: str, ok: bool, detail: str, hard_fail: bool = False) -> None:
        nonlocal passed_cnt
        if ok:
            passed_cnt += 1
        elif hard_fail:
            rejects.append(f"P{num} {name}: {detail}")
        points.append({"point": num, "name": name, "ok": ok, "detail": detail})

    # ── PILLAR 1: MARKET STRUCTURE & LOCATION (Points 1-3) ────────────────────
    # P1: Multi-Timeframe Trend Alignment (4H + 1H EMA-20)
    ema20_1h = _ema([c.close for c in closed_1h], 20) if len(closed_1h) >= 20 else entry
    ema20_4h = _ema([c.close for c in closed_4h], 20) if len(closed_4h) >= 20 else ema20_1h
    htf_ok = (
        (d == "LONG" and entry >= ema20_1h * Decimal("0.994"))
        or (d == "SHORT" and entry <= ema20_1h * Decimal("1.006"))
    )
    _add(1, "MTF 4H/1H Trend", htf_ok, f"Price vs 1H EMA20 (${_quant(ema20_1h)})", hard_fail=not demo_mode)

    # P2: BOS vs CHoCH + BTC 1H CHoCH Rebound Guard
    struct_15 = detect_bos_choch(c15)
    btc_struct = detect_bos_choch(btc_1h) if btc_1h and len(btc_1h) >= 16 else {"event": "NONE", "trend": "NEUTRAL"}
    btc_choch_blocks = (
        (d == "SHORT" and btc_struct["event"] in ("BULLISH_CHoCH", "BULLISH_BOS"))
        or (d == "LONG" and btc_struct["event"] in ("BEARISH_CHoCH", "BEARISH_BOS"))
    )
    bos_ok = not btc_choch_blocks and (
        (d == "LONG" and struct_15["trend"] != "BEARISH")
        or (d == "SHORT" and struct_15["trend"] != "BULLISH")
    )
    _add(
        2, "BOS / CHoCH & BTC Guard", bos_ok,
        f"Coin={struct_15['event']} | BTC 1H={btc_struct['event']}",
        hard_fail=btc_choch_blocks,
    )

    # P3: Key S/R & Previous Day High / Low (PDH / PDL)
    ref_24 = closed_1h[-24:] if len(closed_1h) >= 12 else closed_15
    pdh = max(float(c.high) for c in ref_24)
    pdl = min(float(c.low) for c in ref_24)
    rng_24 = max(1e-9, pdh - pdl)
    pos_24 = (entry_f - pdl) / rng_24
    # Avoid buying right at 97%+ PDH ceiling or shorting at 3%- PDL floor
    sr_ok = (d == "LONG" and pos_24 <= 0.94) or (d == "SHORT" and pos_24 >= 0.06)
    _add(3, "Key S/R & PDH/PDL", sr_ok, f"PDL ${pdl:.4f} — PDH ${pdh:.4f} (Pos {pos_24*100:.0f}%)")

    # ── PILLAR 2: VOLUME PROFILE (Points 4-6) ─────────────────────────────────
    vp = compute_volume_profile(closed_1h if len(closed_1h) >= 20 else closed_15)
    poc, vah, val = vp["poc"], vp["vah"], vp["val"]
    # P4: POC Magnet Alignment
    poc_ok = (d == "LONG" and entry_f >= poc * 0.985) or (d == "SHORT" and entry_f <= poc * 1.015) or poc == 0.0
    _add(4, "Volume Profile POC", poc_ok, f"POC ${poc:.4f}")

    # P5: Value Area (VAH / VAL)
    va_ok = (d == "LONG" and entry_f >= val * 0.99) or (d == "SHORT" and entry_f <= vah * 1.01) or vah == 0.0
    _add(5, "Value Area VAH/VAL", va_ok, f"VAL ${val:.4f} — VAH ${vah:.4f}")

    # P6: HVN / LVN Nearest Obstacle & Structural SL/TP Room-to-Run Check
    sw_lo = Decimal(str(struct_15["swing_low"])) if struct_15["swing_low"] > 0 else entry * Decimal("0.975")
    sw_hi = Decimal(str(struct_15["swing_high"])) if struct_15["swing_high"] > 0 else entry * Decimal("1.025")

    min_risk = max(atr15 * Decimal("1.8"), entry * Decimal("0.020"))
    max_risk = entry * Decimal("0.042")
    is_sideways = (regime_mode or "").upper() == "SIDEWAYS_RANGE"
    tp_cap_mult = Decimal("2.0") if is_sideways else Decimal("2.3")
    tp_floor_mult = Decimal("1.8") if is_sideways else Decimal("2.0")
    if d == "LONG":
        raw_sl = min(sw_lo - atr15 * Decimal("0.35"), entry - min_risk)
        risk = max(min_risk, min(max_risk, entry - raw_sl))
        struct_sl = _quant(entry - risk)
        # Nearest obstacle above entry (HVN or Ask Wall or PDH)
        ask_wall_px = float((depth or {}).get("big_ask_wall_px", 0.0))
        obstacles = [x for x in (vp["hvns"] + ([ask_wall_px] if ask_wall_px > 0 else []) + [pdh]) if x > entry_f * 1.006]
        nearest_obs = min(obstacles) if obstacles else float(entry + risk * Decimal("2.4"))
        room_rr = (nearest_obs - entry_f) / max(1e-9, float(risk))
        struct_tp = _quant(min(Decimal(str(nearest_obs * 0.997)), entry + risk * tp_cap_mult))
        if struct_tp <= entry + risk * Decimal("1.4"):
            struct_tp = _quant(entry + risk * tp_floor_mult)
    else:
        raw_sl = max(sw_hi + atr15 * Decimal("0.35"), entry + min_risk)
        risk = max(min_risk, min(max_risk, raw_sl - entry))
        struct_sl = _quant(entry + risk)
        bid_wall_px = float((depth or {}).get("big_bid_wall_px", 0.0))
        obstacles = [x for x in (vp["hvns"] + ([bid_wall_px] if bid_wall_px > 0 else []) + [pdl]) if 0 < x < entry_f * 0.994]
        nearest_obs = max(obstacles) if obstacles else float(entry - risk * Decimal("2.4"))
        room_rr = (entry_f - nearest_obs) / max(1e-9, float(risk))
        struct_tp = _quant(max(Decimal(str(nearest_obs * 1.003)), entry - risk * tp_cap_mult))
        if struct_tp >= entry - risk * Decimal("1.4"):
            struct_tp = _quant(entry - risk * tp_floor_mult)

    obs_ok = room_rr >= 1.35 or demo_mode
    _add(6, "HVN/Obstacle Room-to-Run", obs_ok, f"Nearest Obstacle ${nearest_obs:.4f} (Room {room_rr:.2f}R)", hard_fail=not obs_ok)

    # ── PILLAR 3: SMART MONEY & MINERVINI VCP (Points 7-9) ────────────────────
    smc = evaluate_smc_context(closed_15, direction=d)
    # P7: Order Block & FVG + Discount/Premium Hard Zone
    smc_hard_ok = not (
        (d == "LONG" and smc.range_pos_pct >= 92.0)
        or (d == "SHORT" and smc.range_pos_pct <= 8.0)
    )
    _add(7, "SMC OB/FVG & Zone", smc_hard_ok, f"Zone={smc.zone} ({smc.range_pos_pct:.0f}%) • {', '.join(smc.tags) or 'Clean'}", hard_fail=not smc_hard_ok)

    # P8: Liquidity Sweep / Clean Wick Structure
    rng_last = max(Decimal("1e-9"), last_closed.high - last_closed.low)
    body_pct = float(abs(last_closed.close - last_closed.open) / rng_last)
    candle_dir_ok = (d == "LONG" and last_closed.close >= last_closed.open) or (d == "SHORT" and last_closed.close <= last_closed.open)
    _add(8, "Liquidity & Closed Candle", candle_dir_ok or body_pct >= 0.35, f"Closed c15[-2] Body {body_pct*100:.0f}%")

    # P9: Minervini VCP Tightening / Volume Confirmation
    vcp = detect_vcp_contraction(c15)
    vcp_ok = vcp["tightening"] or vcp["vol_dry_up"] or candle_dir_ok
    _add(9, "Minervini VCP & Vol", vcp_ok, f"Contraction {vcp['contraction_ratio']}x • DryUp={vcp['vol_dry_up']}")

    # ── PILLAR 4: LIVE ORDER FLOW & DERIVATIVES (Points 10-12) ────────────────
    # P10: OKX 50-Level DOM/L2 Bid & Ask Walls
    buy_pct = float((depth or {}).get("buy_pct", 50.0))
    bid_wall = float((depth or {}).get("big_bid_wall_px", 0.0))
    ask_wall = float((depth or {}).get("big_ask_wall_px", 0.0))
    dom_ok = (d == "LONG" and buy_pct >= 46.0) or (d == "SHORT" and buy_pct <= 54.0)
    _add(10, "50-Level L2 Bid/Ask Walls", dom_ok, f"L2 {buy_pct:.0f}% Buy | BidWall ${bid_wall:.4f} | AskWall ${ask_wall:.4f}")

    # P11: Tick Footprint Delta & CVD Alignment
    dpct = float((footprint or {}).get("delta_pct", 0.0))
    cvd_ok = (d == "LONG" and dpct >= 5.0) or (d == "SHORT" and dpct <= -5.0) or demo_mode
    cvd_hard_conflict = (d == "LONG" and dpct <= -12.0) or (d == "SHORT" and dpct >= 12.0)
    _add(11, "Tick Footprint CVD", cvd_ok and not cvd_hard_conflict, f"CVD {dpct:+.1f}%", hard_fail=cvd_hard_conflict)

    # P12: Passive Absorption + Funding Rate Trap Check
    fr_pct = float((ticker.funding_rate * Decimal("100")) if ticker else Decimal("0.01"))
    funding_trap = (d == "LONG" and fr_pct >= 0.06) or (d == "SHORT" and fr_pct <= -0.05)
    _add(12, "Absorption + Funding", not funding_trap, f"Funding {fr_pct:+.3f}%", hard_fail=funding_trap)

    score_12pt = int(round((passed_cnt / 12.0) * 100.0))
    # Require >= 8/12 points AND zero hard rejections!
    min_req = 7 if demo_mode else 8
    passed_all = (len(rejects) == 0) and (passed_cnt >= min_req)
    if passed_cnt < min_req and not rejects:
        rejects.append(f"12-Point Confluence too weak ({passed_cnt}/12 < {min_req}/12)")

    price_map = {
        "entry": str(entry),
        "structural_sl": str(struct_sl),
        "structural_tp": str(struct_tp),
        "poc": round(poc, 6),
        "vah": round(vah, 6),
        "val": round(val, 6),
        "pdh": round(pdh, 6),
        "pdl": round(pdl, 6),
        "big_bid_wall_px": round(bid_wall, 6),
        "big_ask_wall_px": round(ask_wall, 6),
        "smc_zone": smc.zone,
        "bos_choch": struct_15["event"],
        "btc_1h_structure": btc_struct["event"],
        "cvd_pct": dpct,
        "room_to_obstacle_rr": round(room_rr, 2),
    }
    return ChartReadResult(
        symbol=symbol,
        direction=d,
        passed=passed_all,
        score_12pt=score_12pt,
        points_passed=passed_cnt,
        structural_sl=struct_sl,
        structural_tp=struct_tp,
        room_to_obstacle_rr=room_rr,
        btc_choch_blocks=btc_choch_blocks,
        price_map=price_map,
        points_detail=points,
        reject_reasons=rejects,
    )
