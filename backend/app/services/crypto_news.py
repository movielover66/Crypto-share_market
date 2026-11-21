"""
TRADEIQ Crypto v1.0 — 24/7 Macro & Catalyst News Radar (Musk / Trump / Fed / SEC / Crypto RSS)
+ Category-Themed Telegram Notifier (Clean 3-Line HTML Bubbles + Real-Time Dark Obsidian PnL Image Cards).
Category Color Themes:
  - PERP_WIN   : Emerald Neon Green (#00E676)
  - PERP_LOSS  : Crimson Red (#FF3B5C)
  - BOOM       : Electric Purple (#A855F7)
  - SPOT       : Royal Gold (#F5B700)
  - SUMMARY    : Sapphire Cyan (#00D4FF)
"""
import datetime
import html
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple


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


def _load_font(size: int, bold: bool = True, mono: bool = False) -> Any:
    from PIL import ImageFont

    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        if mono
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render_pnl_card_png(
    category: str,
    badge_text: str,
    symbol: str,
    sub_pill: str,
    big_pnl: str,
    sub_pnl: str,
    col1: Tuple[str, str],
    col2: Tuple[str, str],
    col3: Tuple[str, str],
    ts_label: str = "",
) -> Optional[bytes]:
    """Renders a sleek 960x540 dark-obsidian Binance/OKX style PNG card with distinct category colors."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    themes: Dict[str, Dict[str, Tuple[int, int, int]]] = {
        "PERP_WIN":  {"accent": (0, 230, 118),  "glow": (12, 58, 38),  "badge_bg": (245, 183, 0),  "badge_fg": (10, 14, 22)},
        "PERP_LOSS": {"accent": (255, 59, 92),  "glow": (68, 16, 28),  "badge_bg": (255, 59, 92),  "badge_fg": (255, 255, 255)},
        "BOOM_WIN":  {"accent": (192, 132, 252),"glow": (52, 20, 82),  "badge_bg": (168, 85, 247), "badge_fg": (255, 255, 255)},
        "BOOM_LOSS": {"accent": (255, 82, 119), "glow": (64, 18, 56),  "badge_bg": (168, 85, 247), "badge_fg": (255, 255, 255)},
        "SPOT_WIN":  {"accent": (245, 183, 0),  "glow": (66, 48, 10),  "badge_bg": (245, 183, 0),  "badge_fg": (10, 14, 22)},
        "SPOT_EXIT": {"accent": (0, 212, 255),  "glow": (10, 48, 66),  "badge_bg": (0, 212, 255),  "badge_fg": (10, 14, 22)},
    }
    th = themes.get(category, themes["PERP_WIN"])
    acc = th["accent"]
    glow = th["glow"]

    W, H = 960, 540
    img = Image.new("RGB", (W, H), (6, 9, 15))
    draw = ImageDraw.Draw(img)

    # Subtle radial glow band behind center + geometric candlestick lines
    for r in range(280, 20, -14):
        alpha = max(0.0, 1.0 - (r / 280.0)) * 0.55
        c = (
            int(6 + (glow[0] - 6) * alpha),
            int(9 + (glow[1] - 9) * alpha),
            int(15 + (glow[2] - 15) * alpha),
        )
        draw.ellipse((W // 2 - r * 1.4, H // 2 - r * 0.9, W // 2 + r * 1.4, H // 2 + r * 0.9), fill=c)

    # Background subtle grid & mini candlestick silhouettes
    for x in range(80, W, 110):
        draw.line([(x, 40), (x, H - 40)], fill=(18, 25, 38), width=1)
    for y in range(110, H - 110, 85):
        draw.line([(40, y), (W - 40, y)], fill=(18, 25, 38), width=1)

    # Outer sleek border + top colored accent stripe
    draw.rounded_rectangle([(18, 18), (W - 18, H - 18)], radius=24, outline=(36, 48, 72), width=2)
    draw.rounded_rectangle([(42, 18), (W - 42, 24)], radius=3, fill=acc)

    f_brand = _load_font(28, bold=True)
    f_badge = _load_font(18, bold=True)
    f_sym = _load_font(48, bold=True)
    f_pill = _load_font(20, bold=True)
    f_big = _load_font(84, bold=True, mono=True)
    f_sub = _load_font(26, bold=False, mono=True)
    f_lbl = _load_font(17, bold=True)
    f_val = _load_font(28, bold=True, mono=True)
    f_ts = _load_font(16, bold=False)

    # Header: TRADEIQ PRO + Category Badge + Timestamp
    draw.text((48, 46), "TRADE", fill=(248, 250, 252), font=f_brand)
    draw.text((152, 46), "IQ PRO", fill=acc, font=f_brand)

    bb = draw.textbbox((0, 0), badge_text, font=f_badge)
    bw = (bb[2] - bb[0]) + 28
    draw.rounded_rectangle([(278, 44), (278 + bw, 78)], radius=10, fill=th["badge_bg"])
    draw.text((292, 50), badge_text, fill=th["badge_fg"], font=f_badge)

    if ts_label:
        tb = draw.textbbox((0, 0), ts_label, font=f_ts)
        draw.text((W - 48 - (tb[2] - tb[0]), 54), ts_label, fill=(148, 163, 184), font=f_ts)

    # Center: Symbol + Mode Pill
    sb = draw.textbbox((0, 0), symbol, font=f_sym)
    pb = draw.textbbox((0, 0), sub_pill, font=f_pill)
    sw = sb[2] - sb[0]
    pw = (pb[2] - pb[0]) + 26
    total_top = sw + 18 + pw
    start_x = (W - total_top) // 2
    draw.text((start_x, 122), symbol, fill=(255, 255, 255), font=f_sym)
    draw.rounded_rectangle([(start_x + sw + 18, 134), (start_x + sw + 18 + pw, 168)], radius=10, outline=acc, width=2, fill=(14, 21, 37))
    draw.text((start_x + sw + 31, 139), sub_pill, fill=acc, font=f_pill)

    # Giant PnL numeral
    gb = draw.textbbox((0, 0), big_pnl, font=f_big)
    gw = gb[2] - gb[0]
    draw.text(((W - gw) // 2, 192), big_pnl, fill=acc, font=f_big)

    # Subtitle (% + INR)
    sub_b = draw.textbbox((0, 0), sub_pnl, font=f_sub)
    sub_w = sub_b[2] - sub_b[0]
    draw.text(((W - sub_w) // 2, 296), sub_pnl, fill=(203, 213, 225), font=f_sub)

    # Bottom 3-Column Metrics Bar
    draw.rounded_rectangle([(48, 376), (W - 48, 492)], radius=18, fill=(14, 20, 34), outline=(36, 49, 74), width=2)
    cols = [col1, col2, col3]
    col_w = (W - 96) // 3
    for idx, (lbl, val) in enumerate(cols):
        cx = 48 + idx * col_w + col_w // 2
        if idx > 0:
            draw.line([(48 + idx * col_w, 396), (48 + idx * col_w, 472)], fill=(30, 41, 59), width=1)
        lb = draw.textbbox((0, 0), lbl, font=f_lbl)
        vb = draw.textbbox((0, 0), val, font=f_val)
        draw.text((cx - (lb[2] - lb[0]) // 2, 396), lbl, fill=(148, 163, 184), font=f_lbl)
        val_col = acc if idx == 1 else (248, 250, 252)
        draw.text((cx - (vb[2] - vb[0]) // 2, 428), val, fill=val_col, font=f_val)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


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

        risk_words = (
            "hack", "exploit", "drain", "delist", "bankrupt", "lawsuit", "sec sues", "crash", "rug",
            "plunge", "tumble", "slump", "slide", "sell-off", "selloff", "outflow", "liquidat",
            "rate hike", "hawkish", "bearish", "extreme fear", "drops below", "falls below", "slips below",
            "panic", "wipeout", "bleed", "correction", "tariff", "inflation fears",
        )
        bull_words = (
            "surge", "rally", "approv", "etf inflow", "inflow", "adopt", "launch", "partnership",
            "breakout", "rate cut", "dovish", "bullish", "bull", "rebound", "recover", "soar",
            "all-time high", "ath", "accumulat", "whale buy", "strategic reserve", "record high",
        )
        if any(w in tl for w in risk_words):
            impact = "HIGH_RISK"
        elif any(w in tl for w in bull_words):
            impact = "BULLISH_CATALYST"
        else:
            impact = "NEUTRAL"
        return {"tag": tag, "mapped_coins": mapped, "impact": impact}


class CryptoTelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = "", app_url: str = "") -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.app_url = (app_url or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        self.enabled = bool(self.bot_token and self.chat_id)
        self.last_error: Optional[str] = None

    def _reply_markup(self, btn_label: str = "Open Desk") -> Optional[Dict[str, Any]]:
        if not self.app_url.startswith("http"):
            return None
        return {"inline_keyboard": [[{"text": btn_label, "url": self.app_url}]]}

    def send_html(
        self,
        html_text: str,
        btn_label: str = "Open Desk",
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"delivered": False, "reason": "NOT_CONFIGURED"}
        body: Dict[str, Any] = {"chat_id": self.chat_id, "text": html_text, "parse_mode": "HTML"}
        if reply_to_message_id:
            body["reply_to_message_id"] = int(reply_to_message_id)
            body["allow_sending_without_reply"] = True
        rm = self._reply_markup(btn_label)
        if rm:
            body["reply_markup"] = rm
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                ok = 200 <= resp.status < 300
                raw = resp.read()
                msg_id = None
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    msg_id = parsed.get("result", {}).get("message_id")
                except Exception:
                    pass
                self.last_error = None if ok else f"HTTP_{resp.status}"
                return {"delivered": ok, "message_id": msg_id}
        except Exception as exc:
            self.last_error = type(exc).__name__
            return {"delivered": False, "error": self.last_error}

    def send_photo_bytes(
        self,
        png_bytes: bytes,
        caption_html: str,
        btn_label: str = "Open Desk",
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.enabled:
            return {"delivered": False, "reason": "NOT_CONFIGURED"}
        boundary = f"----TradeIQBoundary{uuid.uuid4().hex}"
        parts: List[bytes] = []

        def _add_field(name: str, value: str) -> None:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
            )

        _add_field("chat_id", self.chat_id)
        _add_field("caption", caption_html)
        _add_field("parse_mode", "HTML")
        if reply_to_message_id:
            _add_field("reply_to_message_id", str(int(reply_to_message_id)))
            _add_field("allow_sending_without_reply", "true")
        rm = self._reply_markup(btn_label)
        if rm:
            _add_field("reply_markup", json.dumps(rm))

        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"tradeiq_card.png\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8")
            + png_bytes
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        data = b"".join(parts)

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendPhoto",
            data=data,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                ok = 200 <= resp.status < 300
                raw = resp.read()
                msg_id = None
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    msg_id = parsed.get("result", {}).get("message_id")
                except Exception:
                    pass
                self.last_error = None if ok else f"HTTP_{resp.status}"
                return {"delivered": ok, "photo": True, "message_id": msg_id}
        except Exception as exc:
            self.last_error = type(exc).__name__
            return self.send_html(caption_html, btn_label=btn_label, reply_to_message_id=reply_to_message_id)

    def send_pnl_card(
        self,
        category: str,
        badge_text: str,
        symbol: str,
        sub_pill: str,
        big_pnl: str,
        sub_pnl: str,
        col1: Tuple[str, str],
        col2: Tuple[str, str],
        col3: Tuple[str, str],
        caption_html: str,
        ts_label: str = "",
        btn_label: str = "Open Desk",
        reply_to_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Generates a category-colored Dark Obsidian PNG card and sends via sendPhoto (graceful HTML fallback)."""
        if not self.enabled:
            return {"delivered": False, "reason": "NOT_CONFIGURED"}
        png = render_pnl_card_png(category, badge_text, symbol, sub_pill, big_pnl, sub_pnl, col1, col2, col3, ts_label)
        if png:
            return self.send_photo_bytes(png, caption_html, btn_label=btn_label, reply_to_message_id=reply_to_message_id)
        return self.send_html(caption_html, btn_label=btn_label, reply_to_message_id=reply_to_message_id)

    def send_alert(self, title: str, body: str, ts_label: str = "") -> Dict[str, Any]:
        """Backward-compatible clean HTML alert helper (Sapphire/Cyan summary & test style)."""
        txt = f"🔷 <b>{html.escape(title)}</b>\n<blockquote>{html.escape(body)}</blockquote>"
        if ts_label:
            txt += f"\n<i>🕒 {html.escape(ts_label)}</i>"
        return self.send_html(txt, btn_label="💻 Open Desk")


class GeminiTrapJudge:
    """
    Hybrid AI Stop-Hunt & Trap Veto Judge.
    Receives ONLY deterministic quantitative order-flow features (Footprint CVD, Sweep, 24h Exhaustion,
    Retail Long/Short Ratio, L2 Depth) and outputs APPROVE or VETO_TRAP.
    """

    def __init__(self, api_keys_csv: str = "") -> None:
        self.keys = [k.strip() for k in (api_keys_csv or "").split(",") if k.strip() and not k.strip().startswith("YOUR_")]
        self.enabled = bool(self.keys)
        self.last_verdict: Optional[str] = None

    def evaluate_trade(self, features: Dict[str, Any]) -> Dict[str, Any]:
        chg24 = float(features.get("change_24h_pct", 0.0))
        direction = str(features.get("direction", "LONG")).upper()
        dpct = float(features.get("footprint_delta_pct", 0.0))
        engine = str(features.get("engine_code", ""))

        if direction == "SHORT" and chg24 <= -3.5 and engine not in ("ENGINE_4_SHADOW_SWEEP", "ENGINE_5_CRASH_SHORT"):
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
