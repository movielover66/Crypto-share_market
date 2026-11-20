"""
TRADEIQ Crypto Pro — Institutional Spot Buy Vault (No Stop-Loss, 3-Step Smart DCA, 4-Point Bounce Check).
- Blue-Chip Whitelist for Auto-Scan (BTC, ETH, SOL, BNB, XRP, LINK, AVAX, SUI, ADA, TON)
- 4-Point Pre-Buy Bounce Filter: Dip/Support + Oversold RSI/BB + OKX Footprint/L2 Absorption + Macro News Shield
- 3-Step Smart DCA (40% / 30% / 30%) at 0%, -4.0%, -8.5% dips to pull Average Buy Price down
- Zero Stop-Loss (1x Spot Holding — zero liquidation risk)
- Exact 0.10% Buy + 0.10% Sell Spot fee accounting so Net Profit hits exact editable Target % (default 5.0%)
"""
import datetime
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from .crypto_engines import _rsi

def _sma(vals, period):
    return sum(vals[-period:]) / Decimal(str(period)) if len(vals) >= period else vals[-1]
from .crypto_simulator import IST
from ..providers.crypto_provider import CryptoTicker

BLUECHIP_SPOT_UNIVERSE: Tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "SUIUSDT",
    "ADAUSDT",
    "TONUSDT",
)

SPOT_FEE_RATE = Decimal("0.001")  # 0.10% Buy + 0.10% Sell = 0.20% round-trip
TRANCHE_WEIGHTS: Tuple[Decimal, Decimal, Decimal] = (
    Decimal("0.40"),
    Decimal("0.30"),
    Decimal("0.30"),
)


def _q(val: Decimal, prec: str = "0.01") -> Decimal:
    return val.quantize(Decimal(prec), rounding=ROUND_HALF_UP)


def _px_prec(px: Decimal) -> str:
    if px < Decimal("0.01"):
        return "0.000001"
    if px < Decimal("1"):
        return "0.0001"
    if px < Decimal("10"):
        return "0.001"
    return "0.01"


@dataclass
class SpotVaultConfig:
    enabled: bool = True
    starting_usdt: Decimal = Decimal("1000.00")
    per_coin_budget_usdt: Decimal = Decimal("120.00")  # ~₹10,000–₹11,000 per coin (editable)
    target_net_pct: Decimal = Decimal("5.0")           # Default +5.0% net after 0.20% spot fees (editable)
    max_active_coins: int = 3
    dca_step2_dip_pct: Decimal = Decimal("-4.0")
    dca_step3_dip_pct: Decimal = Decimal("-8.5")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "starting_usdt": str(self.starting_usdt),
            "per_coin_budget_usdt": str(self.per_coin_budget_usdt),
            "target_net_pct": str(self.target_net_pct),
            "max_active_coins": self.max_active_coins,
            "dca_step2_dip_pct": str(self.dca_step2_dip_pct),
            "dca_step3_dip_pct": str(self.dca_step3_dip_pct),
        }


@dataclass
class SpotPosition:
    position_id: str
    symbol: str
    mode: str                         # "AUTO_DCA" or "MANUAL_SPOT"
    budget_usdt: Decimal              # Total allocated budget for this coin across 3 tranches
    total_invested_usdt: Decimal      # Actual USDT deployed so far
    total_qty: Decimal                # Net coin units held (after 0.10% buy fee)
    avg_entry_price: Decimal          # Weighted average buy price
    first_entry_price: Decimal        # Tranche 1 price (reference for DCA step dips)
    target_net_pct: Decimal           # Target net profit % (e.g. 5.0 or 10.0)
    net_target_price: Decimal         # Exact price where selling clears 0.10% sell fee & nets target_net_pct
    buy_fees_usdt: Decimal            # Cumulative 0.10% buy fees paid
    tranches_filled: int = 1          # 1, 2, or 3
    max_tranches: int = 3
    status: str = "OPEN"              # OPEN or CLOSED
    exit_fill_price: Optional[Decimal] = None
    exit_reason: Optional[str] = None
    net_pnl_usdt: Decimal = Decimal("0.00")
    outcome: Optional[str] = None     # WIN / CLOSED
    opened_at_ist: str = ""
    closed_at_ist: Optional[str] = None
    rationale: str = ""
    tg_message_id: Optional[int] = None


def compute_net_target_price(total_invested: Decimal, total_qty: Decimal, target_net_pct: Decimal) -> Decimal:
    """Computes exact sell price so net proceeds after 0.10% sell fee equal total_invested * (1 + target_net_pct/100)."""
    if total_qty <= 0 or total_invested <= 0:
        return Decimal("0")
    desired_net_proceeds = total_invested * (Decimal("1") + (target_net_pct / Decimal("100")))
    gross_needed = desired_net_proceeds / (Decimal("1") - SPOT_FEE_RATE)
    px = gross_needed / total_qty
    return _q(px, _px_prec(px))


def evaluate_spot_bounce(
    symbol: str,
    c15: List[Any],
    c1h: List[Any],
    ticker: Optional[CryptoTicker],
    depth: Optional[Dict[str, Any]],
    footprint: Optional[Dict[str, Any]],
    risk_blocked: set,
) -> Dict[str, Any]:
    """
    4-Point Pre-Buy Bounce Check ("পড়লে উঠবে কিনা?" Filter):
      1. Dip & Support Zone: >= 2.0% pullback from 24h high OR price in lower 35% of 24h range
      2. Oversold Exhaustion: 15m RSI <= 38 OR 1h RSI <= 42 OR lower Bollinger zone
      3. OKX Live Footprint & Orderbook Absorption: blocks active panic seller dumps
      4. Macro Catalyst Safety: no active hack/delisting/high-risk shock for this coin
    """
    ltp = ticker.ltp if ticker and ticker.ltp > 0 else (c15[-1].close if c15 else Decimal("0"))
    chg24 = ticker.change_24h_pct if ticker else Decimal("0")
    if ltp <= 0:
        return {
            "symbol": symbol, "ltp": "0", "change_24h_pct": "0.00", "dip_from_high_pct": "0.00",
            "rsi_15m": "50.0", "footprint_delta_pct": "0.0", "buy_pct": "50.0",
            "score": 0, "checks_passed": 0, "stage": "NO_DATA",
            "passed_rules": [], "failed_rules": ["No live price data"],
        }

    # 1. Dip & Support Zone check using 1h candles (last 24 bars)
    closes_15 = [c.close for c in c15] if c15 else [ltp]
    closes_1h = [c.close for c in c1h] if c1h else closes_15
    high_24h = max((c.high for c in c1h[-24:]), default=ltp) if c1h else max((c.high for c in c15), default=ltp)
    low_24h = min((c.low for c in c1h[-24:]), default=ltp) if c1h else min((c.low for c in c15), default=ltp)
    dip_from_high = ((ltp - high_24h) / high_24h * Decimal("100")) if high_24h > 0 else Decimal("0")
    rng_24h = high_24h - low_24h
    range_pos = ((ltp - low_24h) / rng_24h) if rng_24h > 0 else Decimal("0.5")

    passed: List[str] = []
    failed: List[str] = []
    score = 0

    # Check 1: Meaningful Dip / Support Zone
    if dip_from_high <= Decimal("-2.0") or range_pos <= Decimal("0.35") or chg24 <= Decimal("-2.0"):
        passed.append(f"Support Dip ({_q(dip_from_high)}% from 24h High)")
        score += 28
    else:
        failed.append(f"No Dip Yet ({_q(dip_from_high)}% from High)")

    # Check 2: Oversold Exhaustion (15m RSI / 1h RSI / Bollinger Lower Zone)
    rsi_15 = _rsi(closes_15, 14) if len(closes_15) >= 15 else Decimal("50")
    rsi_1h = _rsi(closes_1h, 14) if len(closes_1h) >= 15 else Decimal("50")
    sma20 = _sma(closes_15, 20) if len(closes_15) >= 20 else ltp
    below_sma = ltp < sma20 * Decimal("0.994")
    if rsi_15 <= Decimal("38") or rsi_1h <= Decimal("42") or (rsi_15 <= Decimal("44") and below_sma):
        passed.append(f"Oversold Bounce Zone (RSI {rsi_15.quantize(Decimal('0.1'))})")
        score += 27
    else:
        failed.append(f"RSI Not Oversold ({rsi_15.quantize(Decimal('0.1'))})")

    # Check 3: OKX Live Footprint & Orderbook Absorption (Anti-Dump)
    fp_delta = Decimal(str((footprint or {}).get("delta_pct", 0.0)))
    buy_pct = Decimal(str((depth or {}).get("buy_pct", 50.0)))
    dump_blocked = fp_delta < Decimal("-12.0")
    if dump_blocked:
        failed.append(f"Seller Dump Active (Footprint {fp_delta:+.1f}%)")
    elif fp_delta >= Decimal("-6.0") and (buy_pct >= Decimal("47.0") or fp_delta >= Decimal("2.0")):
        passed.append(f"Buyer Absorption (L2 {buy_pct:.0f}% • CVD {fp_delta:+.1f}%)")
        score += 25
    else:
        failed.append(f"Weak Bid Absorption (L2 {buy_pct:.0f}% • CVD {fp_delta:+.1f}%)")

    # Check 4: Macro Catalyst News Safety Shield
    if symbol.upper() not in risk_blocked:
        passed.append("Macro News Safe")
        score += 20
    else:
        failed.append("High-Risk News Shield Active")

    checks_passed = len(passed)
    if dump_blocked or symbol.upper() in risk_blocked:
        stage = "DUMP_BLOCKED"
    elif checks_passed >= 4 and score >= 80:
        stage = "BUY_READY"
    elif checks_passed >= 3 and dip_from_high <= Decimal("-2.0"):
        stage = "WATCHING_DIP"
    else:
        stage = "WAIT_FOR_DIP"

    return {
        "symbol": symbol,
        "ltp": str(_q(ltp, _px_prec(ltp))),
        "change_24h_pct": str(_q(chg24)),
        "dip_from_high_pct": str(_q(dip_from_high)),
        "rsi_15m": str(_q(rsi_15, "0.1")),
        "footprint_delta_pct": str(_q(fp_delta, "0.1")),
        "buy_pct": str(_q(buy_pct, "0.1")),
        "score": min(100, score),
        "checks_passed": checks_passed,
        "stage": stage,
        "passed_rules": passed,
        "failed_rules": failed,
    }


class SpotVaultEngine:
    def __init__(self, config: Optional[SpotVaultConfig] = None) -> None:
        self.config = config or SpotVaultConfig()
        self.positions: Dict[str, SpotPosition] = {}
        self.last_radar: List[Dict[str, Any]] = []
        self.last_scan_ist: Optional[str] = None

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        if "enabled" in updates and updates["enabled"] is not None:
            self.config.enabled = bool(updates["enabled"])
        if "starting_usdt" in updates and updates["starting_usdt"] is not None:
            self.config.starting_usdt = max(Decimal("50"), Decimal(str(updates["starting_usdt"])))
        if "per_coin_budget_usdt" in updates and updates["per_coin_budget_usdt"] is not None:
            self.config.per_coin_budget_usdt = max(Decimal("10"), Decimal(str(updates["per_coin_budget_usdt"])))
        if "target_net_pct" in updates and updates["target_net_pct"] is not None:
            self.config.target_net_pct = max(Decimal("1.0"), min(Decimal("50.0"), Decimal(str(updates["target_net_pct"]))))
        if "max_active_coins" in updates and updates["max_active_coins"] is not None:
            self.config.max_active_coins = max(1, min(10, int(updates["max_active_coins"])))
        return self.config.to_dict()

    def open_spot_position(
        self,
        symbol: str,
        ltp: Decimal,
        now_ist: datetime.datetime,
        mode: str = "AUTO_DCA",
        budget_usdt: Optional[Decimal] = None,
        target_net_pct: Optional[Decimal] = None,
        single_shot: bool = False,
        rationale: str = "",
        store: Any = None,
        tg: Any = None,
        usdt_inr: Decimal = Decimal("85.20"),
    ) -> SpotPosition:
        sym = symbol.upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        budget = budget_usdt if (budget_usdt is not None and budget_usdt > 0) else self.config.per_coin_budget_usdt
        tgt_pct = target_net_pct if (target_net_pct is not None and target_net_pct > 0) else self.config.target_net_pct
        max_tr = 1 if single_shot else 3
        tr1_usdt = budget if single_shot else _q(budget * TRANCHE_WEIGHTS[0])
        buy_fee = _q(tr1_usdt * SPOT_FEE_RATE, "0.0001")
        net_usdt = tr1_usdt - buy_fee
        qty = _q(net_usdt / ltp, "0.000001") if ltp > 0 else Decimal("0")
        target_px = compute_net_target_price(tr1_usdt, qty, tgt_pct)

        pid = f"SPOT-{sym}-{uuid.uuid4().hex[:6].upper()}"
        pos = SpotPosition(
            position_id=pid,
            symbol=sym,
            mode=mode,
            budget_usdt=_q(budget),
            total_invested_usdt=_q(tr1_usdt),
            total_qty=qty,
            avg_entry_price=_q(ltp, _px_prec(ltp)),
            first_entry_price=_q(ltp, _px_prec(ltp)),
            target_net_pct=_q(tgt_pct, "0.1"),
            net_target_price=target_px,
            buy_fees_usdt=_q(buy_fee, "0.0001"),
            tranches_filled=1,
            max_tranches=max_tr,
            status="OPEN",
            opened_at_ist=now_ist.isoformat(),
            rationale=rationale or f"Spot Tranche 1/{max_tr} • Zero Stop-Loss • Net +{tgt_pct}% Target",
        )
        self.positions[pid] = pos
        if tg and tg.enabled:
            inr_val = _q(tr1_usdt * usdt_inr, "1")
            ts_str = now_ist.strftime("%I:%M %p IST")
            tg_res = None
            if hasattr(tg, "send_html"):
                msg = (
                    f"🟡🪙 <b>SPOT VAULT BUY ({pos.tranches_filled}/{pos.max_tranches})</b> • <code>{sym}</code>\n"
                    f"Buy <code>${pos.avg_entry_price}</code> | Target (+{pos.target_net_pct}%) <code>${pos.net_target_price}</code> | SL: <b>NONE</b>\n"
                    f"<i>Invested ${tr1_usdt} (₹{inr_val}) of ${budget} • {ts_str}</i>"
                )
                tg_res = tg.send_html(msg, btn_label="🪙 Open Spot Vault")
            else:
                tg_res = tg.send_alert(f"SPOT BUY — {sym}", f"Buy ${pos.avg_entry_price}", ts_str)
            if isinstance(tg_res, dict) and tg_res.get("message_id"):
                pos.tg_message_id = int(tg_res["message_id"])
        if store and store.is_configured():
            store.upsert_spot_position(pos)
        return pos

    def add_dca_tranche(
        self,
        position_id: str,
        ltp: Decimal,
        now_ist: datetime.datetime,
        reason: str = "AUTO_DCA_DIP",
        store: Any = None,
        tg: Any = None,
        usdt_inr: Decimal = Decimal("85.20"),
    ) -> Optional[SpotPosition]:
        pos = self.positions.get(position_id)
        if not pos or pos.status != "OPEN" or pos.tranches_filled >= pos.max_tranches or ltp <= 0:
            return None
        next_idx = pos.tranches_filled  # 1 -> weight[1] (30%), 2 -> weight[2] (30%)
        w = TRANCHE_WEIGHTS[min(next_idx, 2)]
        add_usdt = _q(pos.budget_usdt * w)
        add_fee = _q(add_usdt * SPOT_FEE_RATE, "0.0001")
        add_qty = _q((add_usdt - add_fee) / ltp, "0.000001")

        pos.total_invested_usdt = _q(pos.total_invested_usdt + add_usdt)
        pos.buy_fees_usdt = _q(pos.buy_fees_usdt + add_fee, "0.0001")
        pos.total_qty = _q(pos.total_qty + add_qty, "0.000001")
        pos.tranches_filled += 1
        # Exact weighted average buy price across all tranches
        if pos.total_qty > 0:
            raw_avg = (pos.total_invested_usdt - pos.buy_fees_usdt) / pos.total_qty
            pos.avg_entry_price = _q(raw_avg, _px_prec(raw_avg))
        pos.net_target_price = compute_net_target_price(pos.total_invested_usdt, pos.total_qty, pos.target_net_pct)
        pos.rationale = f"DCA Tranche {pos.tranches_filled}/{pos.max_tranches} added at ${ltp} ({reason})"

        if store and store.is_configured():
            store.upsert_spot_position(pos)
        if tg and tg.enabled:
            inr_tot = _q(pos.total_invested_usdt * usdt_inr, "1")
            ts_str = now_ist.strftime("%I:%M %p IST")
            if hasattr(tg, "send_html"):
                msg = (
                    f"🟡📥 <b>SPOT DCA #{pos.tranches_filled}/{pos.max_tranches}</b> • <code>{pos.symbol}</code>\n"
                    f"New Avg <code>${pos.avg_entry_price}</code> | Target <code>${pos.net_target_price}</code> | SL: <b>NONE</b>\n"
                    f"<i>Added ${add_usdt} @ ${ltp} • Total ${pos.total_invested_usdt} (₹{inr_tot}) • {ts_str}</i>"
                )
                tg.send_html(msg, btn_label="🪙 Open Spot Vault", reply_to_message_id=pos.tg_message_id)
            else:
                tg.send_alert(f"SPOT DCA #{pos.tranches_filled} — {pos.symbol}", f"Avg ${pos.avg_entry_price}", ts_str)
        return pos

    def close_spot_position(
        self,
        position_id: str,
        exit_ltp: Decimal,
        reason: str,
        now_ist: datetime.datetime,
        store: Any = None,
        tg: Any = None,
        usdt_inr: Decimal = Decimal("85.20"),
    ) -> Optional[SpotPosition]:
        pos = self.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            return None
        gross_proceeds = pos.total_qty * exit_ltp
        sell_fee = gross_proceeds * SPOT_FEE_RATE
        net_proceeds = gross_proceeds - sell_fee
        net_pnl = _q(net_proceeds - pos.total_invested_usdt)

        pos.status = "CLOSED"
        pos.exit_fill_price = _q(exit_ltp, _px_prec(exit_ltp))
        pos.exit_reason = reason
        pos.net_pnl_usdt = net_pnl
        pos.outcome = "WIN" if net_pnl >= 0 else "LOSS"
        pos.closed_at_ist = now_ist.isoformat()

        if store and store.is_configured():
            store.close_spot_position(pos)
        if tg and tg.enabled:
            inr_pnl = _q(net_pnl * usdt_inr, "1")
            sign = "+" if net_pnl >= 0 else ""
            pct_net = _q((net_pnl / pos.total_invested_usdt) * Decimal("100")) if pos.total_invested_usdt > 0 else Decimal("0.00")
            ts_str = now_ist.strftime("%d %b • %I:%M %p IST")
            cat = "SPOT_WIN" if net_pnl >= 0 else "SPOT_EXIT"
            badge = "SPOT TARGET HIT" if reason == "SPOT_TARGET_HIT" else "SPOT SOLD"
            cap = (
                f"🟡🏆 <b>{badge}</b> • <code>{pos.symbol}</code>\n"
                f"Sold <code>${pos.exit_fill_price}</code> | Net <b>{sign}${net_pnl}</b> ({sign}{pct_net}% • ₹{inr_pnl})"
            )
            if hasattr(tg, "send_pnl_card"):
                tg.send_pnl_card(
                    category=cat,
                    badge_text=badge,
                    symbol=pos.symbol,
                    sub_pill=f"SPOT DCA {pos.tranches_filled}/{pos.max_tranches} • NO SL",
                    big_pnl=f"{sign}${net_pnl}",
                    sub_pnl=f"{sign}{pct_net}%  |  {sign}Rs {abs(inr_pnl)} Net",
                    col1=("AVG BUY", f"${pos.avg_entry_price}"),
                    col2=("SOLD EXIT", f"${pos.exit_fill_price}"),
                    col3=("STOP-LOSS", "NONE (1x)"),
                    caption_html=cap,
                    ts_label=ts_str,
                    btn_label="🪙 Open Spot Vault",
                    reply_to_message_id=pos.tg_message_id,
                )
            else:
                tg.send_alert(f"SPOT {reason} — {pos.symbol}", f"Net {sign}${net_pnl}", ts_str)
        return pos

    def monitor_open_positions(
        self,
        ltp_map: Dict[str, Decimal],
        footprints: Dict[str, Dict[str, Any]],
        now_ist: datetime.datetime,
        store: Any = None,
        tg: Any = None,
        usdt_inr: Decimal = Decimal("85.20"),
    ) -> List[Dict[str, Any]]:
        """Monitors all OPEN Spot Vault holdings: Zero Stop-Loss, Auto DCA on dips, Auto Sell on Net Target Hit."""
        actions: List[Dict[str, Any]] = []
        for pos in list(self.positions.values()):
            if pos.status != "OPEN":
                continue
            ltp = ltp_map.get(pos.symbol)
            if not ltp or ltp <= 0:
                continue

            # 1. Check Net Target Hit (Auto Profit Booking!)
            if ltp >= pos.net_target_price and pos.net_target_price > 0:
                closed = self.close_spot_position(pos.position_id, ltp, "SPOT_TARGET_HIT", now_ist, store, tg, usdt_inr)
                if closed:
                    actions.append({"action": "SPOT_TARGET_HIT", "symbol": pos.symbol, "net_pnl_usdt": str(closed.net_pnl_usdt)})
                continue

            # 2. Check Auto DCA Tranche 2 & 3 (only if max_tranches > 1 and not active panic dump)
            if pos.tranches_filled < pos.max_tranches and pos.first_entry_price > 0:
                dip_from_t1 = ((ltp - pos.first_entry_price) / pos.first_entry_price) * Decimal("100")
                fp_delta = Decimal(str((footprints.get(pos.symbol) or {}).get("delta_pct", 0.0)))
                if fp_delta >= Decimal("-12.0"):
                    if pos.tranches_filled == 1 and dip_from_t1 <= self.config.dca_step2_dip_pct:
                        self.add_dca_tranche(pos.position_id, ltp, now_ist, f"DIP_{_q(dip_from_t1)}%", store, tg, usdt_inr)
                        actions.append({"action": "SPOT_DCA_2", "symbol": pos.symbol, "price": str(ltp)})
                    elif pos.tranches_filled == 2 and dip_from_t1 <= self.config.dca_step3_dip_pct:
                        self.add_dca_tranche(pos.position_id, ltp, now_ist, f"DIP_{_q(dip_from_t1)}%", store, tg, usdt_inr)
                        actions.append({"action": "SPOT_DCA_3", "symbol": pos.symbol, "price": str(ltp)})
        return actions

    def vault_ledger(self, ltp_map: Dict[str, Decimal], usdt_inr: Decimal = Decimal("85.20")) -> Dict[str, Any]:
        open_list: List[Dict[str, Any]] = []
        closed_list: List[Dict[str, Any]] = []
        deployed = Decimal("0.00")
        unrealised_net = Decimal("0.00")
        realised_net = Decimal("0.00")
        wins = 0

        for pos in self.positions.values():
            if pos.status == "OPEN":
                ltp = ltp_map.get(pos.symbol, pos.avg_entry_price)
                gross_val = pos.total_qty * ltp
                net_val = gross_val * (Decimal("1") - SPOT_FEE_RATE)
                unr = _q(net_val - pos.total_invested_usdt)
                unr_pct = _q((unr / pos.total_invested_usdt) * Decimal("100")) if pos.total_invested_usdt > 0 else Decimal("0.00")
                dist_to_tgt = _q(((pos.net_target_price - ltp) / ltp) * Decimal("100")) if ltp > 0 else Decimal("0.00")
                deployed += pos.total_invested_usdt
                unrealised_net += unr
                open_list.append({
                    "position_id": pos.position_id,
                    "symbol": pos.symbol,
                    "mode": pos.mode,
                    "budget_usdt": str(pos.budget_usdt),
                    "total_invested_usdt": str(pos.total_invested_usdt),
                    "total_invested_inr": str(_q(pos.total_invested_usdt * usdt_inr, "1")),
                    "total_qty": str(pos.total_qty),
                    "avg_entry_price": str(pos.avg_entry_price),
                    "current_ltp": str(_q(ltp, _px_prec(ltp))),
                    "target_net_pct": str(pos.target_net_pct),
                    "net_target_price": str(pos.net_target_price),
                    "dist_to_target_pct": str(dist_to_tgt),
                    "tranches_filled": pos.tranches_filled,
                    "max_tranches": pos.max_tranches,
                    "stop_loss": "NONE (1x Spot)",
                    "unrealised_net_usdt": str(unr),
                    "unrealised_net_inr": str(_q(unr * usdt_inr, "1")),
                    "unrealised_net_pct": str(unr_pct),
                    "opened_at_ist": pos.opened_at_ist,
                    "rationale": pos.rationale,
                })
            else:
                realised_net += pos.net_pnl_usdt
                if pos.net_pnl_usdt >= 0:
                    wins += 1
                closed_list.append({
                    "position_id": pos.position_id,
                    "symbol": pos.symbol,
                    "mode": pos.mode,
                    "total_invested_usdt": str(pos.total_invested_usdt),
                    "avg_entry_price": str(pos.avg_entry_price),
                    "exit_fill_price": str(pos.exit_fill_price or "0"),
                    "tranches_filled": pos.tranches_filled,
                    "max_tranches": pos.max_tranches,
                    "net_pnl_usdt": str(pos.net_pnl_usdt),
                    "net_pnl_inr": str(_q(pos.net_pnl_usdt * usdt_inr, "1")),
                    "outcome": pos.outcome or "WIN",
                    "exit_reason": pos.exit_reason or "SPOT_TARGET_HIT",
                    "closed_at_ist": pos.closed_at_ist,
                })

        total_net = _q(realised_net + unrealised_net)
        total_equity = _q(self.config.starting_usdt + total_net)
        free_usdt = _q(max(Decimal("0.00"), self.config.starting_usdt + realised_net - deployed))
        closed_list.sort(key=lambda x: x.get("closed_at_ist") or "", reverse=True)
        return {
            "starting_usdt": str(self.config.starting_usdt),
            "total_equity_usdt": str(total_equity),
            "total_equity_inr": str(_q(total_equity * usdt_inr, "1")),
            "deployed_usdt": str(_q(deployed)),
            "deployed_inr": str(_q(deployed * usdt_inr, "1")),
            "free_usdt": str(free_usdt),
            "realised_net_usdt": str(_q(realised_net)),
            "realised_net_inr": str(_q(realised_net * usdt_inr, "1")),
            "unrealised_net_usdt": str(_q(unrealised_net)),
            "total_net_usdt": str(total_net),
            "total_net_inr": str(_q(total_net * usdt_inr, "1")),
            "open_count": len(open_list),
            "closed_count": len(closed_list),
            "wins": wins,
            "open_positions": open_list,
            "closed_trades": closed_list,
        }
