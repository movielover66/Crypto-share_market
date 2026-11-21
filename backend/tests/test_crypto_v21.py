import datetime
import os
import unittest
from decimal import Decimal

os.environ["DEMO_MODE"] = "true"
os.environ["APP_ENV"] = "test"
os.environ["OWNER_PASSWORD"] = "crypto-owner-123"
os.environ["AUTH_JWT_SECRET"] = "test-secret-key-crypto-v21"

from fastapi.testclient import TestClient

from backend.app.core.strategy_config import StrategyConfig
from backend.app.domain.chart_reader import evaluate_12pt_chart_read
from backend.app.domain.crypto_engines import CryptoSignal
from backend.app.domain.crypto_simulator import CryptoPaperEngine, IST
from backend.app.domain.market_brain import MarketBrainEngine, MarketDirective
from backend.app.domain.portfolio_manager import evaluate_soft_bias
from backend.app.main import create_app
from backend.app.providers.crypto_provider import CryptoCandle
from backend.app.services.coin_verifier import CoinFundamentalVerifier
from backend.app.services.crypto_news import GeminiTrapJudge


class TestTradeiqCryptoV21(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)

    def _login(self) -> str:
        r = self.client.post("/api/v1/auth/login", json={"password": "crypto-owner-123"})
        self.assertEqual(r.status_code, 200)
        return r.json()["access_token"]

    def test_gate1_blocks_us_stocks_and_junk_allows_real_crypto(self) -> None:
        v = CoinFundamentalVerifier(demo_mode=True)
        for bad in ("MUUSDT", "SOXLUSDT", "SNDKUSDT", "UVXYUSDT", "USELESSUSDT", "SKDDUSDT"):
            res = v.verify_symbol(bad, Decimal("80000000"))
            self.assertFalse(res.verified, f"{bad} must be hard-blocked at Gate 1")

        sol = v.verify_symbol("SOLUSDT", Decimal("500000000"))
        self.assertTrue(sol.verified)
        # New high-liquidity genuine crypto listing passes Gate 1
        new_ai = v.verify_symbol("NEWAIUSDT", Decimal("65000000"))
        self.assertTrue(new_ai.verified)

    def test_gate3_brain_rebound_exit_and_zero_vip_bypass(self) -> None:
        brain = MarketBrainEngine(demo_mode=True)
        brain.current_directive = MarketDirective(
            mode="SEVERE_CRASH", bias="CRASH_SHORT_ONLY", score=-55, raw_score=-55,
            confidence_pct=80, hurst=0.58, hurst_label="TRENDING", vol_burst_ratio=1.5,
            breadth_green_pct=12.0, breadth_total_coins=534, btc_chg_24h=-6.2, eth_chg_24h=-6.8,
            btc_cvd_pct=-40.0, fear_greed_value=12, fear_greed_label="Extreme Fear",
            session_label="US_HIGH_VOL_SESSION", allow_longs=False, allow_shorts=True,
            allow_boom_long=False, spot_directive="DEEP_DCA_ONLY", primary_cause="crash",
        )
        # When score recovers to -20, must exit SEVERE_CRASH immediately (no stuck crash badge!)
        new_mode = brain._apply_hysteresis(-20, emergency_crash=False)
        self.assertNotEqual(new_mode, "SEVERE_CRASH")

        # Zero VIP bypass: E4 without CVD confirmation is blocked in counter-trend
        cfg = StrategyConfig(soft_bias_enabled=True, counter_trend_min_score=90)
        dec = evaluate_soft_bias("LONG", "ENGINE_4_SHADOW_SWEEP", 92, brain.current_directive, {"delta_pct": 2.0}, cfg)
        self.assertFalse(dec.allowed, "ENGINE_4 must NOT bypass CVD requirement anymore")

    def test_gate4_and_gate5_12pt_chart_reader_and_api_endpoint(self) -> None:
        tok = self._login()
        hdr = {"Authorization": f"Bearer {tok}"}
        r = self.client.get("/api/v1/crypto/chart-read/SOLUSDT?direction=LONG", headers=hdr)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["coin_verification"]["verified"])
        cr = data["chart_read"]
        self.assertEqual(len(cr["points_detail"]), 12)
        self.assertIn("poc", cr["price_map"])
        self.assertIn("big_bid_wall_px", cr["price_map"])
        self.assertIn("big_ask_wall_px", cr["price_map"])

        # Webhook for US stock MUUSDT must be rejected at Gate 1
        wh = self.client.post("/api/v1/webhooks/signal", json={"symbol": "MUUSDT", "direction": "SHORT", "score": 95})
        self.assertEqual(wh.status_code, 200)
        self.assertFalse(wh.json()["accepted"])

    def test_gate5_zero_bypass_trap_judge_and_vision_png_renderer(self) -> None:
        judge = GeminiTrapJudge("")
        # ENGINE_5 can no longer bypass extreme -6.5% bottom-short exhaustion guard
        v = judge.evaluate_trade({
            "symbol": "SOLUSDT", "direction": "SHORT", "engine_code": "ENGINE_5_CRASH_SHORT",
            "change_24h_pct": -7.2, "footprint_delta_pct": -10.0,
        })
        self.assertEqual(v["verdict"], "VETO_TRAP")

        # Pillow chart PNG snapshot renders valid PNG bytes
        candles = [
            CryptoCandle(i * 60000, Decimal(str(100 + i * 0.4)), Decimal(str(101 + i * 0.4)), Decimal(str(99.5 + i * 0.4)), Decimal(str(100.3 + i * 0.4)), Decimal("5000"))
            for i in range(25)
        ]
        png = judge.render_chart_snapshot_png("SOLUSDT", candles, {"poc": 104.0, "structural_sl": 99.0, "structural_tp": 112.0, "cvd_pct": 25.0})
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_gate6_faster_1_0r_breakeven_shift(self) -> None:
        eng = CryptoPaperEngine()
        now = datetime.datetime.now(IST)
        sig = CryptoSignal(
            symbol="SOLUSDT", engine_code="ENGINE_1_BREAKOUT", horizon_bucket="intraday",
            hold_horizon="Intraday", direction="LONG", status="CONFIRMED", score=90,
            entry_price=Decimal("100"), stop_loss=Decimal("97"), target_price=Decimal("109"),
            expected_net_rr=Decimal("3.0"), recommended_leverage=Decimal("2"),
            passed_rules=[], failed_rules=[], rationale="test",
        )
        eng.open_position(sig, now)
        # At $103.20 (> 1.0R above $100 with $3 risk), breakeven must shift immediately!
        acts = eng.monitor_open_positions({"SOLUSDT": Decimal("103.20")}, now)
        self.assertTrue(any(a["action"] == "BREAKEVEN_SHIFT" for a in acts))


if __name__ == "__main__":
    unittest.main()
