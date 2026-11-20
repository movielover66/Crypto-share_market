"""
TRADEIQ Crypto v1.0 — Permanent Supabase Persistence Store (`market="CRYPTO_V1"` & `market="CRYPTO_SPOT"`).
Reuses the user's existing Supabase `paper_trades` table with isolated market keys:
  - `market="CRYPTO_V1"`   -> USDT-M Perpetuals
  - `market="CRYPTO_SPOT"` -> Institutional Spot Buy Vault (No-SL, 3-Step Smart DCA)
Never collides with NSE Equity trades AND survives every code push or Render restart!
Only explicit manual owner delete removes a row.
"""
import datetime
import json
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..domain.crypto_simulator import CryptoPaperPosition, IST
from ..domain.spot_vault import SpotPosition, compute_net_target_price


class CryptoSupabaseStore:
    def __init__(self, supabase_url: str = "", supabase_key: str = "") -> None:
        self.url = (supabase_url or "").strip().rstrip("/")
        self.key = (supabase_key or "").strip()
        self.last_error: Optional[str] = None
        self.last_operation: Optional[str] = None

    def is_configured(self) -> bool:
        return bool(self.url and self.key and not self.key.startswith("YOUR_"))

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, prefer: str = "") -> Any:
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        req = urllib.request.Request(f"{self.url}/rest/v1/{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else []

    # ── PERPETUALS (`market="CRYPTO_V1"`) ──────────────────────────────────────

    def fetch_crypto_positions(self) -> List[CryptoPaperPosition]:
        if not self.is_configured():
            return []
        try:
            rows = self._request("GET", "paper_trades?market=eq.CRYPTO_V1&select=*&order=created_at.desc")
            self.last_error = None
            self.last_operation = "fetch"
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.last_operation = "fetch_failed"
            return []

        restored: List[CryptoPaperPosition] = []
        for r in rows or []:
            try:
                pid = str(r.get("id") or "")
                sym = str(r.get("symbol") or "").upper()
                if not pid or not sym:
                    continue
                entry = Decimal(str(r.get("buy_price_inr") or "0"))
                margin = Decimal(str(r.get("budget_inr") or "200"))
                sl = Decimal(str(r.get("stop_loss_inr") or "0"))
                target = Decimal(str(r.get("target_inr") or "0"))
                raw_label = str(r.get("hold_label") or "intraday|ENGINE_1_BREAKOUT|LONG|2.0|BACKEND_LAB")
                parts = raw_label.split("|")
                raw_hb = (parts[0].strip().lower() if len(parts) > 0 and parts[0] else "intraday")
                horizon_bucket = raw_hb if raw_hb in ("intraday", "reversion", "swing", "boom") else "intraday"
                engine_code = parts[1] if len(parts) > 1 and parts[1] else "ENGINE_1_BREAKOUT"
                direction = parts[2] if len(parts) > 2 and parts[2] in ("LONG", "SHORT") else "LONG"
                lev = Decimal(parts[3]) if len(parts) > 3 else Decimal("2.0")
                tier = parts[4] if len(parts) > 4 else "BACKEND_LAB"
                tg_mid = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None

                hold_map = {
                    "intraday": "Intraday • 2–12 hrs (Session Breakout)",
                    "reversion": "Short Reversion • 1–4 Days",
                    "swing": "Trend Swing • 3–14 Days (Donchian)",
                    "boom": "Daily Boom Radar • 1x Micro-Size",
                }
                status = "OPEN" if str(r.get("status", "")).upper() == "HOLDING" else "CLOSED"
                qty = ((margin * lev) / entry).quantize(Decimal("0.0001")) if entry > 0 else Decimal("0")
                created_ist = datetime.datetime.fromtimestamp(int(r.get("created_at") or 0), tz=datetime.timezone.utc).astimezone(IST).isoformat()
                exit_px = Decimal(str(r.get("exit_price_inr") or "0")) or None
                closed_ist = (
                    datetime.datetime.fromtimestamp(int(r["closed_at"]), tz=datetime.timezone.utc).astimezone(IST).isoformat()
                    if r.get("closed_at") else None
                )
                net_pnl = Decimal("0.00")
                outcome = None
                if status == "CLOSED" and exit_px and entry > 0:
                    net_pnl = (
                        ((exit_px - entry) * qty) if direction == "LONG" else ((entry - exit_px) * qty)
                    ).quantize(Decimal("0.01"))
                    outcome = "WIN" if net_pnl > 0 else ("LOSS" if net_pnl < 0 else "BREAKEVEN")

                restored.append(CryptoPaperPosition(
                    position_id=pid, symbol=sym, horizon_bucket=horizon_bucket,
                    hold_horizon=hold_map.get(horizon_bucket, "Intraday • 2–12 hrs"),
                    engine_code=engine_code, direction=direction, tier=tier,
                    leverage=lev, margin_usdt=margin, quantity=qty,
                    entry_fill_price=entry, stop_loss=sl, initial_stop_loss=sl, target_price=target,
                    status=status, exit_fill_price=exit_px,
                    exit_reason="SUPABASE_RESTORED" if status == "CLOSED" else None,
                    net_pnl_usdt=net_pnl, outcome=outcome,
                    opened_at_ist=created_ist, closed_at_ist=closed_ist,
                    rationale="Restored permanently from Supabase",
                    tg_message_id=tg_mid,
                ))
            except Exception:
                continue
        return restored

    def insert_position(self, pos: CryptoPaperPosition) -> Dict[str, Any]:
        row = {
            "id": pos.position_id,
            "symbol": pos.symbol,
            "market": "CRYPTO_V1",
            "budget_inr": float(pos.margin_usdt),
            "buy_price_inr": float(pos.entry_fill_price),
            "target_inr": float(pos.target_price),
            "stop_loss_inr": float(pos.stop_loss),
            "hold_label": f"{pos.horizon_bucket}|{pos.engine_code}|{pos.direction}|{pos.leverage}|{pos.tier}|{pos.tg_message_id or 0}",
            "status": "HOLDING",
            "created_at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }
        try:
            self._request("POST", "paper_trades", row, prefer="resolution=merge-duplicates")
            self.last_error = None
            self.last_operation = "insert"
            return {"persisted": True}
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.last_operation = "insert_failed"
            return {"persisted": False, "error": self.last_error}

    def close_position(self, pos: CryptoPaperPosition) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"id": f"eq.{pos.position_id}"})
        patch = {
            "status": pos.outcome or "CLOSED",
            "exit_price_inr": float(pos.exit_fill_price or 0),
            "closed_at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }
        try:
            self._request("PATCH", f"paper_trades?{query}", patch)
            self.last_error = None
            self.last_operation = "close"
            return {"persisted": True}
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.last_operation = "close_failed"
            return {"persisted": False, "error": self.last_error}

    def delete_position(self, position_id: str) -> Dict[str, Any]:
        """Explicit owner-only manual delete; never called automatically."""
        if not self.is_configured():
            return {"deleted": False}
        query = urllib.parse.urlencode({"id": f"eq.{position_id}"})
        try:
            self._request("DELETE", f"paper_trades?{query}")
            self.last_error = None
            self.last_operation = "delete"
            return {"deleted": True}
        except Exception as exc:
            self.last_error = type(exc).__name__
            return {"deleted": False, "error": self.last_error}

    # ── SPOT BUY VAULT (`market="CRYPTO_SPOT"`) ────────────────────────────────

    def fetch_spot_positions(self) -> List[SpotPosition]:
        if not self.is_configured():
            return []
        try:
            rows = self._request("GET", "paper_trades?market=eq.CRYPTO_SPOT&select=*&order=created_at.desc")
        except Exception:
            return []

        restored: List[SpotPosition] = []
        for r in rows or []:
            try:
                pid = str(r.get("id") or "")
                sym = str(r.get("symbol") or "").upper()
                if not pid or not sym:
                    continue
                invested = Decimal(str(r.get("budget_inr") or "48"))
                avg_px = Decimal(str(r.get("buy_price_inr") or "0"))
                tgt_px = Decimal(str(r.get("target_inr") or "0"))
                raw_label = str(r.get("hold_label") or "AUTO_DCA|1|3|120|5.0|0|0|0")
                parts = raw_label.split("|")
                mode = parts[0] if len(parts) > 0 and parts[0] else "AUTO_DCA"
                tr_filled = int(parts[1]) if len(parts) > 1 else 1
                max_tr = int(parts[2]) if len(parts) > 2 else 3
                budget = Decimal(parts[3]) if len(parts) > 3 else invested
                tgt_pct = Decimal(parts[4]) if len(parts) > 4 else Decimal("5.0")
                first_px = Decimal(parts[5]) if len(parts) > 5 and Decimal(parts[5]) > 0 else avg_px
                qty = Decimal(parts[6]) if len(parts) > 6 and Decimal(parts[6]) > 0 else (
                    ((invested * Decimal("0.999")) / avg_px).quantize(Decimal("0.000001")) if avg_px > 0 else Decimal("0")
                )
                buy_fees = Decimal(parts[7]) if len(parts) > 7 else (invested * Decimal("0.001")).quantize(Decimal("0.0001"))
                sp_tg_mid = int(parts[8]) if len(parts) > 8 and parts[8].isdigit() and int(parts[8]) > 0 else None
                if tgt_px <= 0 and qty > 0:
                    tgt_px = compute_net_target_price(invested, qty, tgt_pct)

                status = "OPEN" if str(r.get("status", "")).upper() == "HOLDING" else "CLOSED"
                created_ist = datetime.datetime.fromtimestamp(int(r.get("created_at") or 0), tz=datetime.timezone.utc).astimezone(IST).isoformat()
                exit_px = Decimal(str(r.get("exit_price_inr") or "0")) or None
                closed_ist = (
                    datetime.datetime.fromtimestamp(int(r["closed_at"]), tz=datetime.timezone.utc).astimezone(IST).isoformat()
                    if r.get("closed_at") else None
                )
                net_pnl = Decimal("0.00")
                outcome = None
                if status == "CLOSED" and exit_px and qty > 0:
                    net_proceeds = (qty * exit_px) * Decimal("0.999")
                    net_pnl = (net_proceeds - invested).quantize(Decimal("0.01"))
                    outcome = "WIN" if net_pnl >= 0 else "LOSS"

                restored.append(SpotPosition(
                    position_id=pid, symbol=sym, mode=mode, budget_usdt=budget,
                    total_invested_usdt=invested, total_qty=qty,
                    avg_entry_price=avg_px, first_entry_price=first_px,
                    target_net_pct=tgt_pct, net_target_price=tgt_px,
                    buy_fees_usdt=buy_fees, tranches_filled=tr_filled, max_tranches=max_tr,
                    status=status, exit_fill_price=exit_px,
                    exit_reason="SUPABASE_RESTORED" if status == "CLOSED" else None,
                    net_pnl_usdt=net_pnl, outcome=outcome,
                    opened_at_ist=created_ist, closed_at_ist=closed_ist,
                    rationale=f"Spot Vault ({tr_filled}/{max_tr} tranches) restored from Supabase",
                    tg_message_id=sp_tg_mid,
                ))
            except Exception:
                continue
        return restored

    def upsert_spot_position(self, pos: SpotPosition) -> Dict[str, Any]:
        row = {
            "id": pos.position_id,
            "symbol": pos.symbol,
            "market": "CRYPTO_SPOT",
            "budget_inr": float(pos.total_invested_usdt),
            "buy_price_inr": float(pos.avg_entry_price),
            "target_inr": float(pos.net_target_price),
            "stop_loss_inr": 0.0,
            "hold_label": f"{pos.mode}|{pos.tranches_filled}|{pos.max_tranches}|{pos.budget_usdt}|{pos.target_net_pct}|{pos.first_entry_price}|{pos.total_qty}|{pos.buy_fees_usdt}|{pos.tg_message_id or 0}",
            "status": "HOLDING",
            "created_at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }
        try:
            self._request("POST", "paper_trades", row, prefer="resolution=merge-duplicates")
            return {"persisted": True}
        except Exception as exc:
            return {"persisted": False, "error": type(exc).__name__}

    def close_spot_position(self, pos: SpotPosition) -> Dict[str, Any]:
        query = urllib.parse.urlencode({"id": f"eq.{pos.position_id}"})
        patch = {
            "status": pos.outcome or "WIN",
            "exit_price_inr": float(pos.exit_fill_price or 0),
            "closed_at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        }
        try:
            self._request("PATCH", f"paper_trades?{query}", patch)
            return {"persisted": True}
        except Exception as exc:
            return {"persisted": False, "error": type(exc).__name__}

    def status_summary(self) -> Dict[str, Any]:
        return {
            "configured": self.is_configured(),
            "table": "paper_trades (CRYPTO_V1 + CRYPTO_SPOT)",
            "last_operation": self.last_operation,
            "last_error": self.last_error,
        }
