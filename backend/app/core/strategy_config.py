"""
TRADEIQ Crypto Pro v2.0 — Centralized Strategy & Risk Configuration (`StrategyConfig`).
Single source of truth for all tunable parameters across the 6-Layer Architecture.
Supports `unlimited_paper_mode=True` (`max_open_positions=0`, `max_per_direction=0`, `max_boom_open=0`)
so paper accuracy can be tested at full speed while keeping all 6 quality gates active, and can be
dialed back down to 2/3/4/5 at any time from the UI or API.
"""
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict


@dataclass
class StrategyConfig:
    # Paper-Lab Throughput Controls (0 = Unlimited; set 2, 3, 4, 5... anytime from UI/API)
    unlimited_paper_mode: bool = True
    max_open_positions: int = 0          # 0 = Unlimited when unlimited_paper_mode=True
    max_per_direction: int = 0           # 0 = Unlimited
    max_boom_open: int = 0               # 0 = Unlimited
    max_daily_trades: int = 0            # 0 = Unlimited

    # Layer 3: Free LuxAlgo-Inspired SMC Filter (FVG + Order Block + Premium/Discount Zone)
    smc_filter_enabled: bool = True

    # Layer 4: Soft Bias Blending (No Hard Lock on A+ Counter-Trend Setups)
    soft_bias_enabled: bool = True
    counter_trend_min_score: int = 90
    counter_trend_risk_multiplier: Decimal = Decimal("0.50")
    counter_trend_min_abs_cvd: float = 15.0

    # Layer 4: Equal-Risk Volatility-Targeted Position Sizing
    equal_risk_enabled: bool = True
    risk_per_trade_inr: Decimal = Decimal("500.00")
    min_margin_usdt: Decimal = Decimal("25.00")
    max_margin_usdt: Decimal = Decimal("400.00")

    # Layer 5: Risk Firewall
    sl_cooldown_hours: int = 6
    disable_kill_switch_in_unlimited: bool = True

    # Optional External Webhook Receiver Secret
    webhook_secret: str = ""

    def __post_init__(self) -> None:
        if not self.webhook_secret:
            self.webhook_secret = os.getenv("TRADEIQ_WEBHOOK_SECRET", "").strip()

    def update(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        if "unlimited_paper_mode" in updates and updates["unlimited_paper_mode"] is not None:
            self.unlimited_paper_mode = bool(updates["unlimited_paper_mode"])
            if self.unlimited_paper_mode:
                if "max_open_positions" not in updates:
                    self.max_open_positions = 0
                if "max_per_direction" not in updates:
                    self.max_per_direction = 0
                if "max_boom_open" not in updates:
                    self.max_boom_open = 0
                if "max_daily_trades" not in updates:
                    self.max_daily_trades = 0
            else:
                if self.max_open_positions == 0 and "max_open_positions" not in updates:
                    self.max_open_positions = 5
                if self.max_per_direction == 0 and "max_per_direction" not in updates:
                    self.max_per_direction = 3
                if self.max_boom_open == 0 and "max_boom_open" not in updates:
                    self.max_boom_open = 2

        if "max_open_positions" in updates and updates["max_open_positions"] is not None:
            v = max(0, min(200, int(updates["max_open_positions"])))
            self.max_open_positions = v
            if v > 0:
                self.unlimited_paper_mode = False
        if "max_per_direction" in updates and updates["max_per_direction"] is not None:
            self.max_per_direction = max(0, min(100, int(updates["max_per_direction"])))
        if "max_boom_open" in updates and updates["max_boom_open"] is not None:
            self.max_boom_open = max(0, min(50, int(updates["max_boom_open"])))
        if "max_daily_trades" in updates and updates["max_daily_trades"] is not None:
            self.max_daily_trades = max(0, min(500, int(updates["max_daily_trades"])))

        if "smc_filter_enabled" in updates and updates["smc_filter_enabled"] is not None:
            self.smc_filter_enabled = bool(updates["smc_filter_enabled"])
        if "soft_bias_enabled" in updates and updates["soft_bias_enabled"] is not None:
            self.soft_bias_enabled = bool(updates["soft_bias_enabled"])
        if "counter_trend_min_score" in updates and updates["counter_trend_min_score"] is not None:
            self.counter_trend_min_score = max(75, min(98, int(updates["counter_trend_min_score"])))
        if "counter_trend_risk_multiplier" in updates and updates["counter_trend_risk_multiplier"] is not None:
            self.counter_trend_risk_multiplier = max(
                Decimal("0.20"), min(Decimal("1.00"), Decimal(str(updates["counter_trend_risk_multiplier"])))
            ).quantize(Decimal("0.05"))
        if "equal_risk_enabled" in updates and updates["equal_risk_enabled"] is not None:
            self.equal_risk_enabled = bool(updates["equal_risk_enabled"])
        if "risk_per_trade_inr" in updates and updates["risk_per_trade_inr"] is not None:
            self.risk_per_trade_inr = max(
                Decimal("100.00"), min(Decimal("5000.00"), Decimal(str(updates["risk_per_trade_inr"])))
            ).quantize(Decimal("0.01"))
        if "sl_cooldown_hours" in updates and updates["sl_cooldown_hours"] is not None:
            self.sl_cooldown_hours = max(0, min(48, int(updates["sl_cooldown_hours"])))
        if "disable_kill_switch_in_unlimited" in updates and updates["disable_kill_switch_in_unlimited"] is not None:
            self.disable_kill_switch_in_unlimited = bool(updates["disable_kill_switch_in_unlimited"])
        return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unlimited_paper_mode": self.unlimited_paper_mode,
            "max_open_positions": self.max_open_positions,
            "max_per_direction": self.max_per_direction,
            "max_boom_open": self.max_boom_open,
            "max_daily_trades": self.max_daily_trades,
            "smc_filter_enabled": self.smc_filter_enabled,
            "soft_bias_enabled": self.soft_bias_enabled,
            "counter_trend_min_score": self.counter_trend_min_score,
            "counter_trend_risk_multiplier": str(self.counter_trend_risk_multiplier),
            "counter_trend_min_abs_cvd": self.counter_trend_min_abs_cvd,
            "equal_risk_enabled": self.equal_risk_enabled,
            "risk_per_trade_inr": str(self.risk_per_trade_inr),
            "min_margin_usdt": str(self.min_margin_usdt),
            "max_margin_usdt": str(self.max_margin_usdt),
            "sl_cooldown_hours": self.sl_cooldown_hours,
            "disable_kill_switch_in_unlimited": self.disable_kill_switch_in_unlimited,
            "webhook_configured": bool(self.webhook_secret),
        }
