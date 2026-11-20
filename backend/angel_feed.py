"""
angel_feed.py  (put next to main.py in /backend)

Live NSE data from Angel One SmartAPI (free): login with TOTP, equity token lookup, daily candles, websocket ticks.
Env (set on Render, NEVER in code):
  ANGEL_API_KEY      - API key of your SmartAPI app (smartapi.angelone.in)
  ANGEL_CLIENT_CODE  - your Angel One client id
  ANGEL_PIN          - your login PIN / password
  ANGEL_TOTP_SECRET  - the TOTP secret (the text key shown when you enable TOTP), not the 6-digit code
The SDK (smartapi-python, pyotp) is imported lazily, so the rest of the app still starts if it is missing.
"""
import asyncio
import datetime
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_WS_TOKEN = "26000"          # Nifty 50 on the websocket feed (exchange type 1 = NSE cash)
NIFTY_HIST_TOKEN = "99926000"     # Nifty 50 for the historical-candle API
_MASTER_FILE = Path(__file__).resolve().parent / "angel_master_cache.json"


def configured() -> bool:
    return all(os.getenv(k, "").strip() for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"))


def ist_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))


def normalize_tick(msg: Any) -> Optional[Dict[str, Any]]:
    """Angel sends prices in paise: convert to rupees. Returns None for anything that is not a usable tick."""
    if not isinstance(msg, dict) or msg.get("last_traded_price") is None:
        return None
    try:
        tok = str(msg.get("token", "")).strip('"\x00 ')
        out: Dict[str, Any] = {"token": tok, "ltp": float(msg["last_traded_price"]) / 100.0}
        for src, dst in (("average_traded_price", "avg"), ("open_price_of_the_day", "open"),
                         ("high_price_of_the_day", "high"), ("low_price_of_the_day", "low"),
                         ("closed_price", "prev_close")):
            if msg.get(src) is not None:
                out[dst] = float(msg[src]) / 100.0
        if msg.get("volume_trade_for_the_day") is not None:
            out["vol"] = float(msg["volume_trade_for_the_day"])
    except (TypeError, ValueError):
        return None
    return out if tok and out["ltp"] > 0 else None


_OBJ = re.compile(r"\{[^{}]*\}")                      # every instrument is one flat {...} object
_NSE = re.compile(r'"exch_seg"\s*:\s*"NSE"')
_EQ = re.compile(r'"symbol"\s*:\s*"[^"]*-EQ"')


def extract_equities(piece: str, out: Dict[str, str]) -> None:
    for m in _OBJ.finditer(piece):
        o = m.group(0)
        if _NSE.search(o) and _EQ.search(o):
            try:
                d = json.loads(o)
                out[str(d.get("name", "")).upper()] = str(d["token"])
            except (ValueError, KeyError):
                continue


async def load_equity_tokens(client: httpx.AsyncClient, wanted: List[str]) -> Dict[str, str]:
    """{'RELIANCE': '2885', ...} for NSE equity (-EQ) symbols.
    The instrument file is ~30 MB. It is streamed and scanned piece by piece (never fully parsed), because a full
    json.loads can run a small 512 MB server out of memory. The small result is cached once per day."""
    today = ist_now().strftime("%Y-%m-%d")
    full: Optional[Dict[str, str]] = None
    try:
        d = json.loads(_MASTER_FILE.read_text(encoding="utf-8"))
        if d.get("date") == today and isinstance(d.get("map"), dict) and d["map"]:
            full = d["map"]
    except Exception:
        pass
    if full is None:
        full, buf = {}, ""
        async with client.stream("GET", MASTER_URL, timeout=httpx.Timeout(120.0, connect=15.0)) as r:
            if r.status_code != 200:
                raise RuntimeError(f"instrument list HTTP {r.status_code}")
            async for chunk in r.aiter_text():
                buf += chunk
                cut = buf.rfind("}")
                if cut == -1:
                    continue
                piece, buf = buf[:cut + 1], buf[cut + 1:]
                extract_equities(piece, full)
        if not full:
            raise RuntimeError("instrument list had no NSE equities (format changed?)")
        try:
            _MASTER_FILE.write_text(json.dumps({"date": today, "map": full}), encoding="utf-8")
        except Exception:
            pass
    return {s: full[s.upper()] for s in wanted if s.upper() in full}


class AngelFeed:
    def __init__(self) -> None:
        self.smart: Any = None
        self.auth: Dict[str, str] = {}
        self.sws: Any = None
        self.thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_tick = 0.0
        self.n_ticks = 0
        self.error = ""

    # ---- blocking calls: run them with asyncio.to_thread
    def login(self) -> None:
        from SmartApi import SmartConnect      # lazy import
        import pyotp
        key, code = os.getenv("ANGEL_API_KEY", "").strip(), os.getenv("ANGEL_CLIENT_CODE", "").strip()
        pin, secret = os.getenv("ANGEL_PIN", "").strip(), os.getenv("ANGEL_TOTP_SECRET", "").replace(" ", "").strip()
        s = SmartConnect(api_key=key)
        res = s.generateSession(code, pin, pyotp.TOTP(secret).now())
        if not isinstance(res, dict) or not res.get("status"):
            msg = res.get("message") if isinstance(res, dict) else res
            raise RuntimeError(f"Angel login failed: {str(msg)[:120]}")
        d = res["data"]
        self.smart = s
        self.auth = {"jwt": d["jwtToken"], "feed": d.get("feedToken") or s.getfeedToken(), "api_key": key,
                     "client": code}

    def historical(self, token: str, interval: str, frm: str, to: str) -> List[List[Any]]:
        """Rows of [timestamp, open, high, low, close, volume]. frm/to like '2026-10-01 09:15'."""
        if not self.smart:
            return []
        p = {"exchange": "NSE", "symboltoken": token, "interval": interval, "fromdate": frm, "todate": to}
        for attempt in range(2):
            try:
                res = self.smart.getCandleData(p)
                if isinstance(res, dict) and res.get("data"):
                    return res["data"]
                if isinstance(res, dict) and "rate" in str(res.get("message", "")).lower():
                    time.sleep(2.0)
                    continue
                return []
            except Exception:
                time.sleep(1.0)
        return []

    def start(self, stocks: List[str], indices: List[str], on_tick: Callable[[Dict[str, Any]], None]) -> None:
        """Start the websocket in a background thread. on_tick is called from that thread with normalized ticks."""
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        a = self.auth
        sws = SmartWebSocketV2(a["jwt"], a["api_key"], a["client"], a["feed"], max_retry_attempt=5)

        def on_open(*_: Any) -> None:
            self.connected = True
            self.error = ""
            for i in range(0, len(stocks), 100):          # mode 2 = quote: ltp, volume, vwap, ohlc
                sws.subscribe("apex_q", 2, [{"exchangeType": 1, "tokens": stocks[i:i + 100]}])
            if indices:                                    # mode 1 = ltp only
                sws.subscribe("apex_i", 1, [{"exchangeType": 1, "tokens": indices}])

        def on_data(*args: Any) -> None:
            t = normalize_tick(args[-1] if args else None)
            if t:
                self.last_tick = time.time()
                self.n_ticks += 1
                try:
                    on_tick(t)
                except Exception as exc:                   # a bad tick must never kill the socket thread
                    self.error = f"tick handler: {type(exc).__name__}"

        def on_error(*args: Any) -> None:
            self.error = str(args[-1] if args else "websocket error")[:160]

        def on_close(*_: Any) -> None:
            self.connected = False

        sws.on_open, sws.on_data, sws.on_error, sws.on_close = on_open, on_data, on_error, on_close
        self.sws = sws

        def run() -> None:
            try:
                sws.connect()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:160]
            finally:
                self.connected = False

        self.thread = threading.Thread(target=run, name="angel-ws", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        try:
            if self.sws is not None:
                self.sws.close_connection()
        except Exception:
            pass
        self.connected = False
        self.sws = None
