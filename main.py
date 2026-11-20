"""
TRADEIQ Crypto v1.0 — 24/7 Autonomous USDT-M Perpetual Paper-Trading Platform.
- 3-Tier Free Public Exchange Failover: Bybit v5 -> OKX v5 -> Kraken v2
- 4-Engine Quant Suite + Daily Boom Radar (1x Micro-Size) + Macro Catalyst Radar (Musk/Trump/Fed/SEC)
- Two-Tier Pipeline: Full Backend Accuracy Lab vs Top 5-7/Day Telegram Elite Alerts
- Permanent Supabase Auto-Persistence (`market="CRYPTO"`): survives every code push & Render restart
- Single-Owner JWT Authentication + 500ms Live WebSocket (/api/v1/ws/live)
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
)
from .domain.crypto_simulator import CryptoPaperEngine, IST
from .providers.crypto_provider import (
    BOOM_CRYPTO_UNIVERSE,
    CORE_CRYPTO_UNIVERSE,
    MultiExchangeCryptoProvider,
)
from .repositories.crypto_supabase_store import CryptoSupabaseStore
from .services.crypto_news import CryptoCatalystNewsService, CryptoTelegramNotifier

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


class ScanRequest(BaseModel):
    execute_entries: bool = True


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

    # Build live LTP map & monitor open positions first
    ltp_map = {s: t.ltp for s, t in tickers.items()}
    monitor_actions = eng.monitor_open_positions(ltp_map, now_ist, store, tg)

    # Engine 0: BTC Regime Gate
    btc_4h = prov.get_candles("BTCUSDT", "4h", 120)
    regime = evaluate_btc_regime(btc_4h, tickers.get("BTCUSDT"))
    eng.last_regime = regime

    # High-risk blocked symbols from News Shield
    risk_blocked: set = set()
    bull_catalysts: Dict[str, str] = {}
    for n in news_items[:12]:
        if n.get("impact") == "HIGH_RISK":
            for c in n.get("mapped_coins") or []:
                risk_blocked.add(c.upper())
        elif n.get("impact") == "BULLISH_CATALYST":
            for c in n.get("mapped_coins") or []:
                bull_catalysts.setdefault(c.upper(), f"{n.get('tag')}: {n.get('headline', '')[:90]}")

    signals: List[CryptoSignal] = []
    scan_syms = CORE_CRYPTO_UNIVERSE[:10]
    for sym in scan_syms:
        c15 = prov.get_candles(sym, "15m", 40)
        c4h = prov.get_candles(sym, "4h", 80)
        depth = prov.get_orderbook_depth(sym)
        if c15:
            s1 = evaluate_engine_1_breakout(sym, c15, depth)
            # Catalyst Boost if breaking news + live volume confirmation
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

    # Daily Boom Radar scan (volatile small/mid-cap coins, 1x micro-size)
    boom_rows: List[Dict[str, Any]] = []
    for sym in BOOM_CRYPTO_UNIVERSE[:10]:
        c15 = prov.get_candles(sym, "15m", 30)
        depth = prov.get_orderbook_depth(sym)
        b_item = evaluate_boom_radar(sym, c15, tickers.get(sym), depth)
        boom_rows.append(b_item)
        if eng.config.enable_boom_radar and b_item["stage"] == "BOOM TRIGGERED" and sym not in risk_blocked:
            signals.append(CryptoSignal(
                symbol=sym, engine_code="BOOM_RADAR", horizon_bucket="boom",
                hold_horizon="Daily Boom Radar • 1x Micro-Size", direction="LONG",
                status="CONFIRMED", score=int(b_item["score"]),
                entry_price=Decimal(b_item["entry_price"]), stop_loss=Decimal(b_item["stop_loss"]),
                target_price=Decimal(b_item["target_price"]), expected_net_rr=Decimal("2.80"),
                recommended_leverage=Decimal("1.0"), passed_rules=b_item["checks"], failed_rules=[],
                rationale=f"Boom Radar 1x breakout on {sym}: 24h {b_item['change_24h_pct']}%, 15m Vol {b_item['rel_vol_15m']}x.",
            ))

    boom_rows.sort(key=lambda x: x["score"], reverse=True)
    eng.last_boom_items = boom_rows

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
    if execute_entries and eng.config.enabled and not eng.kill_switch_active:
        open_syms = {p.symbol for p in eng.positions.values() if p.status == "OPEN"}
        for sig in signals:
            if len(open_syms) >= eng.config.max_open_positions:
                break
            if sig.status != "CONFIRMED" or sig.symbol in open_syms or sig.symbol in risk_blocked:
                continue
            if sig.direction == "LONG" and not regime.allow_longs:
                continue
            if sig.direction == "SHORT" and not regime.allow_shorts:
                continue
            pos = eng.open_position(sig, now_ist, paper_store=store, telegram_notifier=tg, usdt_inr=usdt_inr)
            open_syms.add(pos.symbol)
            opened.append({"position_id": pos.position_id, "symbol": pos.symbol, "tier": pos.tier, "engine": pos.engine_code})

    return {
        "scan_ist": eng.last_scan_ist,
        "regime": regime.regime,
        "signals_evaluated": len(signals),
        "confirmed_count": sum(1 for s in signals if s.status == "CONFIRMED"),
        "opened_count": len(opened),
        "opened_trades": opened,
        "monitor_actions": monitor_actions,
    }


async def _crypto_24_7_scheduler(app: FastAPI) -> None:
    await asyncio.sleep(8)
    last_scan_min = -1
    last_news_min = -1
    last_summary_date = ""
    while True:
        try:
            now_ist = datetime.datetime.now(IST)
            # 1. Every 30s: monitor open positions on live LTP
            open_pos = [p for p in app.state.engine.positions.values() if p.status == "OPEN"]
            if open_pos:
                tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
                ltps = {s: t.ltp for s, t in tickers.items()}
                await asyncio.to_thread(
                    app.state.engine.monitor_open_positions, ltps, now_ist, app.state.store, app.state.tg
                )
            # 2. Every 5 min (24/7): run full 4-Engine + Boom + Catalyst cycle
            if (now_ist.minute % 5 == 0) and last_scan_min != now_ist.minute and app.state.engine.config.enabled:
                await asyncio.to_thread(_run_full_crypto_cycle, app, True)
                last_scan_min = now_ist.minute
            # 3. Every 15 min: refresh macro/catalyst news
            if (now_ist.minute % 15 == 0) and last_news_min != now_ist.minute:
                await asyncio.to_thread(app.state.news_svc.refresh_news, True)
                last_news_min = now_ist.minute
            # 4. Daily midnight IST summary -> Telegram
            today = now_ist.strftime("%Y-%m-%d")
            if now_ist.hour == 0 and now_ist.minute <= 5 and last_summary_date != today:
                tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
                ltps = {s: t.ltp for s, t in tickers.items()}
                led = app.state.engine.account_ledger(ltps, app.state.provider.get_usdt_inr())
                app.state.tg.send_alert(
                    f"DAILY CRYPTO SUMMARY — {today}",
                    f"💼 Equity: ${led['total_equity_usdt']} (₹{led['total_equity_inr']})\n"
                    f"💰 Net P&L: ${led['total_net_usdt']}\n"
                    f"🏆 Accuracy: {led['accuracy_pct']}% ({led['wins']}W/{led['losses']}L) | PF {led['profit_factor']}\n"
                    f"📂 Open: {led['open_count']} | Elite TG Today: {app.state.engine.tg_sent_today}/{app.state.engine.config.telegram_daily_cap}",
                    now_ist.strftime("%d %b %Y, %I:%M %p IST"),
                )
                last_summary_date = today
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"Crypto 24/7 scheduler warning: {type(exc).__name__}: {exc}")
        await asyncio.sleep(30)


def create_app() -> FastAPI:
    cfg = get_settings()
    app = FastAPI(title="TRADEIQ Crypto v1.0", version="1.0.0")
    app.state.cfg = cfg
    app.state.provider = MultiExchangeCryptoProvider(demo_mode=cfg.demo_mode)
    app.state.engine = CryptoPaperEngine()
    app.state.store = CryptoSupabaseStore(cfg.supabase_url, cfg.supabase_key)
    app.state.news_svc = CryptoCatalystNewsService(demo_mode=cfg.demo_mode)
    app.state.tg = CryptoTelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    app.state._scheduler_task = None

    @app.on_event("startup")
    async def on_startup():
        # Permanently restore all saved crypto paper trades from Supabase on every restart/push!
        if app.state.store.is_configured():
            restored = await asyncio.to_thread(app.state.store.fetch_crypto_positions)
            for pos in restored:
                app.state.engine.positions[pos.position_id] = pos
        await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        await asyncio.to_thread(app.state.news_svc.refresh_news)
        if cfg.app_env != "test":
            app.state._scheduler_task = asyncio.create_task(_crypto_24_7_scheduler(app))

    @app.on_event("shutdown")
    async def on_shutdown():
        t = getattr(app.state, "_scheduler_task", None)
        if t:
            t.cancel()

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "service": "tradeiq-crypto-v1", "paper_only": True, "active_exchange": app.state.provider.active_exchange}

    @app.post("/api/v1/auth/login")
    async def login(payload: LoginRequest):
        if not verify_owner_password(payload.password, cfg):
            raise HTTPException(status_code=401, detail="Invalid owner password")
        return {"access_token": issue_owner_token(cfg), "token_type": "bearer"}

    @app.get("/api/v1/crypto/overview")
    async def crypto_overview(_owner: str = Depends(require_owner)):
        prov = app.state.provider
        eng = app.state.engine
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
        return {
            "active_exchange": prov.active_exchange,
            "usdt_inr": str(usdt_inr),
            "majors": majors,
            "regime": {
                "regime": eng.last_regime.regime if eng.last_regime else "RANGE_BOUND",
                "summary": eng.last_regime.summary if eng.last_regime else "BTC Regime Gate active.",
                "btc_funding_pct": str(eng.last_regime.btc_funding_pct) if eng.last_regime else "0.010",
            },
            "config": eng.config.to_dict(),
            "tg_sent_today": eng.tg_sent_today,
            "tg_daily_cap": eng.config.telegram_daily_cap,
            "kill_switch_active": eng.kill_switch_active,
            "kill_switch_reason": eng.kill_switch_reason,
            "last_scan_ist": eng.last_scan_ist,
            "scheduler_active": bool(app.state._scheduler_task),
            "ledger": eng.account_ledger(ltps, usdt_inr),
            "boom_preview": eng.last_boom_items[:5],
            "recent_signals": eng.last_signals[:25],
        }

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

    @app.get("/api/v1/crypto/chart/{symbol}")
    async def crypto_chart(symbol: str, interval: str = Query("15m"), _owner: str = Depends(require_owner)):
        sym = symbol.upper()
        candles = await asyncio.to_thread(app.state.provider.get_candles, sym, interval, 60)
        depth = await asyncio.to_thread(app.state.provider.get_orderbook_depth, sym)
        return {
            "symbol": sym,
            "interval": interval,
            "candles": [{"ts_ms": c.ts_ms, "o": str(c.open), "h": str(c.high), "l": str(c.low), "c": str(c.close), "v": str(c.volume)} for c in candles],
            "depth": depth,
        }

    @app.put("/api/v1/crypto/config")
    async def crypto_update_config(payload: ConfigUpdateRequest, _owner: str = Depends(require_owner)):
        data = payload.model_dump(exclude_none=True) if hasattr(payload, "model_dump") else payload.dict(exclude_none=True)
        return {"config": app.state.engine.update_config(data)}

    @app.post("/api/v1/paper/positions/{position_id}/close")
    async def close_paper(position_id: str, _owner: str = Depends(require_owner)):
        pos = app.state.engine.positions.get(position_id)
        if not pos or pos.status != "OPEN":
            raise HTTPException(status_code=404, detail="Open position not found")
        tickers = await asyncio.to_thread(app.state.provider.refresh_all_tickers)
        ltp = tickers[pos.symbol].ltp if pos.symbol in tickers else pos.entry_fill_price
        closed = app.state.engine.close_position(position_id, ltp, "MANUAL_CLOSE", datetime.datetime.now(IST), app.state.store, app.state.tg)
        return {"closed": True, "net_pnl_usdt": str(closed.net_pnl_usdt) if closed else "0.00"}

    @app.delete("/api/v1/paper/positions/{position_id}")
    async def delete_paper(position_id: str, _owner: str = Depends(require_owner)):
        """Explicit manual owner-only delete — never called automatically."""
        app.state.engine.positions.pop(position_id, None)
        db_res = await asyncio.to_thread(app.state.store.delete_position, position_id)
        return {"deleted": True, "position_id": position_id, "supabase": db_res}

    @app.get("/api/v1/system/connectivity")
    async def connectivity(_owner: str = Depends(require_owner)):
        prov = app.state.provider
        return {
            "active_exchange": prov.active_exchange,
            "services": [
                {"name": "Primary: Bybit v5 Public API", **prov.exchange_health["bybit"]},
                {"name": "Failover 1: OKX v5 Public API", **prov.exchange_health["okx"]},
                {"name": "Failover 2: Kraken US-Safe API", **prov.exchange_health["kraken"]},
                {"name": "Supabase Permanent Crypto Store", "ok": app.state.store.is_configured(), "detail": app.state.store.status_summary()["table"]},
                {"name": "Telegram Elite Bot (5–7/day)", "ok": app.state.tg.enabled, "detail": f"{app.state.engine.tg_sent_today}/{app.state.engine.config.telegram_daily_cap} sent today"},
                {"name": "24/7 Autonomous Scheduler", "ok": bool(app.state._scheduler_task) or cfg.app_env == "test", "detail": "5m Scan • 30s Live Guard"},
            ],
        }

    @app.post("/api/v1/telegram/test")
    async def test_tg(_owner: str = Depends(require_owner)):
        return await asyncio.to_thread(
            app.state.tg.send_alert,
            "TEST CONNECTION",
            "TRADEIQ Crypto v1.0 is live and connected! (Paper Mode Only)",
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
                await websocket.send_json({
                    "type": "LIVE_TICK",
                    "ts_ist": datetime.datetime.now(IST).strftime("%H:%M:%S IST"),
                    "active_exchange": prov.active_exchange,
                    "usdt_inr": str(usdt_inr),
                    "majors": majors,
                    "ledger": eng.account_ledger(ltps, usdt_inr),
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
