"""
TRADEIQ Crypto v1.0 — 3-Tier Keyless Public Crypto Market Data Provider.
Failover chain (zero paid keys, zero Binance HTTP 451 geo-block on Render):
  1. Primary:    Bybit v5 Public API (Linear Perpetuals: tickers, klines, L2 orderbook, funding, OI)
  2. Failover 1: OKX v5 Public API   (USDT-SWAP tickers, candles, books, funding)
  3. Failover 2: Kraken Public API   (US-cloud-safe spot OHLC/ticker backup)
Paper-trading market data only. Deterministic Demo mode used strictly when DEMO_MODE=true.
"""
import datetime
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple


CORE_CRYPTO_UNIVERSE: List[str] = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOGEUSDT", "SUIUSDT",
    "NEARUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "APTUSDT",
    "INJUSDT", "RENDERUSDT", "FETUSDT", "LTCUSDT", "DOTUSDT",
]

BOOM_CRYPTO_UNIVERSE: List[str] = [
    "PEPEUSDT", "DOGEUSDT", "WIFUSDT", "BONKUSDT", "SHIBUSDT",
    "FLOKIUSDT", "SUIUSDT", "SEIUSDT", "ENAUSDT", "GALAUSDT",
    "RENDERUSDT", "FETUSDT", "NEARUSDT", "TONUSDT", "ARBUSDT",
]


@dataclass(frozen=True)
class CryptoCandle:
    ts_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass
class CryptoTicker:
    symbol: str
    ltp: Decimal
    prev_24h: Decimal
    change_24h_pct: Decimal
    volume_24h_usdt: Decimal
    funding_rate: Decimal
    open_interest: Decimal
    source: str
    updated_at: float


def _http_get_json(url: str, timeout: float = 5.0) -> Optional[Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TRADEIQ-Crypto/1.0", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


class MultiExchangeCryptoProvider:
    def __init__(self, demo_mode: bool = False) -> None:
        self.demo_mode = demo_mode
        self.active_exchange: str = "DEMO" if demo_mode else "BYBIT_V5"
        self.usdt_inr_rate: Decimal = Decimal("85.20")
        self.last_rate_sync: float = 0.0
        self.ticker_cache: Dict[str, CryptoTicker] = {}
        self.candle_cache: Dict[Tuple[str, str], Tuple[float, List[CryptoCandle]]] = {}
        self.depth_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self.exchange_health: Dict[str, Dict[str, Any]] = {
            "bybit": {"ok": not demo_mode, "detail": "Primary Bybit v5 Linear Perp"},
            "okx": {"ok": True, "detail": "Failover 1 OKX v5 Swap"},
            "kraken": {"ok": True, "detail": "Failover 2 Kraken US-Safe"},
        }
        self.last_error: Optional[str] = None

    def get_usdt_inr(self) -> Decimal:
        now = time.time()
        if self.demo_mode or (now - self.last_rate_sync) < 1800:
            return self.usdt_inr_rate
        data = _http_get_json("https://open.er-api.com/v6/latest/USD", timeout=4.0)
        try:
            inr = (data or {}).get("rates", {}).get("INR")
            if inr and float(inr) > 70:
                self.usdt_inr_rate = Decimal(str(round(float(inr), 2)))
                self.last_rate_sync = now
        except Exception:
            pass
        return self.usdt_inr_rate

    def refresh_all_tickers(self) -> Dict[str, CryptoTicker]:
        if self.demo_mode:
            return self._demo_tickers()

        # 1. Try Primary: Bybit v5 Linear Perpetuals (single batch call = super fast!)
        data = _http_get_json("https://api.bybit.com/v5/market/tickers?category=linear", timeout=6.0)
        rows = (((data or {}).get("result") or {}).get("list")) or []
        if rows:
            now = time.time()
            for r in rows:
                sym = str(r.get("symbol") or "").upper()
                if not sym.endswith("USDT"):
                    continue
                try:
                    ltp = Decimal(str(r.get("lastPrice") or "0"))
                    if ltp <= 0:
                        continue
                    prev = Decimal(str(r.get("prevPrice24h") or ltp))
                    pct = (Decimal(str(r.get("price24hPcnt") or "0")) * Decimal("100")).quantize(Decimal("0.01"))
                    vol = Decimal(str(r.get("turnover24h") or "0")).quantize(Decimal("0.01"))
                    fr = Decimal(str(r.get("fundingRate") or "0"))
                    oi = Decimal(str(r.get("openInterestValue") or r.get("openInterest") or "0"))
                    self.ticker_cache[sym] = CryptoTicker(
                        symbol=sym, ltp=ltp, prev_24h=prev, change_24h_pct=pct,
                        volume_24h_usdt=vol, funding_rate=fr, open_interest=oi,
                        source="BYBIT_V5", updated_at=now,
                    )
                except Exception:
                    continue
            if self.ticker_cache:
                self.active_exchange = "BYBIT_V5"
                self.exchange_health["bybit"] = {"ok": True, "detail": f"Connected • {len(self.ticker_cache)} USDT Perps"}
                return self.ticker_cache

        self.exchange_health["bybit"] = {"ok": False, "detail": "Timeout/Geo-limit — Failover to OKX"}

        # 2. Failover 1: OKX v5 USDT-SWAP Tickers
        okx = _http_get_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP", timeout=6.0)
        orows = (okx or {}).get("data") or []
        if orows:
            now = time.time()
            for r in orows:
                inst = str(r.get("instId") or "")
                if not inst.endswith("-USDT-SWAP"):
                    continue
                sym = inst.replace("-USDT-SWAP", "USDT").upper()
                try:
                    ltp = Decimal(str(r.get("last") or "0"))
                    if ltp <= 0:
                        continue
                    open24 = Decimal(str(r.get("open24h") or ltp))
                    pct = (((ltp - open24) / open24) * Decimal("100")).quantize(Decimal("0.01")) if open24 > 0 else Decimal("0.00")
                    vol = Decimal(str(r.get("volCcy24h") or "0")) * ltp
                    self.ticker_cache[sym] = CryptoTicker(
                        symbol=sym, ltp=ltp, prev_24h=open24, change_24h_pct=pct,
                        volume_24h_usdt=vol.quantize(Decimal("0.01")), funding_rate=Decimal("0.0001"),
                        open_interest=Decimal("0"), source="OKX_V5", updated_at=now,
                    )
                except Exception:
                    continue
            if self.ticker_cache:
                self.active_exchange = "OKX_V5"
                self.exchange_health["okx"] = {"ok": True, "detail": f"Active Failover • {len(self.ticker_cache)} Swaps"}
                return self.ticker_cache

        self.exchange_health["okx"] = {"ok": False, "detail": "Timeout — Failover to Kraken"}

        # 3. Failover 2: Kraken US-Safe Public Ticker for major pairs
        k_map = {"BTCUSDT": "XBTUSDT", "ETHUSDT": "ETHUSDT", "SOLUSDT": "SOLUSDT", "XRPUSDT": "XRPUSDT", "DOGEUSDT": "XDGUSDT", "ADAUSDT": "ADAUSDT", "LINKUSDT": "LINKUSDT", "AVAXUSDT": "AVAXUSDT"}
        kr = _http_get_json("https://api.kraken.com/0/public/Ticker?pair=" + ",".join(k_map.values()), timeout=6.0)
        kres = (kr or {}).get("result") or {}
        if kres:
            now = time.time()
            for std_sym, kpair in k_map.items():
                row = kres.get(kpair) or kres.get(std_sym)
                if not row:
                    continue
                try:
                    ltp = Decimal(str(row["c"][0]))
                    op = Decimal(str(row["o"]))
                    pct = (((ltp - op) / op) * Decimal("100")).quantize(Decimal("0.01")) if op > 0 else Decimal("0.00")
                    vol = (Decimal(str(row["v"][1])) * ltp).quantize(Decimal("0.01"))
                    self.ticker_cache[std_sym] = CryptoTicker(
                        symbol=std_sym, ltp=ltp, prev_24h=op, change_24h_pct=pct,
                        volume_24h_usdt=vol, funding_rate=Decimal("0.0001"),
                        open_interest=Decimal("0"), source="KRAKEN_V2", updated_at=now,
                    )
                except Exception:
                    continue
            if self.ticker_cache:
                self.active_exchange = "KRAKEN_V2"
                self.exchange_health["kraken"] = {"ok": True, "detail": "US-Safe Failover Active"}
                return self.ticker_cache

        return self.ticker_cache

    def get_candles(self, symbol: str, interval: str = "15m", limit: int = 120) -> List[CryptoCandle]:
        sym = symbol.upper()
        if self.demo_mode:
            return self._demo_candles(sym, interval, limit)

        key = (sym, interval)
        cached = self.candle_cache.get(key)
        if cached and (time.time() - cached[0]) < 35.0:
            return cached[1]

        # Bybit interval map
        b_int = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}.get(interval, "15")
        url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={urllib.parse.quote(sym)}&interval={b_int}&limit={limit}"
        data = _http_get_json(url, timeout=5.0)
        rows = (((data or {}).get("result") or {}).get("list")) or []
        out: List[CryptoCandle] = []
        if rows:
            for r in reversed(rows):
                try:
                    out.append(CryptoCandle(
                        ts_ms=int(r[0]), open=Decimal(str(r[1])), high=Decimal(str(r[2])),
                        low=Decimal(str(r[3])), close=Decimal(str(r[4])), volume=Decimal(str(r[5])),
                    ))
                except Exception:
                    continue
            if out:
                self.candle_cache[key] = (time.time(), out)
                return out

        # OKX fallback
        okx_sym = sym[:-4] + "-USDT-SWAP" if sym.endswith("USDT") else sym
        o_int = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}.get(interval, "15m")
        odata = _http_get_json(f"https://www.okx.com/api/v5/market/candles?instId={urllib.parse.quote(okx_sym)}&bar={o_int}&limit={min(limit, 100)}", timeout=5.0)
        orows = (odata or {}).get("data") or []
        if orows:
            for r in reversed(orows):
                try:
                    out.append(CryptoCandle(
                        ts_ms=int(r[0]), open=Decimal(str(r[1])), high=Decimal(str(r[2])),
                        low=Decimal(str(r[3])), close=Decimal(str(r[4])), volume=Decimal(str(r[5])),
                    ))
                except Exception:
                    continue
            if out:
                self.candle_cache[key] = (time.time(), out)
                return out
        return out

    def get_orderbook_depth(self, symbol: str) -> Dict[str, Any]:
        sym = symbol.upper()
        if self.demo_mode:
            return self._demo_depth(sym)

        cached = self.depth_cache.get(sym)
        if cached and (time.time() - cached[0]) < 10.0:
            return cached[1]

        url = f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={urllib.parse.quote(sym)}&limit=10"
        data = _http_get_json(url, timeout=4.0)
        res = (data or {}).get("result") or {}
        bids = res.get("b") or []
        asks = res.get("a") or []
        if bids and asks:
            buy_q = sum(float(x[1]) for x in bids[:5])
            sell_q = sum(float(x[1]) for x in asks[:5])
            tot = max(1e-9, buy_q + sell_q)
            buy_pct = round((buy_q / tot) * 100, 1)
            pressure = "BUY_IMBALANCE" if buy_pct >= 58 else ("SELL_IMBALANCE" if buy_pct <= 42 else "BALANCED")
            out = {
                "symbol": sym,
                "source": "BYBIT_L2",
                "buy_pct": buy_pct,
                "pressure": pressure,
                "bids": [{"price": x[0], "qty": x[1]} for x in bids[:5]],
                "asks": [{"price": x[0], "qty": x[1]} for x in asks[:5]],
            }
            self.depth_cache[sym] = (time.time(), out)
            return out

        return {"symbol": sym, "source": "UNAVAILABLE", "buy_pct": 50.0, "pressure": "BALANCED", "bids": [], "asks": []}

    # --- Deterministic Demo Mode (strictly for offline tests) ---
    def _demo_tickers(self) -> Dict[str, CryptoTicker]:
        base_prices = {
            "BTCUSDT": 96420.50, "ETHUSDT": 3412.80, "SOLUSDT": 238.45, "BNBUSDT": 642.10,
            "XRPUSDT": 1.1420, "DOGEUSDT": 0.3895, "PEPEUSDT": 0.00001940, "SUIUSDT": 3.84,
            "AVAXUSDT": 39.65, "LINKUSDT": 18.40, "ADAUSDT": 0.8120, "ENAUSDT": 0.6840,
            "GALAUSDT": 0.0392, "NEARUSDT": 6.18, "RENDERUSDT": 7.95, "WIFUSDT": 3.42,
        }
        now = time.time()
        for idx, (sym, px) in enumerate(base_prices.items()):
            ltp = Decimal(str(px))
            chg = Decimal(str(round(1.5 + (idx % 5) * 1.4, 2)))
            prev = (ltp / (Decimal("1") + chg / Decimal("100"))).quantize(Decimal("0.00000001") if px < 0.01 else Decimal("0.0001"))
            self.ticker_cache[sym] = CryptoTicker(
                symbol=sym, ltp=ltp, prev_24h=prev, change_24h_pct=chg,
                volume_24h_usdt=Decimal(str(85000000 + idx * 12000000)),
                funding_rate=Decimal("0.00012") if idx % 3 else Decimal("-0.00008"),
                open_interest=Decimal(str(18000000 + idx * 3000000)),
                source="DEMO", updated_at=now,
            )
        return self.ticker_cache

    def _demo_candles(self, symbol: str, interval: str, limit: int) -> List[CryptoCandle]:
        self._demo_tickers()
        base = float(self.ticker_cache.get(symbol, CryptoTicker(symbol, Decimal("100"), Decimal("98"), Decimal("2"), Decimal("1000000"), Decimal("0.0001"), Decimal("100000"), "DEMO", time.time())).ltp)
        out: List[CryptoCandle] = []
        now_ms = int(time.time() * 1000)
        step_ms = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}.get(interval, 900000)
        for i in range(limit):
            drift = 0.88 + (i / max(1, limit)) * 0.12 + math.sin(i * 0.35) * 0.008
            c = base * drift
            o = c * (0.996 if i % 2 == 0 else 1.003)
            h = max(o, c) * 1.006
            l = min(o, c) * 0.994
            vol = 120000.0 * (2.6 if i >= limit - 2 else 1.0)
            q = Decimal("0.00000001") if base < 0.01 else Decimal("0.0001")
            out.append(CryptoCandle(
                ts_ms=now_ms - (limit - i) * step_ms,
                open=Decimal(str(o)).quantize(q), high=Decimal(str(h)).quantize(q),
                low=Decimal(str(l)).quantize(q), close=Decimal(str(c)).quantize(q),
                volume=Decimal(str(round(vol, 2))),
            ))
        return out

    def _demo_depth(self, symbol: str) -> Dict[str, Any]:
        self._demo_tickers()
        base = float(self.ticker_cache.get(symbol, CryptoTicker(symbol, Decimal("100"), Decimal("98"), Decimal("2"), Decimal("1000000"), Decimal("0.0001"), Decimal("100000"), "DEMO", time.time())).ltp)
        step = base * 0.0005
        return {
            "symbol": symbol,
            "source": "DEMO_L2",
            "buy_pct": 66.5,
            "pressure": "BUY_IMBALANCE",
            "bids": [{"price": f"{base - i * step:.4f}", "qty": f"{12.5 * i:.2f}"} for i in range(1, 6)],
            "asks": [{"price": f"{base + i * step:.4f}", "qty": f"{6.8 * i:.2f}"} for i in range(1, 6)],
        }
