import os
import unittest
from decimal import Decimal

os.environ["DEMO_MODE"] = "true"
os.environ["APP_ENV"] = "test"
os.environ["OWNER_PASSWORD"] = "crypto-owner-123"
os.environ["AUTH_JWT_SECRET"] = "test-secret-key-crypto-v1"

from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.app.domain.crypto_engines import CryptoSignal
from backend.app.domain.crypto_simulator import CryptoPaperEngine, IST
import datetime


class TestTradeiqCryptoV1(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)

    def _login(self) -> str:
        r = self.client.post("/api/v1/auth/login", json={"password": "crypto-owner-123"})
        self.assertEqual(r.status_code, 200)
        return r.json()["access_token"]

    def test_private_endpoints_require_owner_auth(self) -> None:
        r = self.client.get("/api/v1/crypto/overview")
        self.assertEqual(r.status_code, 401)
        tok = self._login()
        r2 = self.client.get("/api/v1/crypto/overview", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r2.status_code, 200)
        self.assertIn("ledger", r2.json())

    def test_scan_boom_catalysts_chart_and_connectivity(self) -> None:
        tok = self._login()
        hdr = {"Authorization": f"Bearer {tok}"}
        s = self.client.post("/api/v1/crypto/scan", json={"execute_entries": True}, headers=hdr)
        self.assertEqual(s.status_code, 200)
        self.assertGreater(s.json()["signals_evaluated"], 0)

        b = self.client.get("/api/v1/crypto/boom", headers=hdr)
        self.assertEqual(b.status_code, 200)
        self.assertGreater(len(b.json()["items"]), 0)

        c = self.client.get("/api/v1/crypto/catalysts", headers=hdr)
        self.assertEqual(c.status_code, 200)
        self.assertGreater(c.json()["count"], 0)

        ch = self.client.get("/api/v1/crypto/chart/BTCUSDT?interval=15m", headers=hdr)
        self.assertEqual(ch.status_code, 200)
        self.assertGreater(len(ch.json()["candles"]), 10)

        conn = self.client.get("/api/v1/system/connectivity", headers=hdr)
        self.assertEqual(conn.status_code, 200)
        self.assertGreaterEqual(len(conn.json()["services"]), 5)

    def test_two_tier_telegram_cap_and_1_5r_breakeven(self) -> None:
        eng = CryptoPaperEngine()
        eng.config.telegram_daily_cap = 2
        now = datetime.datetime.now(IST)

        class _MockTG:
            enabled = True
            sent = 0
            def send_alert(self, t, b, ts=""):
                self.sent += 1
                return {"delivered": True}

        tg = _MockTG()
        for i in range(4):
            sig = CryptoSignal(
                symbol=f"COIN{i}USDT", engine_code="ENGINE_1_BREAKOUT", horizon_bucket="intraday",
                hold_horizon="Intraday • 2–12 hrs", direction="LONG", status="CONFIRMED",
                score=90, entry_price=Decimal("100"), stop_loss=Decimal("98"),
                target_price=Decimal("106"), expected_net_rr=Decimal("2.5"),
                recommended_leverage=Decimal("2"), passed_rules=[], failed_rules=[], rationale="test",
            )
            eng.open_position(sig, now, telegram_notifier=tg)

        # Only 2 Elite alerts sent to Telegram, remaining 2 tracked in Backend Lab!
        self.assertEqual(tg.sent, 2)
        self.assertEqual(len(eng.positions), 4)
        tiers = [p.tier for p in eng.positions.values()]
        self.assertEqual(tiers.count("ELITE_TG"), 2)
        self.assertEqual(tiers.count("BACKEND_LAB"), 2)

        # Trigger 1.5R breakeven shift on COIN0USDT
        acts = eng.monitor_open_positions({"COIN0USDT": Decimal("103.50")}, now)
        self.assertTrue(any(a["action"] == "BREAKEVEN_SHIFT" for a in acts))

    def test_ws_live_auth_and_stream(self) -> None:
        tok = self._login()
        with self.client.websocket_connect(f"/api/v1/ws/live?token={tok}&max_frames=1") as ws:
            frame = ws.receive_json()
            self.assertEqual(frame["type"], "LIVE_TICK")
            self.assertIn("ledger", frame)


if __name__ == "__main__":
    unittest.main()
