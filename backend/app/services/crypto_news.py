"""
TRADEIQ Crypto v1.0 — 24/7 Macro & Catalyst News Radar (Musk / Trump / Fed / SEC / Crypto RSS)
+ Emoji-Formatted Telegram Elite Notifier.
"""
import datetime
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional


CRYPTO_RSS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Google Crypto Macro", "https://news.google.com/rss/search?q=(Bitcoin+OR+Dogecoin+OR+Solana+OR+Trump+crypto+OR+Elon+Musk+crypto+OR+SEC+ETF)+when:1d&hl=en-US&gl=US&ceid=US:en"),
]

CATALYST_RULES = [
    ("ELON MUSK / X", ("elon musk", "musk", "dogecoin", "doge", "x payments"), ["DOGEUSDT", "BTCUSDT"]),
    ("DONALD TRUMP / POLICY", ("trump", "white house", "tariff", "strategic reserve", "treasury"), ["BTCUSDT", "SOLUSDT", "XRPUSDT"]),
    ("US FED / MACRO", ("fed ", "fomc", "cpi", "inflation", "rate cut", "powell"), ["BTCUSDT", "ETHUSDT"]),
    ("SEC / ETF", ("sec ", "etf", "blackrock", "ripple", "solana etf"), ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]),
]


class CryptoCatalystNewsService:
    def __init__(self, demo_mode: bool = False) -> None:
        self.demo_mode = demo_mode
        self.items: List[Dict[str, Any]] = []
        self.last_refresh_ts: float = 0.0

    def refresh_news(self, force: bool = False) -> List[Dict[str, Any]]:
        if not force and self.items and (time.time() - self.last_refresh_ts) < 300:
            return self.items
        if self.demo_mode:
            self.items = [
                {
                    "tag": "ELON MUSK / X",
                    "headline": "X Payments development fuels renewed Dogecoin & Bitcoin liquidity surge",
                    "source": "CoinDesk",
                    "url": "https://www.coindesk.com",
                    "published_ist": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "mapped_coins": ["DOGEUSDT", "BTCUSDT"],
                    "impact": "BULLISH_CATALYST",
                    "epoch": time.time(),
                },
                {
                    "tag": "DONALD TRUMP / POLICY",
                    "headline": "US crypto regulatory framework discussions boost major layer-1 sentiment",
                    "source": "CoinTelegraph",
                    "url": "https://cointelegraph.com",
                    "published_ist": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "mapped_coins": ["BTCUSDT", "SOLUSDT", "XRPUSDT"],
                    "impact": "BULLISH_CATALYST",
                    "epoch": time.time() - 600,
                },
            ]
            self.last_refresh_ts = time.time()
            return self.items

        collected: List[Dict[str, Any]] = []
        seen: set = set()
        now_ts = time.time()
        for src_name, url in CRYPTO_RSS_FEEDS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "TRADEIQ-Crypto/1.0"})
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    xml_bytes = resp.read()
                root = ET.fromstring(xml_bytes)
                for item in root.findall(".//item")[:10]:
                    title = html.unescape((item.findtext("title") or "").strip())
                    link = (item.findtext("link") or "").strip()
                    pub = (item.findtext("pubDate") or "").strip()
                    if not title or title.lower() in seen:
                        continue
                    seen.add(title.lower())
                    epoch = now_ts
                    if pub:
                        try:
                            epoch = parsedate_to_datetime(pub).timestamp()
                        except Exception:
                            pass
                    if (now_ts - epoch) > 7 * 86400:
                        continue
                    classified = self._classify_headline(title)
                    collected.append({
                        "tag": classified["tag"],
                        "headline": re.sub(r"<[^>]+>", "", title)[:220],
                        "source": src_name,
                        "url": link,
                        "published_ist": pub[:25],
                        "mapped_coins": classified["mapped_coins"],
                        "impact": classified["impact"],
                        "epoch": epoch,
                    })
            except Exception:
                continue

        collected.sort(key=lambda x: x.get("epoch", 0), reverse=True)
        if collected:
            self.items = collected[:25]
            self.last_refresh_ts = now_ts
        return self.items

    @staticmethod
    def _classify_headline(title: str) -> Dict[str, Any]:
        tl = title.lower()
        tag = "CRYPTO MARKET"
        mapped = ["BTCUSDT"]
        for tname, kws, coins in CATALYST_RULES:
            if any(k in tl for k in kws):
                tag = tname
                mapped = coins
                break

        risk_words = ("hack", "exploit", "drain", "delist", "bankrupt", "lawsuit", "sec sues", "crash", "rug")
        bull_words = ("surge", "rally", "approv", "etf inflow", "adopt", "launch", "partnership", "breakout", "rate cut", "bull")
        if any(w in tl for w in risk_words):
            impact = "HIGH_RISK"
        elif any(w in tl for w in bull_words):
            impact = "BULLISH_CATALYST"
        else:
            impact = "NEUTRAL"
        return {"tag": tag, "mapped_coins": mapped, "impact": impact}


class CryptoTelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = "") -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = bool(self.bot_token and self.chat_id)
        self.last_error: Optional[str] = None

    def send_alert(self, title: str, body: str, ts_label: str = "") -> Dict[str, Any]:
        if not self.enabled:
            return {"delivered": False, "reason": "NOT_CONFIGURED"}
        text = f"<b>TRADEIQ CRYPTO [PAPER] — {html.escape(title)}</b>\n\n{html.escape(body)}"
        if ts_label:
            text += f"\n\n<i>🕒 {html.escape(ts_label)}</i>"
        payload = json.dumps({"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                ok = 200 <= resp.status < 300
                self.last_error = None if ok else f"HTTP_{resp.status}"
                return {"delivered": ok}
        except Exception as exc:
            self.last_error = type(exc).__name__
            return {"delivered": False, "error": self.last_error}


class GeminiTrapJudge:
    """
    Hybrid AI Stop-Hunt & Trap Veto Judge.
    Receives ONLY deterministic quantitative order-flow features (Footprint CVD, Sweep, 24h Exhaustion,
    Retail Long/Short Ratio, L2 Depth) and outputs APPROVE or VETO_TRAP.
    Fails open to deterministic trap veto if Gemini API key is missing or offline.
    """

    def __init__(self, api_keys_csv: str = "") -> None:
        self.keys = [k.strip() for k in (api_keys_csv or "").split(",") if k.strip() and not k.strip().startswith("YOUR_")]
        self.enabled = bool(self.keys)
        self.last_verdict: Optional[str] = None

    def evaluate_trade(self, features: Dict[str, Any]) -> Dict[str, Any]:
        # Deterministic fast trap check first (always active even without Gemini key)
        chg24 = float(features.get("change_24h_pct", 0.0))
        direction = str(features.get("direction", "LONG")).upper()
        dpct = float(features.get("footprint_delta_pct", 0.0))
        engine = str(features.get("engine_code", ""))

        if direction == "SHORT" and chg24 <= -3.5 and engine != "ENGINE_4_SHADOW_SWEEP":
            return {"verdict": "VETO_TRAP", "reason": f"Exhaustion trap: already down {chg24:.2f}% in 24h (bounce risk).", "source": "DETERMINISTIC_GUARD"}
        if direction == "LONG" and chg24 >= 8.0 and engine not in ("BOOM_RADAR", "ENGINE_4_SHADOW_SWEEP"):
            return {"verdict": "VETO_TRAP", "reason": f"Exhaustion trap: already up +{chg24:.2f}% in 24h (chase risk).", "source": "DETERMINISTIC_GUARD"}
        if direction == "SHORT" and dpct >= 18.0:
            return {"verdict": "VETO_TRAP", "reason": f"Footprint Buy Absorption (+{dpct}% delta) fighting SHORT.", "source": "FOOTPRINT_GUARD"}
        if direction == "LONG" and dpct <= -18.0:
            return {"verdict": "VETO_TRAP", "reason": f"Footprint Sell Absorption ({dpct}% delta) fighting LONG.", "source": "FOOTPRINT_GUARD"}

        if not self.enabled:
            return {"verdict": "APPROVE", "reason": "Passed deterministic order-flow & exhaustion guard.", "source": "DETERMINISTIC_GUARD"}

        prompt = (
            "You are an institutional crypto order-flow risk judge. Evaluate whether this trade is a retail stop-hunt trap. "
            'Respond ONLY with valid JSON: {"verdict":"APPROVE" or "VETO_TRAP", "reason":"<15 words>"}.\n'
            f"Data: {json.dumps(features)}"
        )
        for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={urllib.parse.quote(self.keys[0])}"
            body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                txt = raw["candidates"][0]["content"]["parts"][0]["text"]
                m = re.search(r"\{[^}]+\}", txt)
                if m:
                    parsed = json.loads(m.group(0))
                    v = str(parsed.get("verdict", "APPROVE")).upper()
                    if v in ("APPROVE", "VETO_TRAP"):
                        self.last_verdict = f"{features.get('symbol')}: {v}"
                        return {"verdict": v, "reason": str(parsed.get("reason", ""))[:120], "source": f"GEMINI_{model}"}
            except Exception:
                continue
        return {"verdict": "APPROVE", "reason": "Passed deterministic guard (Gemini fallback).", "source": "DETERMINISTIC_FALLBACK"}
