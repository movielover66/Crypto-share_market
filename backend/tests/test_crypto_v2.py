import datetime
import os
import unittest
from decimal import Decimal

os.environ["DEMO_MODE"] = "true"
os.environ["APP_ENV"] = "test"
os.environ["OWNER_PASSWORD"] = "crypto-owner-123"
os.environ["AUTH_JWT_SECRET"] = "test-secret-key-crypto-v2"

from fastapi.testclient import TestClient

from backend.app.core.strategy_config import StrategyConfig
from backend.app.domain.crypto_engines import CryptoSignal
from backend.app.domain.crypto_simulator import CryptoPaperEngine, IST
from backend.app.domain.market_brain import MarketBrainEngine, MarketDirective
from backend.app.domain.portfolio_manager import (
    build_engine_scorecard,
    check_risk_firewall,
    compute_equal_risk_sizing,
    evaluate_soft_bias,
)
from backend.app.domain.smc_filter import evaluate_smc_context
from backend.app.domain.spot_vault import SpotPosition
from backend.app.main import create_app
from backend.app.providers.crypto_provider import CryptoCandle, CryptoTicker
from backend.app.repositories.crypto_supabase_store import CryptoSupabaseStore


class TestTradeiqCryptoV2(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)

    def _login(self) -> str:
        r = self.client.post("/api/v1/auth/login", json={"password": "crypto-owner-123"})
        self.assertEqual(r.status_code, 200)
        return r.json()["access_token"]

    def test_bug2_fast_pulse_detects_btc_spike(self) -> None:
        brain = MarketBrainEngine(demo_mode=True)
        now = datetime.datetime.now(IST)
        brain.current_directive = MarketDirective(
            mode="SIDEWAYS_RANGE", bias="BOTH_RANGE", score=0, raw_score=0,
            confidence_pct=60, hurst=0.50, hurst_label="TRANSITION", vol_burst_ratio=1.0,
            breadth_green_pct=50.0, breadth_total_coins=100, btc_chg_24h=0.5, eth_chg_24h=0.5,
            btc_cvd_pct=0.0, fear_greed_value=50, fear_greed_label="Neutral",
            session_label="ASIA_MORNING_BOUNCE", allow_longs=True, allow_shorts=True,
            allow_boom_long=True, spot_directive="QUICK_BOUNCE", primary_cause="test",
        )
        tickers = {
            "BTCUSDT": CryptoTicker("BTCUSDT", Decimal("90000"), Decimal("92000"), Decimal("-2.0"), Decimal("1e9"), Decimal("0"), Decimal("0"), "DEMO", 0.0),
            "ETHUSDT": CryptoTicker("ETHUSDT", Decimal("3000"), Decimal("3050"), Decimal("-1.5"), Decimal("5e8"), Decimal("0"), Decimal("0"), "DEMO", 0.0),
        }
        needs_deep, _ = brain.fast_pulse(tickers, now)
        self.assertTrue(needs_deep, "fast_pulse must trigger deep refresh when BTC shifts >= 1.5%")

    def test_bug4_and_bug5_equal_risk_sizing_and_live_inr_fx(self) -> None:
        cfg = StrategyConfig(equal_risk_enabled=True, risk_per_trade_inr=Decimal("500.00"))
        # Volatile coin with 5% SL vs calm coin with 1.5% SL -> both target ~₹500 risk
        m_vol, _, r_vol = compute_equal_risk_sizing(
            Decimal("100"), Decimal("95"), Decimal("2"), Decimal("93.37"), Decimal("1.0"), cfg, Decimal("200")
        )
        m_calm, _, r_calm = compute_equal_risk_sizing(
            Decimal("100"), Decimal("98.5"), Decimal("2"), Decimal("93.37"), Decimal("1.0"), cfg, Decimal("200")
        )
        self.assertLess(m_vol, m_calm, "Volatile coin must receive smaller margin than calm coin")
        self.assertAlmostEqual(float(r_vol), 500.0, delta=30.0)
        self.assertAlmostEqual(float(r_calm), 500.0, delta=30.0)

        # Verify live FX (93.37) used on Telegram PnL card instead of hardcoded 85.20
        eng = CryptoPaperEngine()
        captured = {}

        class _MockTG:
            enabled = True
            def send_html(self, *a, **kw):
                return {"message_id": 101}
            def send_pnl_card(self, **kw):
                captured.update(kw)
                return {"delivered": True}

        sig = CryptoSignal(
            symbol="SOLUSDT", engine_code="ENGINE_1_BREAKOUT", horizon_bucket="intraday",
            hold_horizon="Intraday", direction="LONG", status="CONFIRMED", score=92,
            entry_price=Decimal("100"), stop_loss=Decimal("98"), target_price=Decimal("106"),
            expected_net_rr=Decimal("3.0"), recommended_leverage=Decimal("2"),
            passed_rules=[], failed_rules=[], rationale="test",
        )
        now = datetime.datetime.now(IST)
        pos = eng.open_position(sig, now, telegram_notifier=_MockTG(), usdt_inr=Decimal("93.37"), strategy_cfg=cfg)
        eng.close_position(pos.position_id, Decimal("106"), "TARGET_HIT", now, telegram_notifier=_MockTG(), usdt_inr=Decimal("93.37"))
        expected_inr = abs((pos.net_pnl_usdt * Decimal("93.37")).quantize(Decimal("1")))
        self.assertIn(f"Rs {expected_inr}", captured.get("sub_pnl", ""))

    def test_bug6_soft_bias_allows_a_plus_counter_trend_at_half_risk(self) -> None:
        cfg = StrategyConfig(soft_bias_enabled=True, counter_trend_min_score=90, counter_trend_risk_multiplier=Decimal("0.50"))
        bull_brain = MarketDirective(
            mode="STRONG_UPTREND", bias="LONG_ONLY", score=68, raw_score=68,
            confidence_pct=85, hurst=0.61, hurst_label="TRENDING", vol_burst_ratio=1.2,
            breadth_green_pct=78.0, breadth_total_coins=534, btc_chg_24h=5.2, eth_chg_24h=4.8,
            btc_cvd_pct=25.0, fear_greed_value=72, fear_greed_label="Greed",
            session_label="US_HIGH_VOL_SESSION", allow_longs=True, allow_shorts=False,
            allow_boom_long=True, spot_directive="WAIT_TOP", primary_cause="rally",
        )
        # Weak counter-trend short (score 82) -> rejected
        weak = evaluate_soft_bias("SHORT", "ENGINE_3_FUNDING_RSI2", 82, bull_brain, {"delta_pct": -22.0}, cfg)
        self.assertFalse(weak.allowed)
        # Strong A+ counter-trend short (score 93 + strong negative CVD) -> allowed at 0.5x!
        strong = evaluate_soft_bias("SHORT", "ENGINE_3_FUNDING_RSI2", 93, bull_brain, {"delta_pct": -35.0}, cfg)
        self.assertTrue(strong.allowed)
        self.assertEqual(strong.risk_multiplier, Decimal("0.50"))

    def test_unlimited_mode_and_custom_cap_switch(self) -> None:
        tok = self._login()
        hdr = {"Authorization": f"Bearer {tok}"}
        ov = self.client.get("/api/v1/crypto/overview", headers=hdr).json()
        self.assertTrue(ov["strategy_config"]["unlimited_paper_mode"])
        self.assertEqual(ov["strategy_config"]["max_open_positions"], 0)

        # Switch to custom cap of 4
        r = self.client.put(
            "/api/v1/crypto/strategy-config",
            json={"unlimited_paper_mode": False, "max_open_positions": 4, "risk_per_trade_inr": 600},
            headers=hdr,
        )
        self.assertEqual(r.status_code, 200)
        sc = r.json()["strategy_config"]
        self.assertFalse(sc["unlimited_paper_mode"])
        self.assertEqual(sc["max_open_positions"], 4)
        self.assertEqual(sc["risk_per_trade_inr"], "600.00")

        # Switch back to Unlimited
        r2 = self.client.put("/api/v1/crypto/strategy-config", json={"unlimited_paper_mode": True}, headers=hdr)
        self.assertTrue(r2.json()["strategy_config"]["unlimited_paper_mode"])
        self.assertEqual(r2.json()["strategy_config"]["max_open_positions"], 0)

    def test_bug9_and_bug10_supabase_created_at_and_taker_fees(self) -> None:
        store = CryptoSupabaseStore("https://example.supabase.co", "test-key")
        saved_rows = []
        store._request = lambda method, path, payload=None, prefer="": saved_rows.append(payload) or []

        orig_dt = datetime.datetime(2026, 11, 10, 12, 0, 0, tzinfo=IST)
        pos = SpotPosition(
            position_id="SPOT-ETHUSDT-TEST", symbol="ETHUSDT", mode="AUTO_DCA",
            budget_usdt=Decimal("120"), total_invested_usdt=Decimal("84"), total_qty=Decimal("0.035"),
            avg_entry_price=Decimal("2400"), first_entry_price=Decimal("2450"),
            target_net_pct=Decimal("5.0"), net_target_price=Decimal("2525"),
            buy_fees_usdt=Decimal("0.084"), tranches_filled=2, max_tranches=3,
            opened_at_ist=orig_dt.isoformat(),
        )
        store.upsert_spot_position(pos)
        self.assertEqual(saved_rows[0]["created_at"], int(orig_dt.timestamp()))

        # Bug #10: Restoring closed perp deducts entry + exit 0.05% taker fees
        store._request = lambda method, path, payload=None, prefer="": [{
            "id": "CR-FEE1", "symbol": "BTCUSDT", "market": "CRYPTO_V1",
            "budget_inr": 200.0, "buy_price_inr": 100.0, "target_inr": 110.0, "stop_loss_inr": 95.0,
            "hold_label": "intraday|ENGINE_1_BREAKOUT|LONG|2.0|BACKEND_LAB|0",
            "status": "WIN", "exit_price_inr": 110.0, "created_at": 1700000000, "closed_at": 1700003600,
        }]
        restored = store.fetch_crypto_positions()
        self.assertEqual(len(restored), 1)
        # Gross = (110 - 100) * 4 = 40.00; Fees = 400*0.0005 (0.20) + 440*0.0005 (0.22) = 0.42 -> Net = 39.58
        self.assertEqual(restored[0].net_pnl_usdt, Decimal("39.58"))

    def test_bug11_duplicate_manual_spot_buy_blocked_and_ws_spot_summary(self) -> None:
        tok = self._login()
        hdr = {"Authorization": f"Bearer {tok}"}
        r1 = self.client.post("/api/v1/spot/buy", json={"symbol": "ETHUSDT"}, headers=hdr)
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/v1/spot/buy", json={"symbol": "ETHUSDT"}, headers=hdr)
        self.assertEqual(r2.status_code, 409)

        with self.client.websocket_connect(f"/api/v1/ws/live?token={tok}&max_frames=1") as ws:
            frame = ws.receive_json()
            self.assertIn("spot_summary", frame)
            self.assertEqual(frame["spot_summary"]["open_count"], 1)
            self.assertIn("strategy_config", frame)

    def test_smc_filter_scorecard_and_webhook_endpoint(self) -> None:
        # Build synthetic candles with a clean Bullish FVG in Discount Zone
        candles = []
        px = 120.0
        for i in range(25):
            px -= 0.7 if i < 20 else -0.4
            candles.append(CryptoCandle(i * 60000, Decimal(str(px)), Decimal(str(px + 0.5)), Decimal(str(px - 0.5)), Decimal(str(px - 0.1)), Decimal("1000")))
        smc = evaluate_smc_context(candles, direction="LONG")
        self.assertEqual(smc.zone, "DISCOUNT")
        self.assertGreaterEqual(smc.score_delta, 4)

        tok = self._login()
        hdr = {"Authorization": f"Bearer {tok}"}
        wh = self.client.post("/api/v1/webhooks/signal", json={"symbol": "SOLUSDT", "direction": "LONG", "score": 92})
        self.assertEqual(wh.status_code, 200)
        self.assertTrue(wh.json()["accepted"])

        sc = self.client.get("/api/v1/crypto/scorecard", headers=hdr)
        self.assertEqual(sc.status_code, 200)
        codes = [e["engine_code"] for e in sc.json()["engines"]]
        self.assertIn("WEBHOOK_EXTERNAL", codes)
        self.assertIn("SPOT_VAULT", codes)


if __name__ == "__main__":
    unittest.main()
