"""
TRADEIQ Crypto Pro v2.0 — Layer 4 & 5 Portfolio Manager, Soft Bias Blender,
Equal-Risk Volatility Sizing, Risk Firewall, and Per-Engine Attribution Scorecard.
"""
import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..core.strategy_config import StrategyConfig


@dataclass
class BiasDecision:
    allowed: bool
    risk_multiplier: Decimal
    bias_tag: str
    reason: str


def evaluate_soft_bias(
    direction: str,
    engine_code: str,
    score: int,
    brain_dir: Optional[Any],
    footprint: Optional[Dict[str, Any]],
    cfg: StrategyConfig,
) -> BiasDecision:
    """
    Layer 4 Soft Bias Blending (replaces v1.0 Hard Lock & Dual-Regime Conflict):
      - Single source of truth: MarketBrainEngine (`brain_dir`).
      - Boom Radar (1x) strictly respects `brain_dir.allow_boom_long` (paused in crash/downtrend).
      - With-Trend or Sideways Range: full 1.0x risk.
      - Counter-Trend (e.g., SHORT in green market or LONG in downtrend):
          Allowed at 0.5x defensive risk ONLY when Soft Bias is enabled AND
          score >= counter_trend_min_score (90) AND footprint CVD confirms direction
          (or ENGINE_4_SHADOW_SWEEP / ENGINE_5_CRASH_SHORT specialist override).
    """
    d = (direction or "LONG").upper()
    if engine_code == "BOOM_RADAR":
        if brain_dir and not brain_dir.allow_boom_long:
            return BiasDecision(False, Decimal("0.00"), "BOOM_LOCKED", f"Boom Radar paused in {brain_dir.mode}")
        return BiasDecision(True, Decimal("1.00"), "BOOM_ALIGNED", "Boom Radar allowed by Market Brain")

    if brain_dir is None:
        return BiasDecision(True, Decimal("1.00"), "NEUTRAL_FULL", "Market Brain syncing — full 1.0x risk")

    trend_ok = (d == "LONG" and brain_dir.allow_longs) or (d == "SHORT" and brain_dir.allow_shorts)
    if trend_ok:
        return BiasDecision(True, Decimal("1.00"), "TREND_ALIGNED", f"{d} aligned with Brain ({brain_dir.mode})")

    # v2.1 Zero-Bypass Rule: ENGINE_5_CRASH_SHORT only allowed when Brain actually allows shorts
    if engine_code == "ENGINE_5_CRASH_SHORT" and d == "SHORT" and brain_dir.mode not in ("DOWNTREND", "SEVERE_CRASH"):
        return BiasDecision(False, Decimal("0.00"), "CRASH_EXPIRED", f"Crash Short blocked in {brain_dir.mode}")

    # Unger Chop Filter: block trend breakouts in choppy Hurst < 0.48 markets
    if getattr(brain_dir, "hurst", 0.55) < 0.48 and engine_code in ("ENGINE_1_BREAKOUT", "ENGINE_2_DONCHIAN"):
        return BiasDecision(False, Decimal("0.00"), "UNGER_CHOP_BLOCK", f"Choppy market (Hurst {brain_dir.hurst} < 0.48)")

    if not cfg.soft_bias_enabled:
        return BiasDecision(False, Decimal("0.00"), "HARD_LOCKED", f"{d} blocked in {brain_dir.mode} (Soft Bias OFF)")

    fp_delta = float((footprint or {}).get("delta_pct", 0.0))
    cvd_confirms = (
        (d == "LONG" and fp_delta >= cfg.counter_trend_min_abs_cvd)
        or (d == "SHORT" and fp_delta <= -cfg.counter_trend_min_abs_cvd)
    )
    if score >= cfg.counter_trend_min_score and cvd_confirms:
        mult = cfg.counter_trend_risk_multiplier
        return BiasDecision(
            True,
            mult,
            f"SOFT_BIAS_{mult}X",
            f"A+ Counter-Trend {d} (Score {score}, CVD {fp_delta:+.1f}%) -> {mult}x defensive sizing",
        )

    return BiasDecision(
        False,
        Decimal("0.00"),
        "COUNTER_REJECTED",
        f"Counter-trend {d} in {brain_dir.mode} needs Score>={cfg.counter_trend_min_score} & strong CVD (got {score}, CVD {fp_delta:+.1f}%)",
    )


def compute_equal_risk_sizing(
    entry_price: Decimal,
    stop_loss: Decimal,
    leverage: Decimal,
    usdt_inr: Decimal,
    risk_multiplier: Decimal,
    cfg: StrategyConfig,
    fallback_margin_usdt: Decimal,
    is_boom: bool = False,
) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Layer 4 Equal-Risk Volatility-Targeted Position Sizing:
      margin_usdt = (risk_usdt / sl_dist_pct) / leverage
    Guarantees every trade caps max SL loss near `risk_per_trade_inr * risk_multiplier`
    (e.g., ₹500 with trend, ₹250 counter-trend) instead of wild fixed-margin swings.
    Returns (margin_usdt, quantity, planned_risk_inr).
    """
    fx = usdt_inr if usdt_inr > Decimal("50") else Decimal("90.00")
    lev = max(Decimal("1.0"), leverage)

    if not cfg.equal_risk_enabled or entry_price <= 0 or stop_loss <= 0:
        base = (fallback_margin_usdt * Decimal("0.5") if is_boom else fallback_margin_usdt) * risk_multiplier
        margin = max(Decimal("10.00"), base).quantize(Decimal("0.01"))
        qty = ((margin * lev) / entry_price).quantize(Decimal("0.0001")) if entry_price > 0 else Decimal("0")
        sl_dist = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else Decimal("0.02")
        est_risk_inr = (margin * lev * sl_dist * fx).quantize(Decimal("0.01"))
        return margin, qty, est_risk_inr

    raw_sl_pct = abs(entry_price - stop_loss) / entry_price
    sl_pct = max(Decimal("0.004"), min(Decimal("0.15"), raw_sl_pct))

    effective_mult = (risk_multiplier * Decimal("0.60")) if is_boom else risk_multiplier
    planned_risk_inr = (cfg.risk_per_trade_inr * effective_mult).quantize(Decimal("0.01"))
    target_risk_usdt = planned_risk_inr / fx
    notional_usdt = target_risk_usdt / sl_pct
    raw_margin = notional_usdt / lev

    margin = max(cfg.min_margin_usdt, min(cfg.max_margin_usdt, raw_margin)).quantize(Decimal("0.01"))
    actual_notional = margin * lev
    qty = (actual_notional / entry_price).quantize(Decimal("0.0001"))
    actual_risk_inr = (actual_notional * sl_pct * fx).quantize(Decimal("0.01"))
    return margin, qty, actual_risk_inr


def check_risk_firewall(
    symbol: str,
    direction: str,
    engine_code: str,
    is_boom: bool,
    open_positions: List[Any],
    all_positions: List[Any],
    risk_blocked_syms: set,
    now_ist: datetime.datetime,
    cfg: StrategyConfig,
    fallback_max_open: int = 5,
    funding_rate: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    Layer 5 Risk Firewall:
      - Always enforces duplicate-symbol lock, 6h SL cooldown, and high-risk news shield.
      - Respects `cfg.unlimited_paper_mode` / `0 = Unlimited` for max open, per-direction,
        boom open, and daily trade caps, while enforcing any user-set finite cap (2, 3, 4, 5...).
    """
    sym = symbol.upper()
    open_syms = {p.symbol.upper() for p in open_positions}
    if sym in open_syms:
        return False, f"Duplicate lock: {sym} already open"

    if sym in risk_blocked_syms and engine_code != "ENGINE_5_CRASH_SHORT":
        return False, f"Macro High-Risk News Shield active on {sym}"

    # Funding Rate Crowding Guard (prevents Long/Short Squeeze traps)
    if funding_rate is not None:
        if direction == "LONG" and funding_rate >= 0.0005:
            return False, f"Funding Crowding Guard: +{funding_rate*100:.3f}%/8h overheated LONG squeeze risk"
        if direction == "SHORT" and funding_rate <= -0.0005 and engine_code != "ENGINE_5_CRASH_SHORT":
            return False, f"Funding Crowding Guard: {funding_rate*100:.3f}%/8h extreme SHORT squeeze risk"

    # 6-hour per-coin SL cooldown
    if cfg.sl_cooldown_hours > 0:
        cooldown_sec = cfg.sl_cooldown_hours * 3600
        for cp in all_positions:
            if cp.status == "CLOSED" and cp.symbol.upper() == sym and cp.outcome == "LOSS" and cp.closed_at_ist:
                try:
                    c_dt = datetime.datetime.fromisoformat(cp.closed_at_ist)
                    if (now_ist - c_dt).total_seconds() < cooldown_sec:
                        return False, f"{cfg.sl_cooldown_hours}h SL cooldown active on {sym}"
                except Exception:
                    pass

    # Finite vs Unlimited caps
    eff_max_open = 0 if (cfg.unlimited_paper_mode and cfg.max_open_positions == 0) else (
        cfg.max_open_positions if cfg.max_open_positions > 0 else (0 if cfg.unlimited_paper_mode else fallback_max_open)
    )
    corr_cap = getattr(cfg, "max_same_direction_cap", 4)
    eff_max_dir = cfg.max_per_direction if cfg.max_per_direction > 0 else corr_cap
    eff_max_boom = 0 if cfg.unlimited_paper_mode and cfg.max_boom_open == 0 else cfg.max_boom_open
    eff_max_daily = 0 if cfg.unlimited_paper_mode and cfg.max_daily_trades == 0 else cfg.max_daily_trades

    if is_boom:
        if eff_max_boom > 0:
            boom_open = sum(1 for p in open_positions if p.horizon_bucket == "boom" or p.engine_code == "BOOM_RADAR")
            if boom_open >= eff_max_boom:
                return False, f"Max Boom positions reached ({eff_max_boom})"
    else:
        if eff_max_open > 0 and len(open_positions) >= eff_max_open:
            return False, f"Max open positions reached ({eff_max_open})"
        if eff_max_dir > 0:
            dir_open = sum(1 for p in open_positions if p.direction == direction and p.horizon_bucket != "boom")
            if dir_open >= eff_max_dir:
                return False, f"Max {direction} positions reached ({eff_max_dir})"

    if eff_max_daily > 0:
        today_str = now_ist.strftime("%Y-%m-%d")
        opened_today = sum(1 for p in all_positions if (p.opened_at_ist or "").startswith(today_str))
        if opened_today >= eff_max_daily:
            return False, f"Max daily trades reached ({eff_max_daily})"

    return True, "FIREWALL_PASSED"


def build_engine_scorecard(
    perp_positions: List[Any],
    spot_positions: List[Any],
    usdt_inr: Decimal,
) -> Dict[str, Any]:
    """Layer 6 Per-Engine Attribution Scorecard across all Perpetual engines + Spot Vault."""
    fx = usdt_inr if usdt_inr > Decimal("50") else Decimal("90.00")
    buckets: Dict[str, Dict[str, Any]] = {}

    def _ensure(code: str, label: str) -> Dict[str, Any]:
        if code not in buckets:
            buckets[code] = {
                "engine_code": code,
                "label": label,
                "open_count": 0,
                "closed_count": 0,
                "wins": 0,
                "losses": 0,
                "gross_win_usdt": Decimal("0.00"),
                "gross_loss_usdt": Decimal("0.00"),
                "net_pnl_usdt": Decimal("0.00"),
            }
        return buckets[code]

    label_map = {
        "ENGINE_1_BREAKOUT": "E1 Breakout Momentum",
        "ENGINE_2_DONCHIAN": "E2 Donchian Swing",
        "ENGINE_3_FUNDING_RSI2": "E3 Funding Mean-Rev",
        "ENGINE_4_SHADOW_SWEEP": "E4 Shadow Sweep",
        "ENGINE_5_CRASH_SHORT": "E5 Crash Short",
        "BOOM_RADAR": "Boom Radar (1x)",
        "CATALYST_MOMENTUM": "Macro Catalyst",
        "WEBHOOK_EXTERNAL": "External Webhook",
    }

    for code, lbl in label_map.items():
        _ensure(code, lbl)
    _ensure("SPOT_VAULT", "Spot Buy Vault (DCA)")

    for p in perp_positions:
        code = p.engine_code or "ENGINE_1_BREAKOUT"
        b = _ensure(code, label_map.get(code, code))
        if p.status == "OPEN":
            b["open_count"] += 1
        else:
            b["closed_count"] += 1
            b["net_pnl_usdt"] += p.net_pnl_usdt
            if p.net_pnl_usdt > 0:
                b["wins"] += 1
                b["gross_win_usdt"] += p.net_pnl_usdt
            elif p.net_pnl_usdt < 0:
                b["losses"] += 1
                b["gross_loss_usdt"] += abs(p.net_pnl_usdt)

    sb = buckets["SPOT_VAULT"]
    for sp in spot_positions:
        if sp.status == "OPEN":
            sb["open_count"] += 1
        else:
            sb["closed_count"] += 1
            sb["net_pnl_usdt"] += sp.net_pnl_usdt
            if sp.net_pnl_usdt >= 0:
                sb["wins"] += 1
                sb["gross_win_usdt"] += sp.net_pnl_usdt
            else:
                sb["losses"] += 1
                sb["gross_loss_usdt"] += abs(sp.net_pnl_usdt)

    rows: List[Dict[str, Any]] = []
    for b in buckets.values():
        closed = b["closed_count"]
        wr = round((b["wins"] / closed) * 100.0, 1) if closed > 0 else 0.0
        gw, gl = b["gross_win_usdt"], b["gross_loss_usdt"]
        pf = round(float(gw / gl), 2) if gl > 0 else (99.99 if gw > 0 else 0.0)
        net_u = b["net_pnl_usdt"].quantize(Decimal("0.01"))
        net_i = (net_u * fx).quantize(Decimal("0.01"))
        rows.append({
            "engine_code": b["engine_code"],
            "label": b["label"],
            "open_count": b["open_count"],
            "closed_count": closed,
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate_pct": wr,
            "profit_factor": pf,
            "net_pnl_usdt": str(net_u),
            "net_pnl_inr": str(net_i),
        })

    rows.sort(key=lambda r: (r["closed_count"] + r["open_count"], float(r["net_pnl_usdt"])), reverse=True)
    return {"usdt_inr": str(fx), "engines": rows}
