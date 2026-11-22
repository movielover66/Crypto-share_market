"""
TRADEIQ Crypto v1.0 — Penny-Accurate USDT-M Perpetual Paper Simulator & Two-Tier Auto-Trader.
- Exact Decimal math with VIP 0 Perpetual fees (0.05% taker per leg) + slippage + 8h funding accrual.
- Two-Tier Trade Pipeline:
  * Backend Accuracy Lab (`BACKEND_LAB`): executes all confirmed setups for full Win Rate % tracking.
  * Telegram Elite Filter (`ELITE_TG`): only top A+ setups (score >= 85, capped at 5-7/day) go to Telegram.
- 1.5R Charge-Buffered Breakeven + 2.0R Trailing SL + 3-SL & 1.5% Daily-Loss Kill-Switch.
- Zero real-money exchange orders anywhere.
"""
import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .crypto_engines import CryptoRegime, CryptoSignal, _quant
from ..core.strategy_config import StrategyConfig
from .portfolio_manager import compute_equal_risk_sizing



def _fmt_inr_px(usd_val: Decimal, fx: Decimal) -> str:
    v = float(usd_val * fx)
    av = abs(v)
    if av < 1.0:
        return f"₹{v:,.4f}"
    if av < 100.0:
        return f"₹{v:,.2f}"
    return f"₹{v:,.0f}"


def _score_bar(score: int) -> str:
    filled = max(1, min(10, int(round(score / 10.0))))
    return "▰" * filled + "▱" * (10 - filled)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TAKER_FEE_RATE = Decimal("0.0005")  # 0.05% VIP 0 USDT-M Perpetual Taker
SLIPPAGE_RATE = Decimal("0.0002")   # 0.02% realistic orderbook slippage


@dataclass
class CryptoPaperPosition:
    position_id: str
    symbol: str
    horizon_bucket: str  # "intraday" | "reversion" | "swing" | "boom"
    hold_horizon: str
    engine_code: str
    direction: str  # "LONG" | "SHORT"
    tier: str  # "ELITE_TG" | "BACKEND_LAB" | "BOOM"
    leverage: Decimal
    margin_usdt: Decimal
    quantity: Decimal
    entry_fill_price: Decimal
    stop_loss: Decimal
    initial_stop_loss: Decimal
    target_price: Decimal
    status: str = "OPEN"  # "OPEN" | "CLOSED"
    exit_fill_price: Optional[Decimal] = None
    exit_reason: Optional[str] = None
    entry_fee_usdt: Decimal = Decimal("0.00")
    exit_fee_usdt: Decimal = Decimal("0.00")
    funding_paid_usdt: Decimal = Decimal("0.00")
    net_pnl_usdt: Decimal = Decimal("0.00")
    outcome: Optional[str] = None  # "WIN" | "LOSS" | "BREAKEVEN"
    breakeven_shifted: bool = False
    trailing_active: bool = False
    score: int = 80
    rationale: str = ""
    opened_at_ist: str = ""
    closed_at_ist: Optional[str] = None
    tg_message_id: Optional[int] = None
    bias_tag: str = "TREND_ALIGNED"
    planned_risk_inr: Decimal = Decimal("500.00")


@dataclass
class CryptoTraderConfig:
    enabled: bool = True
    starting_usdt: Decimal = Decimal("1000.00")
    per_trade_usdt: Decimal = Decimal("200.00")
    max_leverage: Decimal = Decimal("2.0")
    max_open_positions: int = 0  # 0 = Unlimited Paper Mode (editable anytime)
    telegram_daily_cap: int = 6  # Elite 5-7/day cap
    elite_min_score: int = 85
    daily_loss_limit_pct: Decimal = Decimal("1.50")
    max_consecutive_losses: int = 3
    enable_boom_radar: bool = True
    enable_catalyst_radar: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "starting_usdt": str(self.starting_usdt),
            "per_trade_usdt": str(self.per_trade_usdt),
            "max_leverage": str(self.max_leverage),
            "max_open_positions": self.max_open_positions,
            "telegram_daily_cap": self.telegram_daily_cap,
            "elite_min_score": self.elite_min_score,
            "daily_loss_limit_pct": str(self.daily_loss_limit_pct),
            "max_consecutive_losses": self.max_consecutive_losses,
            "enable_boom_radar": self.enable_boom_radar,
            "enable_catalyst_radar": self.enable_catalyst_radar,
        }


class CryptoPaperEngine:
    def __init__(self, config: Optional[CryptoTraderConfig] = None) -> None:
        self.config = config or CryptoTraderConfig()
        self.positions: Dict[str, CryptoPaperPosition] = {}
        self.kill_switch_active: bool = False
        self.kill_switch_reason: Optional[str] = None
        self.consecutive_losses: int = 0
        self.tg_sent_date: str = ""
        self.tg_sent_today: int = 0
        self.tg_reset_after_ist: str = ""
        self.last_regime: Optional[CryptoRegime] = None
        self.last_signals: List[Dict[str, Any]] = []
        self.last_boom_items: List[Dict[str, Any]] = []
        self.last_scan_ist: Optional[str] = None
        self.last_usdt_inr: Decimal = Decimal("93.37")

    def _reset_daily_tg_counter_if_needed(self, now_ist: datetime.datetime) -> None:
        today = now_ist.strftime("%Y-%m-%d")
        if self.tg_sent_date != today:
            self.tg_sent_date = today
            self.tg_sent_today = 0

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        c = self.config
        if "enabled" in updates and updates["enabled"] is not None:
            c.enabled = bool(updates["enabled"])
        if "starting_usdt" in updates and updates["starting_usdt"] is not None:
            c.starting_usdt = max(Decimal("50"), Decimal(str(updates["starting_usdt"]))).quantize(Decimal("0.01"))
        if "per_trade_usdt" in updates and updates["per_trade_usdt"] is not None:
            c.per_trade_usdt = max(Decimal("10"), min(c.starting_usdt, Decimal(str(updates["per_trade_usdt"])))).quantize(Decimal("0.01"))
        if "max_leverage" in updates and updates["max_leverage"] is not None:
            c.max_leverage = max(Decimal("1"), min(Decimal("3"), Decimal(str(updates["max_leverage"])))).quantize(Decimal("0.1"))
        if "telegram_daily_cap" in updates and updates["telegram_daily_cap"] is not None:
            c.telegram_daily_cap = max(1, min(15, int(updates["telegram_daily_cap"])))
        if "max_open_positions" in updates and updates["max_open_positions"] is not None:
            c.max_open_positions = max(0, min(200, int(updates["max_open_positions"])))
        if "enable_boom_radar" in updates and updates["enable_boom_radar"] is not None:
            c.enable_boom_radar = bool(updates["enable_boom_radar"])
        if "enable_catalyst_radar" in updates and updates["enable_catalyst_radar"] is not None:
            c.enable_catalyst_radar = bool(updates["enable_catalyst_radar"])
        if updates.get("reset_kill_switch"):
            self.kill_switch_active = False
            self.kill_switch_reason = None
            self.consecutive_losses = 0
        return c.to_dict()

    def open_position(
        self,
        sig: CryptoSignal,
        now_ist: datetime.datetime,
        paper_store: Optional[Any] = None,
        telegram_notifier: Optional[Any] = None,
        usdt_inr: Decimal = Decimal("93.37"),
        strategy_cfg: Optional[StrategyConfig] = None,
        risk_multiplier: Decimal = Decimal("1.00"),
        bias_tag: str = "TREND_ALIGNED",
        brain_context: str = "",
    ) -> CryptoPaperPosition:
        self._reset_daily_tg_counter_if_needed(now_ist)
        if usdt_inr and usdt_inr > Decimal("50"):
            self.last_usdt_inr = usdt_inr
        eff_fx = self.last_usdt_inr
        is_boom = sig.horizon_bucket == "boom" or sig.engine_code == "BOOM_RADAR"
        lev = Decimal("1.0") if (is_boom or sig.engine_code == "CATALYST_MOMENTUM") else min(self.config.max_leverage, sig.recommended_leverage)

        fill = _quant(
            sig.entry_price * (Decimal("1") + SLIPPAGE_RATE)
            if sig.direction == "LONG"
            else sig.entry_price * (Decimal("1") - SLIPPAGE_RATE)
        )
        # Bug #5 fix: Equal-Risk Volatility-Targeted Position Sizing (₹500 default, 0.5x counter-trend)
        if strategy_cfg is not None:
            margin, qty, planned_risk_inr = compute_equal_risk_sizing(
                entry_price=fill,
                stop_loss=sig.stop_loss,
                leverage=lev,
                usdt_inr=eff_fx,
                risk_multiplier=risk_multiplier,
                cfg=strategy_cfg,
                fallback_margin_usdt=self.config.per_trade_usdt,
                is_boom=is_boom,
            )
            notional = margin * lev
        else:
            margin = (self.config.per_trade_usdt * Decimal("0.5") if is_boom else self.config.per_trade_usdt).quantize(Decimal("0.01"))
            notional = margin * lev
            qty = (notional / fill).quantize(Decimal("0.0001")) if fill > 0 else Decimal("0")
            sl_dist = (abs(fill - sig.stop_loss) / fill) if fill > 0 else Decimal("0.02")
            planned_risk_inr = (notional * sl_dist * eff_fx).quantize(Decimal("0.01"))
        entry_fee = (notional * TAKER_FEE_RATE).quantize(Decimal("0.01"))

        # Decide Two-Tier status: BOOM stays in its own BOOM tier; ELITE_TG is strictly capped at telegram_daily_cap
        is_elite = (
            not is_boom
            and sig.score >= self.config.elite_min_score
            and self.tg_sent_today < self.config.telegram_daily_cap
        )
        tier = "BOOM" if is_boom else ("ELITE_TG" if is_elite else "BACKEND_LAB")

        pos = CryptoPaperPosition(
            position_id=f"CR-{uuid.uuid4().hex[:10].upper()}",
            symbol=sig.symbol.upper(),
            horizon_bucket=sig.horizon_bucket,
            hold_horizon=sig.hold_horizon,
            engine_code=sig.engine_code,
            direction=sig.direction,
            tier=tier,
            leverage=lev,
            margin_usdt=margin,
            quantity=qty,
            entry_fill_price=fill,
            stop_loss=sig.stop_loss,
            initial_stop_loss=sig.stop_loss,
            target_price=sig.target_price,
            entry_fee_usdt=entry_fee,
            score=sig.score,
            rationale=sig.rationale,
            opened_at_ist=now_ist.isoformat(),
            bias_tag=bias_tag,
            planned_risk_inr=planned_risk_inr,
        )
        self.positions[pos.position_id] = pos

        if is_elite and telegram_notifier and getattr(telegram_notifier, "enabled", False):
            self.tg_sent_today += 1
            ts_str = now_ist.strftime("%I:%M %p IST")
            e_inr = _fmt_inr_px(pos.entry_fill_price, eff_fx)
            tp_inr = _fmt_inr_px(pos.target_price, eff_fx)
            sl_inr = _fmt_inr_px(pos.stop_loss, eff_fx)
            m_inr = _fmt_inr_px(pos.margin_usdt, eff_fx)
            r_inr = f"₹{int(round(float(pos.planned_risk_inr))):,}"
            tp_pct = ((abs(pos.target_price - pos.entry_fill_price) / pos.entry_fill_price) * Decimal("100")).quantize(Decimal("0.1")) if pos.entry_fill_price > 0 else Decimal("0")
            sl_pct = ((abs(pos.entry_fill_price - pos.stop_loss) / pos.entry_fill_price) * Decimal("100")).quantize(Decimal("0.1")) if pos.entry_fill_price > 0 else Decimal("0")
            dot = "🟣" if is_boom else ("🟢" if pos.direction == "LONG" else "🔴")
            hdr = f"BOOM 1x" if is_boom else f"{pos.direction} {pos.leverage}x"
            eng_lbl = pos.engine_code.replace("ENGINE_", "E").replace("_", " ").title()
            br_txt = brain_context or "LIVE SYNC"
            bar = _score_bar(pos.score)
            msg = (
                f"<blockquote>"
                f"{dot} <b>{hdr}</b>  •  <code>{pos.symbol}</code>  •  <i>{eng_lbl}</i>\n\n"
                f"<b>Entry</b>   <code>{e_inr}</code>\n"
                f"<b>Target</b>  <code>{tp_inr}</code>  <i>(+{tp_pct}%)</i>\n"
                f"<b>Stop</b>    <code>{sl_inr}</code>  <i>(-{sl_pct}%)</i>\n\n"
                f"<b>Margin</b> <code>{m_inr}</code>  •  <b>Max Risk</b> <code>{r_inr}</code>\n"
                f"<b>Score</b>  <code>{bar}</code> <b>{pos.score}/100</b>\n"
                f"<b>Brain</b>  <code>{br_txt}</code>  •  <i>{ts_str}</i>"
                f"</blockquote>"
            )
            tg_res = None
            if hasattr(telegram_notifier, "send_html"):
                tg_res = telegram_notifier.send_html(msg, btn_label="")
            else:
                tg_res = telegram_notifier.send_alert(f"{hdr} — {pos.symbol}", f"Entry {e_inr} | TP {tp_inr} | SL {sl_inr}", ts_str)
            if isinstance(tg_res, dict) and tg_res.get("message_id"):
                pos.tg_message_id = int(tg_res["message_id"])

        if paper_store and paper_store.is_configured():
            try:
                paper_store.insert_position(pos)
            except Exception:
                pass
        return pos

    def close_position(
        self,
        position_id: str,
        exit_price: Decimal,
        reason: str,
        now_ist: datetime.datetime,
        paper_store: Optional[Any] = None,
        telegram_notifier: Optional[Any] = None,
        usdt_inr: Optional[Decimal] = None,
        disable_kill_switch: bool = False,
    ) -> Optional[CryptoPaperPosition]:
        pos = self.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            return None

        fill = _quant(
            exit_price * (Decimal("1") - SLIPPAGE_RATE)
            if pos.direction == "LONG"
            else exit_price * (Decimal("1") + SLIPPAGE_RATE)
        )
        notional_exit = fill * pos.quantity
        exit_fee = (notional_exit * TAKER_FEE_RATE).quantize(Decimal("0.01"))
        gross = (
            ((fill - pos.entry_fill_price) * pos.quantity)
            if pos.direction == "LONG"
            else ((pos.entry_fill_price - fill) * pos.quantity)
        ).quantize(Decimal("0.01"))
        net = (gross - pos.entry_fee_usdt - exit_fee - pos.funding_paid_usdt).quantize(Decimal("0.01"))

        pos.status = "CLOSED"
        pos.exit_fill_price = fill
        pos.exit_reason = reason
        pos.exit_fee_usdt = exit_fee
        pos.net_pnl_usdt = net
        pos.outcome = "WIN" if net > 0 else ("LOSS" if net < 0 else "BREAKEVEN")
        pos.closed_at_ist = now_ist.isoformat()

        if net < 0:
            self.consecutive_losses += 1
        elif net > 0:
            self.consecutive_losses = 0
        if not disable_kill_switch and self.consecutive_losses >= self.config.max_consecutive_losses:
            self.kill_switch_active = True
            self.kill_switch_reason = f"Consecutive losses reached {self.consecutive_losses}."

        if paper_store and paper_store.is_configured():
            try:
                paper_store.close_position(pos)
            except Exception:
                pass

        # Clean Pure ₹ INR Glass Quote Exit Reply (Zero Photos, Zero Link Buttons!)
        if telegram_notifier and getattr(telegram_notifier, "enabled", False):
            sign = "+" if net >= 0 else "-"
            is_boom_pos = pos.horizon_bucket == "boom" or pos.engine_code == "BOOM_RADAR"
            is_win = net >= 0
            pct_roi = ((net / pos.margin_usdt) * Decimal("100")).quantize(Decimal("0.01")) if pos.margin_usdt > 0 else Decimal("0.00")
            eff_fx = usdt_inr if (usdt_inr and usdt_inr > Decimal("50")) else self.last_usdt_inr
            inr_abs = abs(int((net * eff_fx).quantize(Decimal("1"))))
            e_inr = _fmt_inr_px(pos.entry_fill_price, eff_fx)
            x_inr = _fmt_inr_px(pos.exit_fill_price or exit_price, eff_fx)
            ts_str = now_ist.strftime("%I:%M %p IST")
            badge = "TARGET HIT" if reason == "TARGET_HIT" else ("STOP-LOSS HIT" if "SL" in reason else reason.replace("_", " "))
            icon = "🏆" if is_win else "🛑"
            pnl_lbl = "Net Profit" if is_win else "Net Loss"
            sub_hdr = "BOOM 1x" if is_boom_pos else f"{pos.direction} {pos.leverage}x"
            eng_lbl = pos.engine_code.replace("ENGINE_", "E").replace("_", " ").title()
            msg = (
                f"<blockquote>"
                f"{icon} <b>{badge}</b>  •  <code>{pos.symbol}</code> <i>({sub_hdr})</i>\n\n"
                f"<b>{pnl_lbl}</b>  <code>{sign}₹{inr_abs:,}</code>  <i>({'+' if pct_roi >= 0 else ''}{pct_roi}% ROI)</i>\n"
                f"<b>Price</b>       <code>{e_inr}</code> ➔ <code>{x_inr}</code>\n"
                f"<b>Engine</b>      <i>{eng_lbl} • Fee Paid • {ts_str}</i>"
                f"</blockquote>"
            )
            if hasattr(telegram_notifier, "send_html"):
                telegram_notifier.send_html(msg, btn_label="", reply_to_message_id=pos.tg_message_id)
            else:
                telegram_notifier.send_alert(f"{badge} — {pos.symbol}", f"Net {sign}₹{inr_abs:,}", ts_str)
        return pos

    def monitor_open_positions(
        self,
        live_ltps: Dict[str, Decimal],
        now_ist: datetime.datetime,
        paper_store: Optional[Any] = None,
        telegram_notifier: Optional[Any] = None,
        usdt_inr: Optional[Decimal] = None,
        disable_kill_switch: bool = False,
        regime_mode: str = "TRENDING",
    ) -> List[Dict[str, Any]]:
        if usdt_inr and usdt_inr > Decimal("50"):
            self.last_usdt_inr = usdt_inr
        actions: List[Dict[str, Any]] = []
        for pos in list(self.positions.values()):
            if pos.status != "OPEN":
                continue
            ltp = live_ltps.get(pos.symbol.upper())
            if not ltp or ltp <= 0:
                continue

            init_risk = max(pos.entry_fill_price * Decimal("0.002"), abs(pos.entry_fill_price - pos.initial_stop_loss))
            live_r = (
                (ltp - pos.entry_fill_price) / init_risk
                if pos.direction == "LONG"
                else (pos.entry_fill_price - ltp) / init_risk
            )

            hit_target = (ltp >= pos.target_price) if pos.direction == "LONG" else (ltp <= pos.target_price)
            if hit_target:
                self.close_position(pos.position_id, ltp, "TARGET_HIT", now_ist, paper_store, telegram_notifier, usdt_inr=self.last_usdt_inr, disable_kill_switch=disable_kill_switch)
                actions.append({"action": "TARGET_HIT", "symbol": pos.symbol, "net_pnl_usdt": str(pos.net_pnl_usdt)})
                continue

            hit_sl = (ltp <= pos.stop_loss) if pos.direction == "LONG" else (ltp >= pos.stop_loss)
            if hit_sl:
                reason = "TRAILING_SL_HIT" if pos.trailing_active else ("BREAKEVEN_SL_HIT" if pos.breakeven_shifted else "STOP_LOSS_HIT")
                self.close_position(pos.position_id, ltp, reason, now_ist, paper_store, telegram_notifier, usdt_inr=self.last_usdt_inr, disable_kill_switch=disable_kill_switch)
                actions.append({"action": reason, "symbol": pos.symbol, "net_pnl_usdt": str(pos.net_pnl_usdt)})
                continue

            # 1.15R Charge-Buffered Breakeven (prevents premature wick-outs at 1.0R; covers 0.20% round-trip fees)
            if not pos.breakeven_shifted and live_r >= Decimal("1.15"):
                buf = pos.entry_fill_price * Decimal("0.0020")
                be = _quant(pos.entry_fill_price + buf if pos.direction == "LONG" else pos.entry_fill_price - buf)
                if (pos.direction == "LONG" and be > pos.stop_loss) or (pos.direction == "SHORT" and be < pos.stop_loss):
                    pos.stop_loss = be
                    pos.breakeven_shifted = True
                    actions.append({"action": "BREAKEVEN_SHIFT", "symbol": pos.symbol, "new_sl": str(be)})

            # Regime-Adaptive Trailing SL: Disabled in SIDEWAYS_RANGE (clean fixed TP); 1.0x ATR-equivalent trail in TRENDING after 2.0R
            if (regime_mode or "").upper() != "SIDEWAYS_RANGE" and live_r >= Decimal("2.00"):
                trail = _quant(ltp - init_risk * Decimal("1.00") if pos.direction == "LONG" else ltp + init_risk * Decimal("1.00"))
                if (pos.direction == "LONG" and trail > pos.stop_loss) or (pos.direction == "SHORT" and trail < pos.stop_loss):
                    pos.stop_loss = trail
                    pos.trailing_active = True
                    actions.append({"action": "TRAILING_SL", "symbol": pos.symbol, "new_sl": str(trail)})

        return actions

    def account_ledger(self, live_ltps: Dict[str, Decimal], usdt_inr: Decimal = Decimal("85.20")) -> Dict[str, Any]:
        open_pos = [p for p in self.positions.values() if p.status == "OPEN"]
        closed_pos = [p for p in self.positions.values() if p.status == "CLOSED"]

        realised_net = sum((p.net_pnl_usdt for p in closed_pos), Decimal("0.00")).quantize(Decimal("0.01"))
        unrealised_net = Decimal("0.00")
        deployed_margin = Decimal("0.00")
        open_rows: List[Dict[str, Any]] = []

        for p in open_pos:
            ltp = live_ltps.get(p.symbol.upper()) or p.entry_fill_price
            est_exit_fee = (ltp * p.quantity * TAKER_FEE_RATE).quantize(Decimal("0.01"))
            gross = (
                ((ltp - p.entry_fill_price) * p.quantity)
                if p.direction == "LONG"
                else ((p.entry_fill_price - ltp) * p.quantity)
            ).quantize(Decimal("0.01"))
            mtm_net = (gross - p.entry_fee_usdt - est_exit_fee - p.funding_paid_usdt).quantize(Decimal("0.01"))
            unrealised_net += mtm_net
            deployed_margin += p.margin_usdt
            open_rows.append({
                "position_id": p.position_id,
                "symbol": p.symbol,
                "horizon_bucket": p.horizon_bucket,
                "hold_horizon": p.hold_horizon,
                "engine_code": p.engine_code,
                "direction": p.direction,
                "tier": p.tier,
                "leverage": str(p.leverage),
                "margin_usdt": str(p.margin_usdt),
                "quantity": str(p.quantity),
                "entry_fill_price": str(p.entry_fill_price),
                "current_ltp": str(_quant(ltp)),
                "stop_loss": str(p.stop_loss),
                "target_price": str(p.target_price),
                "fee_and_funding_usdt": str((p.entry_fee_usdt + est_exit_fee + p.funding_paid_usdt).quantize(Decimal("0.01"))),
                "unrealised_net_usdt": str(mtm_net),
                "unrealised_net_inr": str((mtm_net * usdt_inr).quantize(Decimal("0.01"))),
                "score": p.score,
                "bias_tag": p.bias_tag,
                "planned_risk_inr": str(p.planned_risk_inr),
                "rationale": p.rationale,
                "opened_at_ist": p.opened_at_ist,
            })

        total_net = (realised_net + unrealised_net).quantize(Decimal("0.01"))
        equity = (self.config.starting_usdt + total_net).quantize(Decimal("0.01"))
        free_usdt = max(Decimal("0.00"), (equity - deployed_margin).quantize(Decimal("0.01")))

        wins = [p for p in closed_pos if p.net_pnl_usdt > 0]
        losses = [p for p in closed_pos if p.net_pnl_usdt < 0]
        total_closed = len(closed_pos)
        wr = ((Decimal(str(len(wins))) / Decimal(str(total_closed))) * Decimal("100")).quantize(Decimal("0.1")) if total_closed > 0 else Decimal("0.0")
        gw = sum((p.net_pnl_usdt for p in wins), Decimal("0.00"))
        gl = abs(sum((p.net_pnl_usdt for p in losses), Decimal("0.00")))
        pf = (gw / gl).quantize(Decimal("0.01")) if gl > 0 else (Decimal("99.99") if gw > 0 else Decimal("0.00"))

        closed_rows = [
            {
                "position_id": p.position_id,
                "symbol": p.symbol,
                "horizon_bucket": p.horizon_bucket,
                "hold_horizon": p.hold_horizon,
                "engine_code": p.engine_code,
                "direction": p.direction,
                "tier": p.tier,
                "entry_fill_price": str(p.entry_fill_price),
                "exit_fill_price": str(p.exit_fill_price or "0"),
                "exit_reason": p.exit_reason,
                "net_pnl_usdt": str(p.net_pnl_usdt),
                "net_pnl_inr": str((p.net_pnl_usdt * usdt_inr).quantize(Decimal("0.01"))),
                "outcome": p.outcome,
                "closed_at_ist": p.closed_at_ist,
            }
            for p in sorted(closed_pos, key=lambda x: x.closed_at_ist or "", reverse=True)
        ]

        return {
            "usdt_inr_rate": str(usdt_inr),
            "starting_usdt": str(self.config.starting_usdt),
            "total_equity_usdt": str(equity),
            "total_equity_inr": str((equity * usdt_inr).quantize(Decimal("0.01"))),
            "available_usdt": str(free_usdt),
            "deployed_margin_usdt": str(deployed_margin.quantize(Decimal("0.01"))),
            "realised_net_usdt": str(realised_net),
            "unrealised_net_usdt": str(unrealised_net.quantize(Decimal("0.01"))),
            "total_net_usdt": str(total_net),
            "total_net_inr": str((total_net * usdt_inr).quantize(Decimal("0.01"))),
            "open_count": len(open_pos),
            "closed_count": total_closed,
            "wins": len(wins),
            "losses": len(losses),
            "accuracy_pct": str(wr),
            "profit_factor": str(pf),
            "open_positions_live": open_rows,
            "closed_trades": closed_rows,
        }
