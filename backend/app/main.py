"""
TRADEIQ Crypto v1.0 — 24/7 Autonomous USDT-M Perpetual + Institutional Spot Buy Vault Platform.
- 3-Tier Free Public Exchange Failover: OKX v5 Primary -> Bybit v5 -> Kraken v2
- 5-Engine Quant Suite + Daily Boom Radar (1x Micro-Size) + Macro Catalyst Radar + Institutional Spot Buy Vault (No-SL, 3-Step Smart DCA)
- Two-Tier Pipeline: Full Backend Accuracy Lab vs Top 5-7/Day Telegram Elite Alerts
- Permanent Supabase Auto-Persistence (`market="CRYPTO_V1"` & `market="CRYPTO_SPOT"`): survives every code push & Render restart
- Single-Owner JWT Authentication + Live WebSocket (/api/v1/ws/live)
"""
import asyncio
import datetime
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .core.config_auth import (
    get_settings,
    issue_owner_token,
    require_owner,
    verify_owner_password,
    verify_owner_token,
)
from .domain.crypto_engines import (
    CryptoSignal,
    evaluate_boom_radar,
    evaluate_btc_regime,
    evaluate_engine_1_breakout,
    evaluate_engine_2_donchian,
    evaluate_engine_3_funding_rsi2,
    evaluate_engine_4_shadow_sweep,
    evaluate_engine_5_crash_short,
)
from .domain.crypto_simulator import CryptoPaperEngine, IST
from .core.strategy_config import StrategyConfig
from .domain.market_brain import MarketBrainEngine
from .domain.portfolio_manager import (
    build_engine_scorecard,
    check_risk_firewall,
    evaluate_soft_bias,
)
from .domain.chart_reader import evaluate_12pt_chart_read
from .domain.smc_filter import evaluate_smc_context
from .services.coin_verifier import CoinFundamentalVerifier
from .domain.spot_vault import (
    BLUECHIP_SPOT_UNIVERSE,
    SpotVaultEngine,
    evaluate_spot_bounce,
)
from .providers.crypto_provider import (
    BOOM_CRYPTO_UNIVERSE,
    CORE_CRYPTO_UNIVERSE,
    MultiExchangeCryptoProvider,
)
from .repositories.crypto_supabase_store import CryptoSupabaseStore
from .services.crypto_news import CryptoCatalystNewsService, CryptoTelegramNotifier, GeminiTrapJudge

logger = logging.getLogger("tradeiq_crypto")
STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


class LoginRequest(BaseModel):
    password: str


class ConfigUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    starting_usdt: Optional[float] = None
    per_trade_usdt: Optional[float] = None
    max_leverage: Optional[float] = None
    max_open_positions: Optional[int] = None
    telegram_daily_cap: Optional[int] = None
    enable_boom_radar: Optional[bool] = None
    enable_catalyst_radar: Optional[bool] = None
    reset_kill_switch: Optional[bool] = False


class SpotConfigUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    starting_usdt: Optional[float] = None
    per_coin_budget_usdt: Optional[float] = None
    target_net_pct: Optional[float] = None
    max_active_coins: Optional[int] = None


class SpotBuyRequest(BaseModel):
    symbol: str
    budget_usdt: Optional[float] = None
    target_net_pct: Optional[float] = None
    single_shot: bool = False


class ScanRequest(BaseModel):
    execute_entries: bool = True


class StrategyConfigUpdateRequest(BaseModel):
    unlimited_paper_mode: Optional[bool] = None
    max_open_positions: Optional[int] = None
    max_per_direction: Optional[int] = None
    max_boom_open: Optional[int] = None
    max_daily_trades: Optional[int] = None
    smc_filter_enabled: Optional[bool] = None
    soft_bias_enabled: Optional[bool] = None
    counter_trend_min_score: Optional[int] = None
    counter_trend_risk_multiplier: Optional[float] = None
    equal_risk_enabled: Optional[bool] = None
    risk_per_trade_inr: Optional[float] = None
    sl_cooldown_hours: Optional[int] = None
    disable_kill_switch_in_unlimited: Optional[bool] = None


class ExternalWebhookSignalRequest(BaseModel):
    symbol: str
    direction: str = "LONG"
    score: int = 88
    stop_loss_pct: float = 2.2
    target_pct: float = 5.5
    source: str = "TRADINGVIEW_WEBHOOK"
    secret: Optional[str] = None



def _run_brain_deep(app: FastAPI) -> Dict[str, Any]:
    prov: MultiExchangeCryptoProvider = app.state.provider
    brain: MarketBrainEngine = app.state.brain
    news_svc: CryptoCatalystNewsService = app.state.news_svc
    tg: CryptoTelegramNotifier = app.state.tg

    now_ist = datetime.datetime.now(IST)
    tickers = prov.refresh_all_tickers()
    news_items = news_svc.refresh_news()
    btc_15 = prov.get_candles("BTCUSDT", "15m", 35)
    btc_1h = prov.get_candles("BTCUSDT", "1h", 48)
    btc_4h = prov.get_candles("BTCUSDT", "4h", 60)
    eth_1h = prov.get_candles("ETHUSDT", "1h", 35)
    btc_depth = prov.get_orderbook_depth("BTCUSDT")
    btc_fp = prov.get_footprint_cvd("BTCUSDT")
    eth_fp = prov.get_footprint_cvd("ETHUSDT")
    d = brain.evaluate_deep(
        tickers=tickers, btc_15m=btc_15, btc_1h=btc_1h, btc_4h=btc_4h, eth_1h=eth_1h,
        btc_depth=btc_depth, btc_fp=btc_fp, eth_fp=eth_fp, news_items=news_items,
        now_ist=now_ist, tg=tg,
    )
    return d.to_dict()


def _run_spot_vault_cycle(app: FastAPI, execute_entries: bool = True) -> Dict[str, Any]:
    prov: MultiExchangeCryptoProvider = app.state.provider
    vault: SpotVaultEngine = app.state.spot_vault
    store: CryptoSupabaseStore = app.state.store
    news_svc: CryptoCatalystNewsService = app.state.news_svc
    tg: CryptoTelegramNotifier = app.state.tg

    now_ist = datetime.datetime.now(IST)
    vault.last_scan_ist = now_ist.isoformat()
    tickers = prov.refresh_all_tickers()
    usdt_inr = prov.get_usdt_inr()
    news_items = news_svc.refresh_news()
    ltp_map = {s: t.ltp for s, t in tickers.items()}

    risk_blocked: set = set()
    for n in news_items[:12]:
        if n.get("impact") == "HIGH_RISK":
            for c in n.get("mapped_coins") or []:
                risk_blocked.add(c.upper())

    open_spot_syms = {p.symbol for p in vault.positions.values() if p.status == "OPEN"}
    footprints: Dict[str, Dict[str, Any]] = {}
    for sym in open_spot_syms:
        footprints[sym] = prov.get_footprint_cvd(sym)
    monitor_actions = vault.monitor_open_positions(ltp_map, footprints, now_ist, store, tg, usdt_inr)

    brain_dir = app.state.brain.current_directive
    spot_dir = brain_dir.spot_directive if brain_dir else "QUICK_BOUNCE"
    strat_cfg: StrategyConfig = app.state.strategy_cfg

    radar_rows: List[Dict[str, Any]] = []
    for sym in BLUECHIP_SPOT_UNIVERSE:
        c15 = prov.get_candles(sym, "15m", 35)
        c1h = prov.get_candles(sym, "1h", 30)
        depth = prov.get_orderbook_depth(sym)
        fp = footprints.get(sym) or prov.get_footprint_cvd(sym)
        row = evaluate_spot_bounce(sym, c15, c1h, tickers.get(sym), depth, fp, risk_blocked)
        if strat_cfg.smc_filter_enabled and c1h:
            smc = evaluate_smc_context(c1h, direction="LONG")
            row["smc_zone"] = smc.zone
            row["smc_tags"] = smc.tags
            if smc.zone == "DISCOUNT":
                row["score"] = min(100, int(row["score"]) + 5)
                row["passed_rules"] = list(row["passed_rules"]) + [f"SMC Discount Zone ({smc.range_pos_pct:.0f}%)"]
        row["brain_spot_directive"] = spot_dir
        radar_rows.append(row)

    radar_rows.sort(key=lambda x: (1 if x["stage"] == "BUY_READY" else 0, x["score"]), reverse=True)
    vault.last_radar = radar_rows

    opened: List[Dict[str, Any]] = []
    # Bug #3 fix: Spot Vault obeys Market Brain spot_directive (WAIT_TOP pauses chasing green tops)
    allow_auto_spot = (spot_dir != "WAIT_TOP")
    if execute_entries and vault.config.enabled and allow_auto_spot:
        active_syms = {p.symbol for p in vault.positions.values() if p.status == "OPEN"}
        max_spot_coins = 10 if strat_cfg.unlimited_paper_mode else vault.config.max_active_coins
        for item in radar_rows:
            if len(active_syms) >= max_spot_coins:
                break
            sym = item["symbol"]
            if item["stage"] != "BUY_READY" or sym in active_syms:
                continue
            # In SEVERE_CRASH (DEEP_DCA_ONLY), require deeper dip >= -4.0% or SMC Discount Zone
            if spot_dir == "DEEP_DCA_ONLY":
                try:
                    dip_val = Decimal(str(item.get("dip_from_high_pct", "0")))
                except Exception:
                    dip_val = Decimal("0")
                if dip_val > Decimal("-4.0") and item.get("smc_zone") != "DISCOUNT":
                    continue
            ltp = ltp_map.get(sym) or Decimal(item["ltp"])
            if ltp <= 0:
                continue
            pos = vault.open_spot_position(
                symbol=sym,
                ltp=ltp,
                now_ist=now_ist,
                mode="AUTO_DCA",
                rationale=f"4-Point Bounce ({item['score']}/100): " + " • ".join(item["passed_rules"][:3]),
                store=store,
                tg=tg,
                usdt_inr=usdt_inr,
                brain_context=(f"{brain_dir.mode.replace('_', ' ')} ({brain_dir.score:+d})" if brain_dir else ""),
            )
            active_syms.add(sym)
            opened.append({"position_id": pos.position_id, "symbol": sym, "avg_entry": str(pos.avg_entry_price)})

    return {
        "scan_ist": vault.last_scan_ist,
        "bluechips_scanned": len(radar_rows),
        "buy_ready_count": sum(1 for r in radar_rows if r["stage"] == "BUY_READY"),
        "opened_count": len(opened),
        "opened_trades": opened,
        "monitor_actions": monitor_actions,
    }


def _run_full_crypto_cycle(app: FastAPI, execute_entries: bool = True) -> Dict[str, Any]:
    prov: MultiExchangeCryptoProvider = app.state.provider
    eng: CryptoPaperEngine = app.state.engine
    store: CryptoSupabaseStore = app.state.store
    news_svc: CryptoCatalystNewsService = app.state.news_svc
    tg: CryptoTelegramNotifier = app.state.tg

    now_ist = datetime.datetime.now(IST)
    eng.last_scan_ist = now_ist.isoformat()
    tickers = prov.refresh_all_tickers()
    usdt_inr = prov.get_usdt_inr()
    news_items = news_svc.refresh_news()

    strat_cfg: StrategyConfig = app.state.strategy_cfg
    ltp_map = {s: t.ltp for s, t in tickers.items()}
    disable_ks = strat_cfg.unlimited_paper_mode and strat_cfg.disable_kill_switch_in_unlimited
    cur_regime = app.state.brain.current_directive.mode if app.state.brain.current_directive else "TRENDING"
    monitor_actions = eng.monitor_open_positions(
        ltp_map, now_ist, store, tg, usdt_inr=usdt_inr, disable_kill_switch=disable_ks, regime_mode=cur_regime
    )

    # Bug #1 fix: MarketBrainEngine is the SINGLE regime source of truth (no dual Engine 0 conflict)
    _run_brain_deep(app)
    brain_dir = app.state.brain.current_directive

    risk_blocked: set = set()
    bull_catalysts: Dict[str, str] = {}
    bear_macro_headline = ""
    for n in news_items[:12]:
        if n.get("impact") == "HIGH_RISK":
            if not bear_macro_headline:
                bear_macro_headline = f"{n.get('tag')}: {n.get('headline', '')[:90]}"
            for c in n.get("mapped_coins") or []:
                risk_blocked.add(c.upper())
        elif n.get("impact") == "BULLISH_CATALYST":
            for c in n.get("mapped_coins") or []:
                bull_catalysts.setdefault(c.upper(), f"{n.get('tag')}: {n.get('headline', '')[:90]}")

    signals: List[CryptoSignal] = []
    coin_verifier: CoinFundamentalVerifier = app.state.coin_verifier
    # Gate 1 (Stage 1 Fast Screener): 534+ OKX -> Top 15-20 Verified Pure-Crypto Coins (0% US Stocks / Junk)
    scan_syms, gate1_rejected = coin_verifier.select_verified_candidates(tickers, max_candidates=18)
    liquid_all = [
        t for t in tickers.values()
        if t.symbol.endswith("USDT") and t.ltp > 0 and coin_verifier.verify_symbol(t.symbol, t.volume_24h_usdt).verified
    ]
    if not liquid_all:
        liquid_all = [t for t in tickers.values() if t.symbol.endswith("USDT") and t.ltp > 0]
    trap_judge: GeminiTrapJudge = app.state.trap_judge
    btc_chg = tickers["BTCUSDT"].change_24h_pct if "BTCUSDT" in tickers else Decimal("0")
    for sym in scan_syms:
        c15 = prov.get_candles(sym, "15m", 40)
        c1h = prov.get_candles(sym, "1h", 35)
        c4h = prov.get_candles(sym, "4h", 80)
        depth = prov.get_orderbook_depth(sym)
        fp = prov.get_footprint_cvd(sym)
        crowd = prov.get_crowd_positioning(sym)
        sym_chg = tickers[sym].change_24h_pct if sym in tickers else Decimal("0")
        eff_chg = min(sym_chg, btc_chg) if sym_chg < 0 else max(sym_chg, btc_chg)
        if c15:
            s1 = evaluate_engine_1_breakout(sym, c15, depth, c1h=c1h, footprint=fp, change_24h_pct=eff_chg)
            signals.append(evaluate_engine_4_shadow_sweep(sym, c15, footprint=fp, crowd=crowd))
            if c1h and (btc_chg <= Decimal("-3.0") or (brain_dir and brain_dir.mode in ("DOWNTREND", "SEVERE_CRASH") and brain_dir.btc_chg_24h <= 0.0)):
                signals.append(evaluate_engine_5_crash_short(
                    sym, c15, c1h, depth, footprint=fp,
                    change_24h_pct=sym_chg, btc_chg_24h=btc_chg, macro_reason=bear_macro_headline,
                ))
            if eng.config.enable_catalyst_radar and sym in bull_catalysts and s1.direction == "LONG" and s1.score >= 70:
                s1 = CryptoSignal(
                    symbol=s1.symbol, engine_code="CATALYST_MOMENTUM", horizon_bucket="intraday",
                    hold_horizon="Catalyst Momentum • 2–8 hrs", direction="LONG",
                    status="CONFIRMED", score=min(96, s1.score + 12),
                    entry_price=s1.entry_price, stop_loss=s1.stop_loss, target_price=s1.target_price,
                    expected_net_rr=s1.expected_net_rr, recommended_leverage=Decimal("1.0"),
                    passed_rules=s1.passed_rules + ["Breaking Catalyst News + Vol Confirmed"],
                    failed_rules=s1.failed_rules,
                    rationale=f"{bull_catalysts[sym]} • {s1.rationale}",
                )
            signals.append(s1)
        if c4h:
            signals.append(evaluate_engine_2_donchian(sym, c4h))
            signals.append(evaluate_engine_3_funding_rsi2(sym, c4h, tickers.get(sym)))

    boom_candidates = sorted(
        [
            t for t in liquid_all
            if t.symbol not in ("BTCUSDT", "ETHUSDT", "BNBUSDT")
            and Decimal("4.0") <= t.change_24h_pct <= Decimal("30.0")
        ],
        key=lambda t: (t.change_24h_pct * (t.volume_24h_usdt / Decimal("1000000"))),
        reverse=True,
    )[:10]
    if not boom_candidates:
        boom_candidates = sorted(
            [t for t in liquid_all if t.symbol not in ("BTCUSDT", "ETHUSDT", "BNBUSDT")],
            key=lambda t: t.change_24h_pct,
            reverse=True,
        )[:10]

    boom_rows: List[Dict[str, Any]] = []
    for t_obj in boom_candidates:
        sym = t_obj.symbol
        c15 = prov.get_candles(sym, "15m", 30)
        depth = prov.get_orderbook_depth(sym)
        fp_b = prov.get_footprint_cvd(sym)
        b_item = evaluate_boom_radar(sym, c15, t_obj, depth, footprint=fp_b)
        boom_rows.append(b_item)
        if eng.config.enable_boom_radar and btc_chg > Decimal("-5.0") and (not brain_dir or brain_dir.allow_boom_long) and b_item["stage"] == "BOOM TRIGGERED" and sym not in risk_blocked:
            signals.append(CryptoSignal(
                symbol=sym, engine_code="BOOM_RADAR", horizon_bucket="boom",
                hold_horizon="Daily Boom Radar • 1x Micro-Size", direction="LONG",
                status="CONFIRMED", score=int(b_item["score"]),
                entry_price=Decimal(b_item["entry_price"]), stop_loss=Decimal(b_item["stop_loss"]),
                target_price=Decimal(b_item["target_price"]), expected_net_rr=Decimal("1.90"),
                recommended_leverage=Decimal("1.0"), passed_rules=b_item["checks"], failed_rules=[],
                rationale=f"Boom Radar 1x breakout on {sym}: 24h {b_item['change_24h_pct']}%, 15m Vol {b_item['rel_vol_15m']}x.",
            ))

    boom_rows.sort(key=lambda x: x["score"], reverse=True)
    eng.last_boom_items = boom_rows

    # Layer 3: Apply Free LuxAlgo-inspired SMC Filter (FVG / Order Block / Premium-Discount Zone)
    if strat_cfg.smc_filter_enabled:
        enriched_signals: List[CryptoSignal] = []
        smc_cache: Dict[str, Any] = {}
        for s in signals:
            ckey = f"{s.symbol}:{s.direction}"
            if ckey not in smc_cache:
                c_ref = prov.get_candles(s.symbol, "15m", 35)
                smc_cache[ckey] = evaluate_smc_context(c_ref, direction=s.direction)
            smc = smc_cache[ckey]
            adj_score = max(1, min(99, s.score + smc.score_delta))
            new_rules = list(s.passed_rules) + smc.tags
            enriched_signals.append(CryptoSignal(
                symbol=s.symbol, engine_code=s.engine_code, horizon_bucket=s.horizon_bucket,
                hold_horizon=s.hold_horizon, direction=s.direction, status=s.status,
                score=adj_score, entry_price=s.entry_price, stop_loss=s.stop_loss,
                target_price=s.target_price, expected_net_rr=s.expected_net_rr,
                recommended_leverage=s.recommended_leverage, passed_rules=new_rules,
                failed_rules=s.failed_rules,
                rationale=(f"[SMC {smc.zone}] " if smc.tags else "") + s.rationale,
            ))
        signals = enriched_signals

    signals.sort(key=lambda s: (1 if s.status == "CONFIRMED" else 0, s.score), reverse=True)
    eng.last_signals = [
        {
            "symbol": s.symbol, "engine_code": s.engine_code, "horizon_bucket": s.horizon_bucket,
            "hold_horizon": s.hold_horizon, "direction": s.direction, "status": s.status,
            "score": s.score, "entry_price": str(s.entry_price), "stop_loss": str(s.stop_loss),
            "target_price": str(s.target_price), "passed_rules": s.passed_rules,
            "failed_rules": s.failed_rules, "rationale": s.rationale,
        }
        for s in signals[:40]
    ]

    opened: List[Dict[str, Any]] = []
    can_trade = execute_entries and eng.config.enabled and (disable_ks or not eng.kill_switch_active)
    if can_trade:
        for sig in signals:
            if sig.status != "CONFIRMED":
                continue
            is_boom = sig.engine_code == "BOOM_RADAR"
            open_pos_list = [p for p in eng.positions.values() if p.status == "OPEN"]
            all_pos_list = list(eng.positions.values())

            # Layer 5: Risk Firewall (Duplicate Lock, 6h SL Cooldown, News Shield, Unlimited/Custom Caps)
            fw_ok, _fw_reason = check_risk_firewall(
                symbol=sig.symbol,
                direction=sig.direction,
                engine_code=sig.engine_code,
                is_boom=is_boom,
                open_positions=open_pos_list,
                all_positions=all_pos_list,
                risk_blocked_syms=risk_blocked,
                now_ist=now_ist,
                cfg=strat_cfg,
                fallback_max_open=eng.config.max_open_positions,
                funding_rate=(float(tickers[sig.symbol].funding_rate) if sig.symbol in tickers else None),
            )
            if not fw_ok:
                continue

            # Layer 4: Soft Bias Blending (No Hard Lock on A+ Counter-Trend Setups)
            fp_now = prov.get_footprint_cvd(sig.symbol)
            bias_dec = evaluate_soft_bias(
                direction=sig.direction,
                engine_code=sig.engine_code,
                score=sig.score,
                brain_dir=brain_dir,
                footprint=fp_now,
                cfg=strat_cfg,
            )
            if not bias_dec.allowed:
                continue

            # Gate 4 & 5A: Full 12-Point Professional Chart Reader (Closed Candles, Vol Profile, BOS/CHoCH, 50-Level L2 Walls)
            c15_now = prov.get_candles(sig.symbol, "15m", 40)
            c1h_now = prov.get_candles(sig.symbol, "1h", 35)
            c4h_now = prov.get_candles(sig.symbol, "4h", 40)
            depth_now = prov.get_orderbook_depth(sig.symbol)
            btc_1h_now = prov.get_candles("BTCUSDT", "1h", 30)
            cr = evaluate_12pt_chart_read(
                symbol=sig.symbol,
                direction=sig.direction,
                c15=c15_now,
                c1h=c1h_now,
                c4h=c4h_now,
                depth=depth_now,
                footprint=fp_now,
                ticker=tickers.get(sig.symbol),
                btc_1h=btc_1h_now,
                demo_mode=prov.demo_mode,
                regime_mode=(brain_dir.mode if brain_dir else "TRENDING"),
            )
            if not cr.passed:
                continue

            # Gate 5B: Zero-Bypass Gemini Vision Chart PNG + 12-Point Map Final Audit
            sym_chg_now = float(tickers[sig.symbol].change_24h_pct) if sig.symbol in tickers else 0.0
            verdict = trap_judge.evaluate_chart_vision(
                symbol=sig.symbol,
                direction=sig.direction,
                candles=c15_now,
                price_map=cr.price_map,
                features={
                    "symbol": sig.symbol, "direction": sig.direction, "engine_code": sig.engine_code,
                    "change_24h_pct": sym_chg_now, "footprint_delta_pct": fp_now.get("delta_pct", 0.0),
                    "cvd_bias": fp_now.get("cvd_bias", "NEUTRAL"), "score": sig.score,
                    "points_12pt": f"{cr.points_passed}/12", "macro_headline": bear_macro_headline,
                },
            )
            if verdict.get("verdict") == "VETO_TRAP":
                continue

            # Upgrade signal with Structural SL & Structural TP from 12-Point Chart Reader
            sig_exec = CryptoSignal(
                symbol=sig.symbol, engine_code=sig.engine_code, horizon_bucket=sig.horizon_bucket,
                hold_horizon=sig.hold_horizon, direction=sig.direction, status=sig.status,
                score=max(sig.score, cr.score_12pt),
                entry_price=sig.entry_price,
                stop_loss=cr.structural_sl if cr.structural_sl > 0 else sig.stop_loss,
                target_price=cr.structural_tp if cr.structural_tp > 0 else sig.target_price,
                expected_net_rr=sig.expected_net_rr,
                recommended_leverage=sig.recommended_leverage,
                passed_rules=list(sig.passed_rules) + [f"12-Pt Reader {cr.points_passed}/12"],
                failed_rules=sig.failed_rules,
                rationale=f"[12-Pt {cr.points_passed}/12 | POC ${cr.price_map.get('poc')}] {sig.rationale}",
            )

            # Gate 6: Execution with Structural SL + Equal-Risk ₹500 Volatility Sizing
            pos = eng.open_position(
                sig_exec,
                now_ist,
                paper_store=store,
                telegram_notifier=tg,
                usdt_inr=usdt_inr,
                strategy_cfg=strat_cfg,
                risk_multiplier=bias_dec.risk_multiplier,
                bias_tag=bias_dec.bias_tag,
                brain_context=(f"{brain_dir.mode.replace('_', ' ')} ({brain_dir.score:+d})" if brain_dir else ""),
            )
            opened.append({
                "position_id": pos.position_id,
                "symbol": pos.symbol,
                "tier": pos.tier,
                "engine": pos.engine_code,
                "bias_tag": pos.bias_tag,
                "planned_risk_inr": str(pos.planned_risk_inr),
            })

    return {
        "scan_ist": eng.last_scan_ist,
        "regime": brain_dir.mode if brain_dir else "SIDEWAYS_RANGE",
        "market_universe_screened": len(tickers),
        "dynamic_coins_deep_scanned": len(set(scan_syms) | {t.symbol for t in boom_candidates}),
        "signals_evaluated": len(signals),
        "confirmed_count": sum(1 for s in signals if s.status == "CONFIRMED"),
        "opened_count": len(opened),
        "opened_trades": opened,
        "monitor_actions": monitor_actions,
    }


async def _crypto_24_7_scheduler(app: FastAPI) -> None:
    await asyncio.sleep(5)
    tick_cnt = 0
    last_brain_min = -1
    last_scan_min = -1
    last_news_min = -1
    last_summary_date = ""
    while True:
        try:
            now_ist = datetime.datetime.now(IST)
            tick_cnt += 1
            # 1. Every 10s (Event-Driven Fast Pulse): live BTC/ETH + 534-coin breadth -> instant crash override!
            tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
            needs_deep, _ = await asyncio.to_thread(app.state.brain.fast_pulse, tickers, now_ist, app.state.tg)
            if needs_deep:
                await asyncio.to_thread(_run_brain_deep, app)

            # 2. Every 30s (every 3rd 10s tick): monitor open Perpetuals & Spot Vault holdings on live LTP
            if tick_cnt % 3 == 0:
                open_pos = [p for p in app.state.engine.positions.values() if p.status == "OPEN"]
                open_spot = [p for p in app.state.spot_vault.positions.values() if p.status == "OPEN"]
                if open_pos or open_spot:
                    ltps = {s: t.ltp for s, t in tickers.items()}
                    usdt_inr = app.state.provider.get_usdt_inr()
                    if open_pos:
                        dis_ks = app.state.strategy_cfg.unlimited_paper_mode and app.state.strategy_cfg.disable_kill_switch_in_unlimited
                        bg_regime = app.state.brain.current_directive.mode if app.state.brain.current_directive else "TRENDING"
                        await asyncio.to_thread(
                            app.state.engine.monitor_open_positions,
                            ltps, now_ist, app.state.store, app.state.tg, usdt_inr, dis_ks, bg_regime,
                        )
                    if open_spot:
                        fps = {
                            p.symbol: await asyncio.to_thread(app.state.provider.get_footprint_cvd, p.symbol)
                            for p in open_spot
                        }
                        await asyncio.to_thread(
                            app.state.spot_vault.monitor_open_positions, ltps, fps, now_ist, app.state.store, app.state.tg, usdt_inr
                        )

            # 3. Every 60s (1-Min Deep Brain Sync — zero 5-minute lag!)
            if last_brain_min != now_ist.minute:
                await asyncio.to_thread(_run_brain_deep, app)
                last_brain_min = now_ist.minute

            # 4. Every 5 min: full 534-coin Perpetual + Boom + Spot Vault entry scan
            if (now_ist.minute % 5 == 0) and last_scan_min != now_ist.minute:
                if app.state.engine.config.enabled:
                    await asyncio.to_thread(_run_full_crypto_cycle, app, True)
                if app.state.spot_vault.config.enabled:
                    await asyncio.to_thread(_run_spot_vault_cycle, app, True)
                last_scan_min = now_ist.minute

            # 5. Every 15 min: refresh macro/catalyst news
            if (now_ist.minute % 15 == 0) and last_news_min != now_ist.minute:
                await asyncio.to_thread(app.state.news_svc.refresh_news, True)
                last_news_min = now_ist.minute

            # 6. Daily midnight IST summary -> Telegram
            today = now_ist.strftime("%Y-%m-%d")
            if now_ist.hour == 0 and now_ist.minute <= 5 and last_summary_date != today:
                ltps = {s: t.ltp for s, t in tickers.items()}
                usdt_inr = app.state.provider.get_usdt_inr()
                led = app.state.engine.account_ledger(ltps, usdt_inr)
                sled = app.state.spot_vault.vault_ledger(ltps, usdt_inr)
                b_dir = app.state.brain.current_directive
                b_txt = f"{b_dir.mode} ({b_dir.score:+d}/100)" if b_dir else "SYNCING"
                if False:  # Muted extra non-trade messages per owner preference
                    app.state.tg.send_alert(
                    f"DAILY CRYPTO SUMMARY — {today}",
                    f"🧠 Market Brain: {b_txt}\n"
                    f"💼 Perp Equity: ${led['total_equity_usdt']} (₹{led['total_equity_inr']}) | Net: ${led['total_net_usdt']}\n"
                    f"🏆 Perp Accuracy: {led['accuracy_pct']}% ({led['wins']}W/{led['losses']}L) | PF {led['profit_factor']}\n"
                    f"🪙 Spot Vault: ${sled['total_equity_usdt']} | Realised: ${sled['realised_net_usdt']} | Open: {sled['open_count']}\n"
                    f"⭐ Elite TG Today: {app.state.engine.tg_sent_today}/{app.state.engine.config.telegram_daily_cap}",
                    now_ist.strftime("%d %b %Y, %I:%M %p IST"),
                )
                last_summary_date = today
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Crypto 24/7 scheduler warning: {type(exc).__name__}: {exc}")
        await asyncio.sleep(10)


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="TRADEIQ Crypto Pro v2.1", version="2.1.0")
    app.state.cfg = cfg
    app.state.provider = MultiExchangeCryptoProvider(demo_mode=cfg.demo_mode)
    app.state.strategy_cfg = StrategyConfig()
    app.state.engine = CryptoPaperEngine()
    app.state.spot_vault = SpotVaultEngine()
    app.state.brain = MarketBrainEngine(demo_mode=cfg.demo_mode)
    app.state.store = CryptoSupabaseStore(cfg.supabase_url, cfg.supabase_key)
    app.state.news_svc = CryptoCatalystNewsService(demo_mode=cfg.demo_mode)
    app.state.tg = CryptoTelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    app.state.trap_judge = GeminiTrapJudge(cfg.gemini_api_keys)
    app.state.coin_verifier = CoinFundamentalVerifier(demo_mode=cfg.demo_mode)
    app.state._scheduler_task = None

    @app.on_event("startup")
    async def on_startup():
        if app.state.store.is_configured():
            restored = await asyncio.to_thread(app.state.store.fetch_crypto_positions)
            for pos in restored:
                app.state.engine.positions[pos.position_id] = pos
            today_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d")
            app.state.engine.tg_sent_date = today_ist
            rst_after = getattr(app.state.engine, "tg_reset_after_ist", "") or ""
            try:
                import json as _json
                _rp = "/tmp/tradeiq_tg_reset.json"
                if not rst_after and os.path.exists(_rp):
                    _rd = _json.loads(open(_rp).read() or "{}")
                    if _rd.get("date") == today_ist and _rd.get("reset_after_ist"):
                        rst_after = _rd["reset_after_ist"]
                        app.state.engine.tg_reset_after_ist = rst_after
            except Exception:
                pass
            app.state.engine.tg_sent_today = min(
                app.state.engine.config.telegram_daily_cap,
                sum(
                    1 for p in app.state.engine.positions.values()
                    if p.tier == "ELITE_TG"
                    and (p.opened_at_ist or "").startswith(today_ist)
                    and (not rst_after or (p.opened_at_ist or "") >= rst_after)
                ),
            )
            spot_restored = await asyncio.to_thread(app.state.store.fetch_spot_positions)
            for spos in spot_restored:
                app.state.spot_vault.positions[spos.position_id] = spos
        await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        await asyncio.to_thread(app.state.news_svc.refresh_news)
        await asyncio.to_thread(_run_brain_deep, app)
        if cfg.app_env != "test":
            app.state._scheduler_task = asyncio.create_task(_crypto_24_7_scheduler(app))

    @app.on_event("shutdown")
    async def on_shutdown():
        t = getattr(app.state, "_scheduler_task", None)
        if t:
            t.cancel()

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "service": "tradeiq-crypto-v2", "paper_only": True, "active_exchange": app.state.provider.active_exchange}

    @app.post("/api/v1/auth/login")
    async def login(payload: LoginRequest):
        if not verify_owner_password(payload.password, cfg):
            raise HTTPException(status_code=401, detail="Invalid owner password")
        return {"access_token": issue_owner_token(cfg), "token_type": "bearer"}

    @app.get("/api/v1/crypto/overview")
    async def crypto_overview(_owner: str = Depends(require_owner)):
        prov = app.state.provider
        eng = app.state.engine
        vault = app.state.spot_vault
        tickers = await asyncio.to_thread(prov.refresh_all_tickers)
        usdt_inr = prov.get_usdt_inr()
        ltps = {s: t.ltp for s, t in tickers.items()}
        majors = [
            {
                "symbol": sym, "ltp": str(tickers[sym].ltp), "change_24h_pct": str(tickers[sym].change_24h_pct),
                "funding_pct": str((tickers[sym].funding_rate * Decimal("100")).quantize(Decimal("0.001"))),
                "open_interest": str(tickers[sym].open_interest),
            }
            for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT") if sym in tickers
        ]
        if not app.state.brain.current_directive:
            await asyncio.to_thread(_run_brain_deep, app)
        sled = vault.vault_ledger(ltps, usdt_inr)
        return {
            "active_exchange": prov.active_exchange,
            "usdt_inr": str(usdt_inr),
            "majors": majors,
            "regime": {
                "regime": app.state.brain.current_directive.mode if app.state.brain.current_directive else "SIDEWAYS_RANGE",
                "summary": app.state.brain.current_directive.primary_cause if app.state.brain.current_directive else "Master Market Brain active.",
                "btc_funding_pct": majors[0]["funding_pct"] if majors else "0.010",
            },
            "config": eng.config.to_dict(),
            "strategy_config": app.state.strategy_cfg.to_dict(),
            "scorecard": build_engine_scorecard(list(eng.positions.values()), list(vault.positions.values()), usdt_inr),
            "tg_sent_today": eng.tg_sent_today,
            "tg_daily_cap": eng.config.telegram_daily_cap,
            "kill_switch_active": eng.kill_switch_active,
            "kill_switch_reason": eng.kill_switch_reason,
            "last_scan_ist": eng.last_scan_ist,
            "scheduler_active": bool(app.state._scheduler_task),
            "ledger": eng.account_ledger(ltps, usdt_inr),
            "brain": app.state.brain.current_directive.to_dict() if app.state.brain.current_directive else None,
            "brain_history": app.state.brain.history[:10],
            "spot_summary": {
                "open_count": sled["open_count"],
                "realised_net_usdt": sled["realised_net_usdt"],
                "target_net_pct": str(vault.config.target_net_pct),
                "enabled": vault.config.enabled,
            },
            "boom_preview": eng.last_boom_items[:5],
            "recent_signals": eng.last_signals[:25],
        }


    @app.get("/api/v1/crypto/brain")
    async def crypto_brain(_owner: str = Depends(require_owner)):
        if not app.state.brain.current_directive:
            await asyncio.to_thread(_run_brain_deep, app)
        return {
            "directive": app.state.brain.current_directive.to_dict(),
            "history": app.state.brain.history[:15],
        }

    @app.post("/api/v1/crypto/brain/refresh")
    async def crypto_brain_refresh(_owner: str = Depends(require_owner)):
        d = await asyncio.to_thread(_run_brain_deep, app)
        return {"directive": d, "history": app.state.brain.history[:15]}

    @app.get("/api/v1/spot/overview")
    async def spot_overview(_owner: str = Depends(require_owner)):
        prov = app.state.provider
        vault = app.state.spot_vault
        if not vault.last_radar:
            await asyncio.to_thread(_run_spot_vault_cycle, app, False)
        tickers = await asyncio.to_thread(prov.refresh_all_tickers)
        usdt_inr = prov.get_usdt_inr()
        ltps = {s: t.ltp for s, t in tickers.items()}
        return {
            "usdt_inr": str(usdt_inr),
            "last_scan_ist": vault.last_scan_ist,
            "config": vault.config.to_dict(),
            "bluechips": list(BLUECHIP_SPOT_UNIVERSE),
            "ledger": vault.vault_ledger(ltps, usdt_inr),
            "radar": vault.last_radar,
        }

    @app.post("/api/v1/spot/scan")
    async def spot_scan(payload: ScanRequest, _owner: str = Depends(require_owner)):
        return await asyncio.to_thread(_run_spot_vault_cycle, app, payload.execute_entries)

    @app.put("/api/v1/spot/config")
    async def spot_update_config(payload: SpotConfigUpdateRequest, _owner: str = Depends(require_owner)):
        data = payload.model_dump(exclude_none=True) if hasattr(payload, "model_dump") else payload.dict(exclude_none=True)
        return {"config": app.state.spot_vault.update_config(data)}

    @app.post("/api/v1/spot/buy")
    async def spot_manual_buy(payload: SpotBuyRequest, _owner: str = Depends(require_owner)):
        sym = payload.symbol.strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        existing_open = [p for p in app.state.spot_vault.positions.values() if p.status == "OPEN" and p.symbol == sym]
        if existing_open:
            raise HTTPException(status_code=409, detail=f"Spot Vault already holds an open position for {sym}. Use DCA or Sell instead.")
        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        t_obj = tickers.get(sym)
        if not t_obj or t_obj.ltp <= 0:
            raise HTTPException(status_code=404, detail=f"Live price not found for {sym}")
        budget = Decimal(str(payload.budget_usdt)) if payload.budget_usdt else None
        tgt = Decimal(str(payload.target_net_pct)) if payload.target_net_pct else None
        pos = app.state.spot_vault.open_spot_position(
            symbol=sym,
            ltp=t_obj.ltp,
            now_ist=datetime.datetime.now(IST),
            mode="MANUAL_SPOT",
            budget_usdt=budget,
            target_net_pct=tgt,
            single_shot=payload.single_shot,
            rationale="Manual Spot Vault Buy • Zero Stop-Loss",
            store=app.state.store,
            tg=app.state.tg,
            usdt_inr=app.state.provider.get_usdt_inr(),
        )
        return {
            "opened": True,
            "position_id": pos.position_id,
            "symbol": pos.symbol,
            "avg_entry_price": str(pos.avg_entry_price),
            "net_target_price": str(pos.net_target_price),
            "tranches_filled": pos.tranches_filled,
        }

    @app.post("/api/v1/spot/positions/{position_id}/dca")
    async def spot_manual_dca(position_id: str, _owner: str = Depends(require_owner)):
        pos = app.state.spot_vault.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            raise HTTPException(status_code=404, detail="Open spot position not found")
        if pos.tranches_filled >= pos.max_tranches:
            raise HTTPException(status_code=400, detail="All DCA tranches already filled")
        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        ltp = tickers[pos.symbol].ltp if pos.symbol in tickers else pos.avg_entry_price
        updated = app.state.spot_vault.add_dca_tranche(
            position_id, ltp, datetime.datetime.now(IST), "MANUAL_DCA",
            app.state.store, app.state.tg, app.state.provider.get_usdt_inr(),
        )
        return {
            "updated": bool(updated),
            "tranches_filled": updated.tranches_filled if updated else pos.tranches_filled,
            "avg_entry_price": str(updated.avg_entry_price) if updated else str(pos.avg_entry_price),
            "net_target_price": str(updated.net_target_price) if updated else str(pos.net_target_price),
        }

    @app.post("/api/v1/spot/positions/{position_id}/sell")
    async def spot_manual_sell(position_id: str, _owner: str = Depends(require_owner)):
        pos = app.state.spot_vault.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            raise HTTPException(status_code=404, detail="Open spot position not found")
        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        ltp = tickers[pos.symbol].ltp if pos.symbol in tickers else pos.avg_entry_price
        closed = app.state.spot_vault.close_spot_position(
            position_id, ltp, "MANUAL_SPOT_SELL", datetime.datetime.now(IST),
            app.state.store, app.state.tg, app.state.provider.get_usdt_inr(),
        )
        return {"closed": True, "net_pnl_usdt": str(closed.net_pnl_usdt) if closed else "0.00"}

    @app.delete("/api/v1/spot/positions/{position_id}")
    async def spot_delete(position_id: str, _owner: str = Depends(require_owner)):
        app.state.spot_vault.positions.pop(position_id, None)
        db_res = await asyncio.to_thread(app.state.store.delete_position, position_id)
        return {"deleted": True, "position_id": position_id, "supabase": db_res}

    @app.post("/api/v1/crypto/scan")
    async def crypto_scan(payload: ScanRequest, _owner: str = Depends(require_owner)):
        return await asyncio.to_thread(_run_full_crypto_cycle, app, payload.execute_entries)

    @app.get("/api/v1/crypto/boom")
    async def crypto_boom(_owner: str = Depends(require_owner)):
        if not app.state.engine.last_boom_items:
            await asyncio.to_thread(_run_full_crypto_cycle, app, False)
        return {"items": app.state.engine.last_boom_items, "usdt_inr": str(app.state.provider.get_usdt_inr())}

    @app.get("/api/v1/crypto/catalysts")
    async def crypto_catalysts(_owner: str = Depends(require_owner)):
        items = await asyncio.to_thread(app.state.news_svc.refresh_news)
        return {"count": len(items), "items": items}

    @app.get("/api/v1/crypto/markets")
    async def crypto_markets(
        q: str = Query(""),
        sort: str = Query("volume"),
        limit: int = Query(100, ge=10, le=600),
        _owner: str = Depends(require_owner),
    ):
        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        ql = (q or "").strip().upper()
        rows = [t for t in tickers.values() if t.symbol.endswith("USDT") and (not ql or ql in t.symbol)]
        if sort == "gainers":
            rows.sort(key=lambda t: t.change_24h_pct, reverse=True)
        elif sort == "losers":
            rows.sort(key=lambda t: t.change_24h_pct)
        else:
            rows.sort(key=lambda t: t.volume_24h_usdt, reverse=True)
        return {
            "total_market_coins": len(tickers),
            "filtered_count": len(rows),
            "usdt_inr": str(app.state.provider.get_usdt_inr()),
            "items": [
                {
                    "symbol": t.symbol,
                    "ltp": str(t.ltp),
                    "change_24h_pct": str(t.change_24h_pct),
                    "volume_24h_usdt": str(t.volume_24h_usdt),
                    "funding_pct": str((t.funding_rate * Decimal("100")).quantize(Decimal("0.001"))),
                }
                for t in rows[:limit]
            ],
        }

    @app.get("/api/v1/crypto/chart/{symbol}")
    async def crypto_chart(symbol: str, interval: str = Query("1m"), direction: str = Query("LONG"), limit: int = Query(300, ge=30, le=300), before_ms: Optional[int] = Query(None), _owner: str = Depends(require_owner)):
        sym = symbol.upper()
        candles = await asyncio.to_thread(app.state.provider.get_candles, sym, interval, limit, before_ms)
        if before_ms is not None:
            return {
                "symbol": sym,
                "interval": interval,
                "candles": [{"ts_ms": c.ts_ms, "o": str(c.open), "h": str(c.high), "l": str(c.low), "c": str(c.close), "v": str(c.volume)} for c in candles],
            }
        depth = await asyncio.to_thread(app.state.provider.get_orderbook_depth, sym)
        fp = await asyncio.to_thread(app.state.provider.get_footprint_cvd, sym)
        t_obj = app.state.provider.ticker_cache.get(sym)
        c15 = candles if interval == "15m" else await asyncio.to_thread(app.state.provider.get_candles, sym, "15m", 40)
        c1h = await asyncio.to_thread(app.state.provider.get_candles, sym, "1h", 35)
        c4h = await asyncio.to_thread(app.state.provider.get_candles, sym, "4h", 35)
        btc_1h = await asyncio.to_thread(app.state.provider.get_candles, "BTCUSDT", "1h", 30)
        cr_long = evaluate_12pt_chart_read(sym, direction, c15, c1h, c4h, depth, fp, t_obj, btc_1h, app.state.provider.demo_mode)
        cver = app.state.coin_verifier.verify_symbol(sym, t_obj.volume_24h_usdt if t_obj else Decimal("0"))
        return {
            "symbol": sym,
            "interval": interval,
            "ltp": str(t_obj.ltp) if t_obj else (str(candles[-1].close) if candles else "0"),
            "change_24h_pct": str(t_obj.change_24h_pct) if t_obj else "0.00",
            "candles": [{"ts_ms": c.ts_ms, "o": str(c.open), "h": str(c.high), "l": str(c.low), "c": str(c.close), "v": str(c.volume)} for c in candles],
            "depth": depth,
            "footprint": fp,
            "coin_verification": {"verified": cver.verified, "category": cver.category, "reason": cver.reason},
            "chart_read": cr_long.to_dict(),
        }

    @app.get("/api/v1/crypto/chart-read/{symbol}")
    async def crypto_chart_read(symbol: str, direction: str = Query("LONG"), _owner: str = Depends(require_owner)):
        sym = symbol.strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        t_obj = tickers.get(sym)
        cver = app.state.coin_verifier.verify_symbol(sym, t_obj.volume_24h_usdt if t_obj else Decimal("0"))
        c15 = await asyncio.to_thread(app.state.provider.get_candles, sym, "15m", 40)
        c1h = await asyncio.to_thread(app.state.provider.get_candles, sym, "1h", 35)
        c4h = await asyncio.to_thread(app.state.provider.get_candles, sym, "4h", 35)
        btc_1h = await asyncio.to_thread(app.state.provider.get_candles, "BTCUSDT", "1h", 30)
        depth = await asyncio.to_thread(app.state.provider.get_orderbook_depth, sym)
        fp = await asyncio.to_thread(app.state.provider.get_footprint_cvd, sym)
        cr = evaluate_12pt_chart_read(sym, direction, c15, c1h, c4h, depth, fp, t_obj, btc_1h, app.state.provider.demo_mode)
        return {
            "coin_verification": {"verified": cver.verified, "category": cver.category, "reason": cver.reason},
            "chart_read": cr.to_dict(),
        }

    @app.put("/api/v1/crypto/config")
    async def crypto_update_config(payload: ConfigUpdateRequest, _owner: str = Depends(require_owner)):
        data = payload.model_dump(exclude_none=True) if hasattr(payload, "model_dump") else payload.dict(exclude_none=True)
        if "max_open_positions" in data:
            app.state.strategy_cfg.update({"max_open_positions": data["max_open_positions"]})
        return {"config": app.state.engine.update_config(data), "strategy_config": app.state.strategy_cfg.to_dict()}

    @app.get("/api/v1/crypto/strategy-config")
    async def get_strategy_config(_owner: str = Depends(require_owner)):
        return {"strategy_config": app.state.strategy_cfg.to_dict()}

    @app.put("/api/v1/crypto/strategy-config")
    async def update_strategy_config(payload: StrategyConfigUpdateRequest, _owner: str = Depends(require_owner)):
        data = payload.model_dump(exclude_none=True) if hasattr(payload, "model_dump") else payload.dict(exclude_none=True)
        updated = app.state.strategy_cfg.update(data)
        if "max_open_positions" in data:
            app.state.engine.config.max_open_positions = app.state.strategy_cfg.max_open_positions
        if app.state.strategy_cfg.unlimited_paper_mode and app.state.strategy_cfg.disable_kill_switch_in_unlimited:
            app.state.engine.kill_switch_active = False
            app.state.engine.kill_switch_reason = None
        return {"strategy_config": updated}

    @app.get("/api/v1/crypto/scorecard")
    async def crypto_scorecard(_owner: str = Depends(require_owner)):
        usdt_inr = app.state.provider.get_usdt_inr()
        return build_engine_scorecard(
            list(app.state.engine.positions.values()),
            list(app.state.spot_vault.positions.values()),
            usdt_inr,
        )

    @app.post("/api/v1/webhooks/signal")
    async def external_webhook_signal(payload: ExternalWebhookSignalRequest):
        strat_cfg: StrategyConfig = app.state.strategy_cfg
        if strat_cfg.webhook_secret and (payload.secret or "").strip() != strat_cfg.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
        sym = payload.symbol.strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        direction = payload.direction.strip().upper()
        if direction not in ("LONG", "SHORT"):
            raise HTTPException(status_code=400, detail="direction must be LONG or SHORT")
        cver = app.state.coin_verifier.verify_symbol(sym, Decimal("50000000"))
        if not cver.verified:
            return {"accepted": False, "reason": cver.reason}

        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        t_obj = tickers.get(sym)
        if not t_obj or t_obj.ltp <= 0:
            raise HTTPException(status_code=404, detail=f"Live price unavailable for {sym}")

        ltp = t_obj.ltp
        sl_pct = Decimal(str(max(0.5, min(12.0, payload.stop_loss_pct)))) / Decimal("100")
        tp_pct = Decimal(str(max(1.0, min(30.0, payload.target_pct)))) / Decimal("100")
        sl = (ltp * (Decimal("1") - sl_pct) if direction == "LONG" else ltp * (Decimal("1") + sl_pct)).quantize(Decimal("0.0001"))
        tp = (ltp * (Decimal("1") + tp_pct) if direction == "LONG" else ltp * (Decimal("1") - tp_pct)).quantize(Decimal("0.0001"))
        now_ist = datetime.datetime.now(IST)
        eng = app.state.engine

        fw_ok, fw_reason = check_risk_firewall(
            symbol=sym, direction=direction, engine_code="WEBHOOK_EXTERNAL", is_boom=False,
            open_positions=[p for p in eng.positions.values() if p.status == "OPEN"],
            all_positions=list(eng.positions.values()), risk_blocked_syms=set(),
            now_ist=now_ist, cfg=strat_cfg, fallback_max_open=eng.config.max_open_positions,
        )
        if not fw_ok:
            return {"accepted": False, "reason": fw_reason}

        fp_now = await asyncio.to_thread(app.state.provider.get_footprint_cvd, sym)
        bias_dec = evaluate_soft_bias(
            direction=direction, engine_code="WEBHOOK_EXTERNAL", score=payload.score,
            brain_dir=app.state.brain.current_directive, footprint=fp_now, cfg=strat_cfg,
        )
        if not bias_dec.allowed:
            return {"accepted": False, "reason": bias_dec.reason}

        sig = CryptoSignal(
            symbol=sym, engine_code="WEBHOOK_EXTERNAL", horizon_bucket="intraday",
            hold_horizon="External Webhook • Paper Exec", direction=direction, status="CONFIRMED",
            score=max(1, min(99, payload.score)), entry_price=ltp, stop_loss=sl, target_price=tp,
            expected_net_rr=(tp_pct / sl_pct).quantize(Decimal("0.01")),
            recommended_leverage=Decimal("2.0"), passed_rules=[f"Source: {payload.source}", bias_dec.bias_tag],
            failed_rules=[], rationale=f"Webhook ({payload.source}) • {bias_dec.reason}",
        )
        pos = eng.open_position(
            sig, now_ist, paper_store=app.state.store, telegram_notifier=app.state.tg,
            usdt_inr=app.state.provider.get_usdt_inr(), strategy_cfg=strat_cfg,
            risk_multiplier=bias_dec.risk_multiplier, bias_tag=bias_dec.bias_tag,
        )
        return {
            "accepted": True,
            "position_id": pos.position_id,
            "symbol": pos.symbol,
            "direction": pos.direction,
            "bias_tag": pos.bias_tag,
            "planned_risk_inr": str(pos.planned_risk_inr),
            "margin_usdt": str(pos.margin_usdt),
        }

    @app.post("/api/v1/paper/positions/{position_id}/close")
    async def close_paper(position_id: str, _owner: str = Depends(require_owner)):
        pos = app.state.engine.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            raise HTTPException(status_code=404, detail="Open position not found")
        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        ltp = tickers[pos.symbol].ltp if pos.symbol in tickers else pos.entry_fill_price
        closed = app.state.engine.close_position(position_id, ltp, "MANUAL_CLOSE", datetime.datetime.now(IST), app.state.store, app.state.tg, usdt_inr=app.state.provider.get_usdt_inr())
        return {"closed": True, "net_pnl_usdt": str(closed.net_pnl_usdt) if closed else "0.00"}

    @app.delete("/api/v1/paper/positions/{position_id}")
    async def delete_paper(position_id: str, _owner: str = Depends(require_owner)):
        app.state.engine.positions.pop(position_id, None)
        db_res = await asyncio.to_thread(app.state.store.delete_position, position_id)
        return {"deleted": True, "position_id": position_id, "supabase": db_res}

    @app.get("/api/v1/system/connectivity")
    async def connectivity(_owner: str = Depends(require_owner)):
        prov = app.state.provider
        return {
            "active_exchange": prov.active_exchange,
            "services": [
                {"name": "Primary: OKX v5 Public API", **prov.exchange_health["okx"]},
                {"name": "Failover 1: Bybit v5 Public API", **prov.exchange_health["bybit"]},
                {"name": "Failover 2: Kraken US-Safe API", **prov.exchange_health["kraken"]},
                {"name": "Supabase Permanent Crypto Store", "ok": app.state.store.is_configured(), "detail": app.state.store.status_summary()["table"]},
                {"name": "Institutional Spot Buy Vault", "ok": True, "detail": f"3-Step Smart DCA • Net +{app.state.spot_vault.config.target_net_pct}% • Zero SL"},
                {"name": "Telegram Elite Bot (5–7/day)", "ok": app.state.tg.enabled, "detail": f"{app.state.engine.tg_sent_today}/{app.state.engine.config.telegram_daily_cap} sent today"},
                {"name": "OKX Free Tick Footprint & CVD", "ok": True, "detail": "Real-time Buy/Sell Delta + Shadow Sweep"},
                {"name": "Gemini Stop-Hunt Trap Judge", "ok": True, "detail": "Gemini AI + Deterministic Trap Veto Active" if app.state.trap_judge.enabled else "Deterministic Trap Veto Active (Add GEMINI_API_KEYS for AI)"},
                {"name": "24/7 Autonomous Scheduler", "ok": bool(app.state._scheduler_task) or cfg.app_env == "test", "detail": "5m Scan • 30s Live Guard"},
            ],
        }

    @app.post("/api/v1/telegram/reset-counter")
    async def reset_tg_counter(_owner: str = Depends(require_owner)):
        now_rst = datetime.datetime.now(IST)
        app.state.engine.tg_sent_today = 0
        app.state.engine.tg_sent_date = now_rst.strftime("%Y-%m-%d")
        app.state.engine.tg_reset_after_ist = now_rst.isoformat()
        try:
            import json as _json
            open("/tmp/tradeiq_tg_reset.json", "w").write(_json.dumps({
                "date": app.state.engine.tg_sent_date,
                "reset_after_ist": app.state.engine.tg_reset_after_ist,
            }))
        except Exception:
            pass
        return {
            "reset": True,
            "tg_sent_today": 0,
            "tg_daily_cap": app.state.engine.config.telegram_daily_cap,
        }

    @app.post("/api/v1/telegram/test")
    async def test_tg(_owner: str = Depends(require_owner)):
        return await asyncio.to_thread(
            app.state.tg.send_alert,
            "TEST CONNECTION",
            "TRADEIQ Crypto Pro v2.0 (6-Layer + Unlimited Paper Lab) is live and connected! (Paper Mode Only)",
            datetime.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST"),
        )

    @app.websocket("/api/v1/ws/live")
    async def ws_live(websocket: WebSocket, token: str = Query(""), max_frames: int = Query(0)):
        if not verify_owner_token(token, cfg):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        sent = 0
        try:
            while True:
                prov = app.state.provider
                eng = app.state.engine
                tickers = prov.ticker_cache or await asyncio.to_thread(prov.refresh_all_tickers)
                usdt_inr = prov.get_usdt_inr()
                ltps = {s: t.ltp for s, t in tickers.items()}
                majors = [
                    {"symbol": s, "ltp": str(tickers[s].ltp), "change_24h_pct": str(tickers[s].change_24h_pct)}
                    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT") if s in tickers
                ]
                vault = app.state.spot_vault
                sled = vault.vault_ledger(ltps, usdt_inr)
                await websocket.send_json({
                    "type": "LIVE_TICK",
                    "ts_ist": datetime.datetime.now(IST).strftime("%H:%M:%S IST"),
                    "active_exchange": prov.active_exchange,
                    "usdt_inr": str(usdt_inr),
                    "majors": majors,
                    "ledger": eng.account_ledger(ltps, usdt_inr),
                    "brain": app.state.brain.current_directive.to_dict() if app.state.brain.current_directive else None,
                    "spot_summary": {
                        "open_count": sled["open_count"],
                        "realised_net_usdt": sled["realised_net_usdt"],
                        "target_net_pct": str(vault.config.target_net_pct),
                        "enabled": vault.config.enabled,
                    },
                    "strategy_config": app.state.strategy_cfg.to_dict(),
                })
                sent += 1
                if max_frames > 0 and sent >= max_frames:
                    break
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception:
            try:
                await websocket.close()
            except Exception:
                pass

    @app.get("/", include_in_schema=False)
    async def serve_ui():
        return FileResponse(str(STATIC_INDEX))

    return app


app = create_app()
