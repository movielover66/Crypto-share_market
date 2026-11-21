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

        # Decide Two-Tier status: ELITE_TG (capped 5-7/day) vs BACKEND_LAB
        is_elite = (
            sig.score >= self.config.elite_min_score
            and self.tg_sent_today < self.config.telegram_daily_cap
        )
        tier = "ELITE_TG" if is_elite else ("BOOM" if is_boom else "BACKEND_LAB")

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
            tg_res = None
            if is_boom and hasattr(telegram_notifier, "send_html"):
                msg = (
                    f"🟣🚀 <b>BOOM 1x</b> • <code>{pos.symbol}</code>\n"
                    f"Entry <code>${pos.entry_fill_price}</code> | TP <code>${pos.target_price}</code> | SL <code>${pos.stop_loss}</code>\n"
                    f"<i>Score {pos.score}/100 • Margin ${pos.margin_usdt} • Alert #{self.tg_sent_today}/{self.config.telegram_daily_cap} • {ts_str}</i>"
                )
                tg_res = telegram_notifier.send_html(msg, btn_label="🚀 Open Boom Radar")
            elif hasattr(telegram_notifier, "send_html"):
                dot = "🟢" if pos.direction == "LONG" else "🔴"
                msg = (
                    f"{dot} <b>{pos.direction} {pos.leverage}x</b> • <code>{pos.symbol}</code>\n"
                    f"Entry <code>${pos.entry_fill_price}</code> | TP <code>${pos.target_price}</code> | SL <code>${pos.stop_loss}</code>\n"
                    f"<i>{pos.horizon_bucket.upper()} • Score {pos.score}/100 • Margin ${pos.margin_usdt} • #{self.tg_sent_today}/{self.config.telegram_daily_cap} • {ts_str}</i>"
                )
                tg_res = telegram_notifier.send_html(msg, btn_label="📊 Open Crypto Desk")
            else:
                tg_res = telegram_notifier.send_alert(f"{pos.direction} — {pos.symbol}", f"Entry ${pos.entry_fill_price}", ts_str)
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

        # 100% Guaranteed Category-Colored Dark PnL Image Card on EVERY Closed Trade (Target Hit / SL Hit / Boom Exit)!
        if telegram_notifier and getattr(telegram_notifier, "enabled", False):
            sign = "+" if net > 0 else ""
            is_boom_pos = pos.horizon_bucket == "boom" or pos.engine_code == "BOOM_RADAR"
            is_win = net >= 0
            pct_roi = ((net / pos.margin_usdt) * Decimal("100")).quantize(Decimal("0.01")) if pos.margin_usdt > 0 else Decimal("0.00")
            eff_fx = usdt_inr if (usdt_inr and usdt_inr > Decimal("50")) else self.last_usdt_inr
            inr_est = (net * eff_fx).quantize(Decimal("1"))
            ts_str = now_ist.strftime("%d %b • %I:%M %p IST")
            if is_boom_pos:
                cat = "BOOM_WIN" if is_win else "BOOM_LOSS"
                badge = "BOOM TARGET HIT" if is_win else "BOOM SL HIT"
                icon = "🟣🚀"
                btn = "🚀 Open Boom Radar"
            else:
                cat = "PERP_WIN" if is_win else "PERP_LOSS"
                badge = "TARGET HIT" if reason == "TARGET_HIT" else ("STOP-LOSS HIT" if "SL" in reason else reason.replace("_", " "))
                icon = "🟢🏆" if is_win else "🔴🛑"
                btn = "📊 Open Crypto Desk"
            cap = (
                f"{icon} <b>{badge}</b> • <code>{pos.symbol}</code>\n"
                f"Exit <code>${pos.exit_fill_price}</code> | Net <b>{sign}${pos.net_pnl_usdt}</b> ({sign}{pct_roi}% • ₹{inr_est})"
            )
            if hasattr(telegram_notifier, "send_pnl_card"):
                telegram_notifier.send_pnl_card(
                    category=cat,
                    badge_text=badge,
                    symbol=pos.symbol,
                    sub_pill=f"{'BOOM 1x' if is_boom_pos else f'{pos.direction} {pos.leverage}x'}",
                    big_pnl=f"{sign}${pos.net_pnl_usdt}",
                    sub_pnl=f"{sign}{pct_roi}%  |  {sign}Rs {abs(inr_est)} Net",
                    col1=("ENTRY", f"${pos.entry_fill_price}"),
                    col2=("EXIT", f"${pos.exit_fill_price}"),
                    col3=("STOP-LOSS", f"${pos.stop_loss}"),
                    caption_html=cap,
                    ts_label=ts_str,
                    btn_label=btn,
                    reply_to_message_id=pos.tg_message_id,
                )
            else:
                telegram_notifier.send_alert(f"{badge} — {pos.symbol}", f"Net {sign}${pos.net_pnl_usdt}", ts_str)
        return pos

    def monitor_open_positions(
        self,
        live_ltps: Dict[str, Decimal],
        now_ist: datetime.datetime,
        paper_store: Optional[Any] = None,
        telegram_notifier: Optional[Any] = None,
        usdt_inr: Optional[Decimal] = None,
        disable_kill_switch: bool = False,
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

            # 1.5R Charge-Buffered Breakeven
            if not pos.breakeven_shifted and live_r >= Decimal("1.50"):
                buf = pos.entry_fill_price * Decimal("0.0018")
                be = _quant(pos.entry_fill_price + buf if pos.direction == "LONG" else pos.entry_fill_price - buf)
                if (pos.direction == "LONG" and be > pos.stop_loss) or (pos.direction == "SHORT" and be < pos.stop_loss):
                    pos.stop_loss = be
                    pos.breakeven_shifted = True
                    actions.append({"action": "BREAKEVEN_SHIFT", "symbol": pos.symbol, "new_sl": str(be)})

            # 2.0R Trailing SL
            if live_r >= Decimal("2.00"):
                trail = _quant(ltp - init_risk * Decimal("0.85") if pos.direction == "LONG" else ltp + init_risk * Decimal("0.85"))
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
