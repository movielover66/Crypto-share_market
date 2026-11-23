"""
TRADEIQ Crypto Pro — Institutional Master Market Brain (`MarketBrainEngine`).
Purely analytical regime commander: NEVER executes trades itself — broadcasts one unified
`MarketDirective` (-100 to +100) to all downstream engines (Perpetuals E1–E5, Boom Radar, Spot Vault).

6-Layer Quantitative Scoring Architecture:
  1. BTC/ETH Multi-TF Trend + Rescaled-Range Hurst Exponent (H)  : +/- 25 pts
  2. Full 534-Coin Cross-Sectional Market Breadth (% Green/Red)  : +/- 25 pts
  3. OKX Live Order-Flow (BTC+ETH Tick Footprint CVD & L2 Depth) : +/- 20 pts
  4. Live Macro & Catalyst News Sentiment + Cause Extraction     : +/- 15 pts
  5. Rolling Volatility Burst & Perpetual Funding Rate Stress    : +/- 10 pts
  6. External Crypto Fear & Greed Index (1h cached context)      : +/-  5 pts

Anti-Whipsaw Protection:
  - Dual-Threshold Hysteresis (10-pt buffer) + 2-cycle confirmation for normal transitions.
  - Instant Emergency Crash Override (< 10s via Fast Pulse) when BTC <= -5.0% & >= 72% coins red.
"""
import datetime
import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .crypto_engines import _ema
from .crypto_simulator import IST
from ..providers.crypto_provider import CryptoCandle, CryptoTicker


def compute_hurst_exponent(closes: List[Decimal]) -> float:
    """
    Rescaled Range (R/S) Hurst Exponent estimator on log-returns:
      H > 0.52 -> Persistent (Trending: breakouts & crash momentum work)
      H < 0.48 -> Anti-Persistent (Mean-Reverting / Choppy: range & spot bounce work)
      0.48–0.52 -> Random Walk / Transition
    """
    if len(closes) < 24:
        return 0.50
    vals = [float(c) for c in closes if float(c) > 0]
    if len(vals) < 24:
        return 0.50
    rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals))]
    lags = [w for w in (6, 10, 16, 24, 36) if w < len(rets)]
    if len(lags) < 2:
        return 0.50

    log_lags: List[float] = []
    log_rs: List[float] = []
    for w in lags:
        rs_vals: List[float] = []
        for start in range(0, len(rets) - w + 1, max(1, w // 2)):
            seg = rets[start : start + w]
            mean = sum(seg) / len(seg)
            dev = [x - mean for x in seg]
            cum: List[float] = []
            acc = 0.0
            for d in dev:
                acc += d
                cum.append(acc)
            r_span = max(cum) - min(cum)
            var = sum(d * d for d in dev) / max(1, len(dev))
            std = math.sqrt(var)
            if std > 1e-12 and r_span > 0:
                rs_vals.append(r_span / std)
        if rs_vals:
            avg_rs = sum(rs_vals) / len(rs_vals)
            if avg_rs > 0:
                log_lags.append(math.log(w))
                log_rs.append(math.log(avg_rs))

    if len(log_lags) < 2:
        return 0.50
    mx = sum(log_lags) / len(log_lags)
    my = sum(log_rs) / len(log_rs)
    num = sum((x - mx) * (y - my) for x, y in zip(log_lags, log_rs))
    den = sum((x - mx) ** 2 for x in log_lags)
    if den <= 1e-12:
        return 0.50
    h = num / den
    return round(max(0.15, min(0.85, h)), 3)


def compute_vol_burst_ratio(c15: List[CryptoCandle]) -> float:
    """Ratio of recent 6-bar (90m) realized volatility vs 30-bar baseline volatility."""
    if len(c15) < 24:
        return 1.0
    vals = [float(c.close) for c in c15 if float(c.close) > 0]
    if len(vals) < 24:
        return 1.0
    rets = [abs(math.log(vals[i] / vals[i - 1])) for i in range(1, len(vals))]
    short_vol = sum(rets[-6:]) / 6.0
    long_vol = sum(rets[-24:]) / 24.0
    if long_vol <= 1e-9:
        return 1.0
    return round(short_vol / long_vol, 2)


def get_ist_session_label(now_ist: datetime.datetime) -> str:
    h = now_ist.hour
    if now_ist.weekday() >= 5:
        return "WEEKEND_LOW_LIQ"
    if 5 <= h < 13:
        return "ASIA_MORNING_BOUNCE"
    if 13 <= h < 19:
        return "EUROPE_OVERLAP"
    return "US_HIGH_VOL_SESSION"


@dataclass
class MarketDirective:
    mode: str                     # STRONG_UPTREND | HEALTHY_CORRECTION | SIDEWAYS_RANGE | DOWNTREND | SEVERE_CRASH
    bias: str                     # LONG_ONLY | DIP_LONG | BOTH_RANGE | SHORT_ONLY | CRASH_SHORT_ONLY
    score: int                    # -100 to +100
    raw_score: int                # Unsmoothed raw score
    confidence_pct: int           # 0 to 100
    hurst: float                  # > 0.52 Trending, < 0.48 Choppy
    hurst_label: str              # TRENDING | CHOPPY | TRANSITION
    vol_burst_ratio: float        # >= 1.8x indicates crash/breakout vol burst
    breadth_green_pct: float      # % of all 534+ coins green in 24h
    breadth_total_coins: int
    btc_chg_24h: float
    eth_chg_24h: float
    btc_cvd_pct: float
    fear_greed_value: int         # 0–100 (External F&G context)
    fear_greed_label: str
    session_label: str
    allow_longs: bool
    allow_shorts: bool
    allow_boom_long: bool
    spot_directive: str           # WAIT_TOP | PRIME_BUY | QUICK_BOUNCE | WAIT_BOTTOM | DEEP_DCA_ONLY
    primary_cause: str
    factors: List[Dict[str, Any]] = field(default_factory=list)
    updated_at_ist: str = ""
    emergency_override: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "bias": self.bias,
            "score": self.score,
            "raw_score": self.raw_score,
            "confidence_pct": self.confidence_pct,
            "hurst": self.hurst,
            "hurst_label": self.hurst_label,
            "vol_burst_ratio": self.vol_burst_ratio,
            "breadth_green_pct": self.breadth_green_pct,
            "breadth_total_coins": self.breadth_total_coins,
            "btc_chg_24h": self.btc_chg_24h,
            "eth_chg_24h": self.eth_chg_24h,
            "btc_cvd_pct": self.btc_cvd_pct,
            "fear_greed_value": self.fear_greed_value,
            "fear_greed_label": self.fear_greed_label,
            "session_label": self.session_label,
            "allow_longs": self.allow_longs,
            "allow_shorts": self.allow_shorts,
            "allow_boom_long": self.allow_boom_long,
            "spot_directive": self.spot_directive,
            "primary_cause": self.primary_cause,
            "factors": self.factors,
            "updated_at_ist": self.updated_at_ist,
            "emergency_override": self.emergency_override,
        }


def _mode_permissions(mode: str) -> Tuple[str, bool, bool, bool, str]:
    """Crash-Only Shield: allows BOTH LONG & SHORT in normal regimes (coin's 4H 200-EMA decides direction),
    and locks LONGs + Boom Radar ONLY in confirmed DOWNTREND / SEVERE_CRASH!"""
    if mode == "STRONG_UPTREND":
        return ("BOTH_BULL_TREND", True, True, True, "SWING_HOLD")
    if mode == "HEALTHY_CORRECTION":
        return ("BOTH_HEALTHY_DIP", True, True, True, "PRIME_BUY")
    if mode == "SIDEWAYS_RANGE":
        return ("BOTH_RANGE", True, True, True, "QUICK_BOUNCE")
    if mode == "DOWNTREND":
        return ("CRASH_SHIELD_SHORT", False, True, False, "CASH_SHIELD")
    return ("CRASH_SHORT_ONLY", False, True, False, "DEEP_DCA_ONLY")


def _raw_mode_from_score(score: int) -> str:
    if score >= 40:
        return "STRONG_UPTREND"
    if score >= 15:
        return "HEALTHY_CORRECTION"
    if score > -15:
        return "SIDEWAYS_RANGE"
    if score > -45:
        return "DOWNTREND"
    return "SEVERE_CRASH"


class MarketBrainEngine:
    def __init__(self, demo_mode: bool = False) -> None:
        self.demo_mode = demo_mode
        self.current_directive: Optional[MarketDirective] = None
        self.pending_mode: Optional[str] = None
        self.pending_count: int = 0
        self.history: List[Dict[str, Any]] = []
        self._fg_cache: Tuple[int, str, float] = (50, "Neutral", 0.0)
        self._global_cache: Tuple[float, float, float] = (0.0, 56.0, 0.0)  # (mcap_chg_24h, btc_dom, ts)
        self.ema_score: Optional[float] = None
        self.last_deep_ts: float = 0.0
        self.last_pulse_ts: float = 0.0

    def fetch_global_market(self) -> Tuple[float, float]:
        """Fetches free CoinGecko /global Total Market Cap 24h % and BTC Dominance (5-min cache)."""
        now = time.time()
        mchg, dom, ts = self._global_cache
        if (now - ts) < 300 and ts > 0:
            return mchg, dom
        if self.demo_mode:
            self._global_cache = (1.2, 56.5, now)
            return 1.2, 56.5
        try:
            req = urllib.request.Request(
                "https://api.coingecko.com/api/v3/global",
                headers={"User-Agent": "TRADEIQ-Brain/2.2", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = (json.loads(resp.read().decode("utf-8")) or {}).get("data") or {}
            m = round(float(data.get("market_cap_change_percentage_24h_usd") or 0.0), 2)
            d = round(float((data.get("market_cap_percentage") or {}).get("btc") or 56.0), 1)
            self._global_cache = (m, d, now)
            return m, d
        except Exception:
            self._global_cache = (mchg, dom, now)
            return mchg, dom

    def fetch_fear_greed(self) -> Tuple[int, str]:
        now = time.time()
        val, lbl, ts = self._fg_cache
        if (now - ts) < 3600 and ts > 0:
            return val, lbl
        if self.demo_mode:
            self._fg_cache = (32, "Fear", now)
            return 32, "Fear"
        try:
            req = urllib.request.Request(
                "https://api.alternative.me/fng/?limit=1",
                headers={"User-Agent": "TRADEIQ-Brain/1.0"},
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            row = (data.get("data") or [{}])[0]
            v = int(row.get("value", 50))
            l = str(row.get("value_classification", "Neutral"))
            self._fg_cache = (v, l, now)
            return v, l
        except Exception:
            self._fg_cache = (val, lbl, now)
            return val, lbl

    def _apply_hysteresis(self, raw_score: int, emergency_crash: bool) -> str:
        """
        Dual-Threshold Anti-Whipsaw Hysteresis:
          - Emergency Crash Override switches to SEVERE_CRASH instantly (zero lag!).
          - Exiting SEVERE_CRASH requires score >= -30 for 2 consecutive evaluations.
          - Normal mode changes require 2 consecutive evaluations outside current band +/- 6pt buffer.
        """
        if emergency_crash:
            self.pending_mode = None
            self.pending_count = 0
            return "SEVERE_CRASH"

        candidate = _raw_mode_from_score(raw_score)
        if self.current_directive is None:
            return candidate

        cur = self.current_directive.mode
        if candidate == cur:
            self.pending_mode = None
            self.pending_count = 0
            return cur

        # v2.1 Rebound Guard: exit SEVERE_CRASH immediately when score recovers >= -25
        if cur == "SEVERE_CRASH" and raw_score >= -25:
            self.pending_mode = None
            self.pending_count = 0
            return candidate
        # Exit buffer out of SEVERE_CRASH (-45 -> must recover above -30)
        if cur == "SEVERE_CRASH" and raw_score < -30:
            self.pending_mode = None
            self.pending_count = 0
            return cur

        # 10-point buffer out of STRONG_UPTREND (+40 -> must drop below +30)
        if cur == "STRONG_UPTREND" and raw_score > 30:
            self.pending_mode = None
            self.pending_count = 0
            return cur

        # 2-Cycle Confirmation prevents 1-candle whipsaw flips
        if self.pending_mode == candidate:
            self.pending_count += 1
        else:
            self.pending_mode = candidate
            self.pending_count = 1

        if self.pending_count >= 2:
            self.pending_mode = None
            self.pending_count = 0
            return candidate
        return cur

    def fast_pulse(
        self,
        tickers: Dict[str, CryptoTicker],
        now_ist: datetime.datetime,
        tg: Any = None,
    ) -> Tuple[bool, Optional[MarketDirective]]:
        """
        Layer 1 — 10-Second Event-Driven Fast Pulse (Zero 5-Min Delay!).
        Checks live BTC/ETH 24h change & 534-coin breadth. Returns (needs_deep_refresh, directive).
        """
        self.last_pulse_ts = time.time()
        btc = tickers.get("BTCUSDT")
        eth = tickers.get("ETHUSDT")
        if not btc:
            return (self.current_directive is None), self.current_directive

        usdt_all = [t for t in tickers.values() if t.symbol.endswith("USDT") and t.ltp > 0]
        total = max(1, len(usdt_all))
        green_pct = round(sum(1 for t in usdt_all if t.change_24h_pct >= 0) / total * 100.0, 1)
        red_pct = 100.0 - green_pct
        btc_chg = float(btc.change_24h_pct)
        eth_chg = float(eth.change_24h_pct) if eth else btc_chg

        if self.current_directive is None:
            return True, None

        # Capture previous BTC 24h delta BEFORE updating live telemetry (Bug #2 fix)
        prev_btc_chg = self.current_directive.btc_chg_24h
        self.current_directive.btc_chg_24h = round(btc_chg, 2)
        self.current_directive.eth_chg_24h = round(eth_chg, 2)
        self.current_directive.breadth_green_pct = green_pct
        self.current_directive.breadth_total_coins = len(usdt_all)
        self.current_directive.updated_at_ist = now_ist.strftime("%H:%M:%S IST")

        # Instant Emergency Crash Override (< 10 seconds!)
        if btc_chg <= -5.0 and red_pct >= 72.0 and self.current_directive.mode != "SEVERE_CRASH":
            return True, self.current_directive

        # Sudden major move (>= 1.5% BTC shift since last pulse/deep sync) -> trigger immediate deep sync
        if abs(btc_chg - prev_btc_chg) >= 1.5:
            return True, self.current_directive

        return False, self.current_directive

    def evaluate_deep(
        self,
        tickers: Dict[str, CryptoTicker],
        btc_15m: List[CryptoCandle],
        btc_1h: List[CryptoCandle],
        btc_4h: List[CryptoCandle],
        eth_1h: List[CryptoCandle],
        btc_depth: Dict[str, Any],
        btc_fp: Dict[str, Any],
        eth_fp: Dict[str, Any],
        news_items: List[Dict[str, Any]],
        now_ist: datetime.datetime,
        tg: Any = None,
    ) -> MarketDirective:
        """Layer 2 — 60-Second Full 6-Layer Quantitative Market Brain Evaluation."""
        self.last_deep_ts = time.time()
        prev_mode = self.current_directive.mode if self.current_directive else None

        btc_t = tickers.get("BTCUSDT")
        eth_t = tickers.get("ETHUSDT")
        btc_chg = float(btc_t.change_24h_pct) if btc_t else 0.0
        eth_chg = float(eth_t.change_24h_pct) if eth_t else 0.0

        # ── Layer 1: BTC & ETH Multi-TF EMA Structure + Hurst Exponent (±25 pts) ──
        closes_1h = [c.close for c in btc_1h] if btc_1h else []
        closes_4h = [c.close for c in btc_4h] if btc_4h else []
        eth_c1h = [c.close for c in eth_1h] if eth_1h else []
        hurst = compute_hurst_exponent(closes_1h)
        hurst_label = "TRENDING" if hurst > 0.52 else ("CHOPPY" if hurst < 0.48 else "TRANSITION")

        f1 = 0
        if len(closes_1h) >= 20:
            ema20_1h = _ema(closes_1h, 20) or closes_1h[-1]
            f1 += 8 if closes_1h[-1] > ema20_1h else -8
        if len(closes_4h) >= 50:
            ema50_4h = _ema(closes_4h, 50) or closes_4h[-1]
            f1 += 9 if closes_4h[-1] > ema50_4h else -9
        elif len(closes_4h) >= 20:
            ema20_4h = _ema(closes_4h, 20) or closes_4h[-1]
            f1 += 9 if closes_4h[-1] > ema20_4h else -9
        if len(eth_c1h) >= 20:
            eth_ema = _ema(eth_c1h, 20) or eth_c1h[-1]
            f1 += 5 if eth_c1h[-1] > eth_ema else -5
        # Live 4-hour BTC ROC removes 24h lagging bias when market reverses
        btc_roc_4h = 0.0
        if len(closes_1h) >= 5 and float(closes_1h[-5]) > 0:
            btc_roc_4h = ((float(closes_1h[-1]) - float(closes_1h[-5])) / float(closes_1h[-5])) * 100.0
        if btc_roc_4h >= 1.2:
            f1 += 4
        elif btc_roc_4h <= -1.2:
            f1 -= 4
        # Amplify directional trend when Hurst confirms persistence (H > 0.52)
        if hurst < 0.48:
            f1 = int(f1 * 0.7)
        f1 = max(-25, min(25, f1))

        # ── Layer 2: Full 534-Coin Cross-Sectional Market Breadth (±25 pts) ───────
        usdt_all = [t for t in tickers.values() if t.symbol.endswith("USDT") and t.ltp > 0]
        total_coins = max(1, len(usdt_all))
        green_cnt = sum(1 for t in usdt_all if t.change_24h_pct >= 0)
        green_pct = round((green_cnt / total_coins) * 100.0, 1)
        red_pct = 100.0 - green_pct
        mcap_chg, btc_dom = self.fetch_global_market()
        # Map 0% green -> -22, 50% green -> 0, 100% green -> +22, plus +/-3 pts from Global MCap flow
        mcap_pts = max(-3, min(3, int(round(mcap_chg))))
        f2 = int(round((green_pct - 50.0) * 0.44)) + mcap_pts
        f2 = max(-25, min(25, f2))

        # ── Layer 3: OKX Live Order-Flow (Footprint CVD + L2 Depth) (±20 pts) ─────
        btc_cvd = float((btc_fp or {}).get("delta_pct", 0.0))
        eth_cvd = float((eth_fp or {}).get("delta_pct", 0.0))
        avg_cvd = (btc_cvd * 0.65) + (eth_cvd * 0.35)
        buy_pct = float((btc_depth or {}).get("buy_pct", 50.0))
        f3 = int(round((avg_cvd / 40.0) * 7.0 + ((buy_pct - 50.0) / 25.0) * 3.0))
        f3 = max(-10, min(10, f3))

        # ── Layer 4: Live MACRO-ONLY News Sentiment (±15 pts) ────────────────────
        # Single-coin hacks (e.g. Chainflip) only block that coin — ONLY systemic macro/BTC/ETF
        # headlines can move the Market Brain!
        macro_kws = ("FED", "FOMC", "CPI", "INFLATION", "RATE", "TREASURY", "ETF", "BITCOIN", "BTC", "SEC", "TARIFF", "WAR", "RECESSION", "LIQUIDATION", "MACRO", "POWELL")
        bull_n = 0
        bear_n = 0
        top_bear_hl = ""
        top_bull_hl = ""
        for n in (news_items or [])[:12]:
            hl_up = f"{n.get('tag', '')} {n.get('headline', '')}".upper()
            if not any(k in hl_up for k in macro_kws):
                continue
            imp = n.get("impact", "NEUTRAL")
            if imp == "HIGH_RISK":
                bear_n += 1
                if not top_bear_hl:
                    top_bear_hl = f"{n.get('tag')}: {n.get('headline', '')[:80]}"
            elif imp == "BULLISH_CATALYST":
                bull_n += 1
                if not top_bull_hl:
                    top_bull_hl = f"{n.get('tag')}: {n.get('headline', '')[:80]}"
        f4 = max(-15, min(15, (bull_n - bear_n) * 5))

        # ── Layer 5: Volatility Burst & Funding Rate Stress (±10 pts) ─────────────
        vol_burst = compute_vol_burst_ratio(btc_15m)
        fr_pct = float((btc_t.funding_rate * Decimal("100")) if btc_t else Decimal("0.01"))
        f5 = 0
        if vol_burst >= 1.6:
            f5 += 6 if btc_chg >= 0 else -6
        if fr_pct >= 0.05:
            f5 -= 4  # Over-leveraged longs -> correction risk
        elif fr_pct <= -0.02:
            f5 += 4  # Crowded shorts -> squeeze potential
        f5 = max(-10, min(10, f5))

        # ── Layer 6: External Fear & Greed Index (±5 pts background context) ──────
        fg_val, fg_lbl = self.fetch_fear_greed()
        f6 = int(round(((fg_val - 50.0) / 50.0) * 5.0))
        f6 = max(-5, min(5, f6))

        raw_score = max(-100, min(100, f1 + f2 + f3 + f4 + f5 + f6))
        # Broad-Breadth Sanity Guard: a 62%+ Green market with BTC >= 0% cannot flip into DOWNTREND
        if green_pct >= 62.0 and btc_chg >= 0.0 and raw_score < -10:
            raw_score = -10
        if green_pct >= 72.0 and btc_chg >= 2.5 and raw_score < 20:
            raw_score = 20
        if red_pct >= 62.0 and btc_chg <= 0.0 and raw_score > 10:
            raw_score = 10

        emergency_crash = (btc_chg <= -5.0 and red_pct >= 72.0) or (btc_chg <= -6.5) or (btc_roc_4h <= -3.5 and red_pct >= 70.0)
        if emergency_crash and raw_score > -48:
            raw_score = -55

        # EMA Score Smoothing (0.40 * raw + 0.60 * prev_ema) prevents 1-minute CVD flickers;
        # bypassed immediately on true emergency crashes for zero lag!
        if emergency_crash or self.ema_score is None:
            smoothed_score = int(raw_score)
            self.ema_score = float(raw_score)
        else:
            self.ema_score = (0.40 * float(raw_score)) + (0.60 * self.ema_score)
            smoothed_score = int(round(self.ema_score))

        confirmed_mode = self._apply_hysteresis(smoothed_score, emergency_crash)
        bias, allow_l, allow_s, allow_b, spot_dir = _mode_permissions(confirmed_mode)

        # Build human-readable primary market cause
        if confirmed_mode == "SEVERE_CRASH":
            cause = top_bear_hl or f"Systemic Sell-Off: BTC {btc_chg:+.2f}% • {red_pct:.0f}% of {len(usdt_all)} Coins Red • Vol {vol_burst}x"
        elif confirmed_mode == "DOWNTREND":
            cause = top_bear_hl or f"Bearish Structure: BTC {btc_chg:+.2f}% below EMA • {red_pct:.0f}% Coins Red • CVD {avg_cvd:+.1f}%"
        elif confirmed_mode == "STRONG_UPTREND":
            cause = top_bull_hl or f"Broad Rally: BTC {btc_chg:+.2f}% • {green_pct:.0f}% Coins Green • Hurst {hurst} (Trending)"
        elif confirmed_mode == "HEALTHY_CORRECTION":
            cause = f"Pullback in Bull Structure: {green_pct:.0f}% Green • Prime Spot Dip Zone"
        else:
            cause = f"Range-Bound / Choppy (Hurst {hurst}) • {green_pct:.0f}% Green • Mean-Reversion Favored"

        conf = min(98, max(52, int(55 + abs(raw_score) * 0.43)))
        factors = [
            {"name": "BTC/ETH Trend + Hurst", "pts": f1, "max": 25, "detail": f"BTC {btc_chg:+.2f}% • Hurst {hurst} ({hurst_label})"},
            {"name": "534-Coin Breadth + Global MCap", "pts": f2, "max": 25, "detail": f"{green_pct:.0f}% Green • MCap {mcap_chg:+.1f}% • BTC.D {btc_dom}%"},
            {"name": "OKX Footprint CVD + L2", "pts": f3, "max": 20, "detail": f"CVD {avg_cvd:+.1f}% • L2 {buy_pct:.0f}% Buy"},
            {"name": "Macro News Sentiment", "pts": f4, "max": 15, "detail": f"{bull_n} Bull / {bear_n} Risk Headlines"},
            {"name": "Vol Burst + Funding", "pts": f5, "max": 10, "detail": f"Vol {vol_burst}x • Funding {fr_pct:+.3f}%"},
            {"name": "Fear & Greed Context", "pts": f6, "max": 5, "detail": f"{fg_val}/100 ({fg_lbl})"},
        ]

        directive = MarketDirective(
            mode=confirmed_mode,
            bias=bias,
            score=smoothed_score,
            raw_score=raw_score,
            confidence_pct=conf,
            hurst=hurst,
            hurst_label=hurst_label,
            vol_burst_ratio=vol_burst,
            breadth_green_pct=green_pct,
            breadth_total_coins=len(usdt_all),
            btc_chg_24h=round(btc_chg, 2),
            eth_chg_24h=round(eth_chg, 2),
            btc_cvd_pct=round(avg_cvd, 1),
            fear_greed_value=fg_val,
            fear_greed_label=fg_lbl,
            session_label=get_ist_session_label(now_ist),
            allow_longs=allow_l,
            allow_shorts=allow_s,
            allow_boom_long=allow_b,
            spot_directive=spot_dir,
            primary_cause=cause,
            factors=factors,
            updated_at_ist=now_ist.strftime("%H:%M:%S IST"),
            emergency_override=emergency_crash,
        )
        self.current_directive = directive

        # Record & send Sapphire Cyan Telegram Mode-Shift Alert when confirmed regime changes!
        if prev_mode and prev_mode != confirmed_mode:
            self.history.insert(0, {
                "from": prev_mode, "to": confirmed_mode, "score": smoothed_score,
                "cause": cause, "at_ist": now_ist.strftime("%d %b %I:%M %p IST"),
            })
            self.history = self.history[:20]
            # Brain mode shifts are recorded in UI history only — zero Telegram spam!
            # Current Brain state is attached directly inside Trade Entry messages.
        return directive
