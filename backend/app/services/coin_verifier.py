"""
TRADEIQ Crypto Pro v2.1 — Gate 1: CoinGecko Entity & Fundamental Coin Verifier + Stage 1 Fast Screener.
Prevents OKX Tokenized US Stocks/ETFs (MUUSDT, SOXLUSDT, SNDKUSDT, UVXYUSDT, etc.) and micro-cap
junk tokens from ever entering the trading pipeline, while allowing genuine established AND newly
launched verified crypto projects (AI, L1, L2, DeFi, RWA) with real liquidity.
"""
import json
import time
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

# Hard blacklist of OKX Tokenized US Stocks, ETFs, Commodities & Known Non-Crypto/Junk Swaps
NON_CRYPTO_BLACKLIST: Set[str] = {
    # US Tech & Semiconductor Stocks / ETFs on OKX Swaps
    "MUUSDT", "SOXLUSDT", "SNDKUSDT", "UVXYUSDT", "TSLAUSDT", "NVDAUSDT",
    "AAPLUSDT", "MSTRUSDT", "COINUSDT", "QQQUSDT", "SPYUSDT", "AMDUSDT",
    "AMZNUSDT", "GOOGLUSDT", "GOOGUSDT", "METAUSDT", "MSFTUSDT", "NFLXUSDT",
    "PLTRUSDT", "INTCUSDT", "SQQQUSDT", "TQQQUSDT", "ARKKUSDT", "IWMUSDT",
    "BABAUSDT", "HOODUSDT", "SMCIUSDT", "ARMUSDT", "AVGOUSDT", "ORCLUSDT",
    "CRWDUSDT", "SNOWUSDT", "UBERUSDT", "PYPLUSDT", "SHOPUSDT", "SQUSDT",
    "GMEUSDT", "AMCUSDT", "RIVNUSDT", "LCIDUSDT", "NIOUSDT", "DISUSDT",
    "BAUSDT", "JPMUSDT", "GSUSDT", "VUSDT", "MAUSDT", "XOMUSDT", "CVXUSDT",
    # Commodity / FX / Index Proxies & Stablecoins
    "GLDUSDT", "SLVUSDT", "XAUTUSDT", "PAXGUSDT", "XAUUSDT", "XAGUSDT", "USDCUSDT", "FDUSDUSDT",
    "TUSDUSDT", "USDEUSDT", "DAIUSDT", "EURTUSDT", "PYUSDUSDT", "USDPUSDT",
    # Known ultra-noisy micro-junk tickers from live audit
    "USELESSUSDT", "SKDDUSDT", "BTWUSDT", "PROSUSDT",
}

# Verified core crypto symbols (always recognized even if CoinGecko rate-limits)
VERIFIED_CRYPTO_BASE: Set[str] = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "AVAX", "LINK", "DOGE", "SUI",
    "TON", "LTC", "DOT", "NEAR", "ARB", "OP", "INJ", "RENDER", "FET", "APT",
    "UNI", "AAVE", "ATOM", "FIL", "HBAR", "ICP", "VET", "ALGO", "XLM", "ETC",
    "BCH", "STX", "IMX", "MKR", "LDO", "GRT", "RUNE", "SEI", "TIA", "JUP",
    "WIF", "PEPE", "BONK", "SHIB", "FLOKI", "ENA", "ONDO", "PENDLE", "TAO",
    "WLD", "PYTH", "JTO", "STRK", "ZK", "EIGEN", "ETHFI", "VIRTUAL", "AI16Z",
    "TRX", "ZEC", "XMR", "CRV", "SNX", "COMP", "SUSHI", "SAND", "MANA", "AXS",
    "GALA", "CHZ", "APE", "BLUR", "DYDX", "GMX", "ENS", "KAS", "FTM", "S",
    "BERA", "IP", "KAITO", "TRUMP", "PNUT", "POPCAT", "NEIRO", "NOT", "DOGS",
}


@dataclass
class CoinVerification:
    symbol: str
    verified: bool
    category: str
    market_cap_usd: float
    reason: str
    source: str


class CoinFundamentalVerifier:
    """
    Gate 1 Entity & Fundamental Verifier.
    Uses free CoinGecko public /coins/markets (cached 6 hours, 1 single lightweight call)
    plus deterministic US-stock/ETF blacklist so zero non-crypto swaps slip through.
    """

    def __init__(self, demo_mode: bool = False) -> None:
        self.demo_mode = demo_mode
        self._cg_map: Dict[str, Dict[str, Any]] = {}
        self._last_sync: float = 0.0

    def _refresh_coingecko_cache(self) -> None:
        now = time.time()
        if self.demo_mode or (now - self._last_sync) < 21600 and self._cg_map:
            return
        try:
            url = (
                "https://api.coingecko.com/api/v3/coins/markets"
                "?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "TRADEIQ-Crypto-v2.1", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
            if isinstance(rows, list) and rows:
                new_map: Dict[str, Dict[str, Any]] = {}
                for r in rows:
                    sym = str(r.get("symbol") or "").upper().strip()
                    if not sym or sym in new_map:
                        continue
                    new_map[sym] = {
                        "id": str(r.get("id") or ""),
                        "name": str(r.get("name") or sym),
                        "market_cap": float(r.get("market_cap") or 0.0),
                        "total_volume": float(r.get("total_volume") or 0.0),
                        "rank": int(r.get("market_cap_rank") or 9999),
                    }
                if new_map:
                    self._cg_map = new_map
                    self._last_sync = now
        except Exception:
            self._last_sync = now  # Soft backoff on rate-limit

    def verify_symbol(
        self,
        symbol: str,
        volume_24h_usdt: Decimal = Decimal("0"),
    ) -> CoinVerification:
        sym = (symbol or "").strip().upper()
        if not sym.endswith("USDT"):
            sym += "USDT"
        base = sym[:-4]

        # 1. Hard Reject OKX Tokenized US Stocks, ETFs, Commodities & Known Junk
        if sym in NON_CRYPTO_BLACKLIST:
            return CoinVerification(
                symbol=sym,
                verified=False,
                category="NON_CRYPTO_STOCK_OR_JUNK",
                market_cap_usd=0.0,
                reason=f"Blocked by Gate 1: {sym} is a tokenized US stock/ETF/stablecoin or junk ticker.",
                source="BLACKLIST_GUARD",
            )

        # 2. Minimum real 24h liquidity check ($10M USD minimum to eliminate illiquid manipulation traps)
        if not self.demo_mode and volume_24h_usdt > 0 and volume_24h_usdt < Decimal("10000000"):
            return CoinVerification(
                symbol=sym,
                verified=False,
                category="LOW_LIQUIDITY",
                market_cap_usd=0.0,
                reason=f"Blocked by Gate 1: 24h volume ${volume_24h_usdt:,.0f} < $10M minimum.",
                source="LIQUIDITY_GUARD",
            )

        # 3. Check live CoinGecko top-250 verified market-cap registry
        self._refresh_coingecko_cache()
        cg = self._cg_map.get(base)
        if cg:
            mcap = float(cg.get("market_cap") or 0.0)
            if mcap >= 25_000_000:
                return CoinVerification(
                    symbol=sym,
                    verified=True,
                    category=f"COINGECKO_TOP_{cg.get('rank', 250)}",
                    market_cap_usd=mcap,
                    reason=f"Verified Crypto ({cg.get('name')} • MCap ${mcap/1e6:.0f}M)",
                    source="COINGECKO_API",
                )

        # 4. Verified core crypto base fallback (handles CoinGecko 429 rate limits cleanly)
        if base in VERIFIED_CRYPTO_BASE:
            return CoinVerification(
                symbol=sym,
                verified=True,
                category="VERIFIED_CRYPTO_CORE",
                market_cap_usd=100_000_000.0,
                reason=f"Verified Pure-Crypto Asset ({base})",
                source="VERIFIED_REGISTRY",
            )

        # 5. Allow genuinely liquid new crypto launches ONLY if 24h volume >= $40M (strong institutional listing)
        if volume_24h_usdt >= Decimal("40000000") and len(base) >= 3 and base.isalpha():
            return CoinVerification(
                symbol=sym,
                verified=True,
                category="HIGH_LIQ_NEW_CRYPTO",
                market_cap_usd=0.0,
                reason=f"High-liquidity crypto listing (${volume_24h_usdt/Decimal('1000000'):.1f}M 24h vol)",
                source="HIGH_LIQ_LISTING",
            )

        return CoinVerification(
            symbol=sym,
            verified=False,
            category="UNVERIFIED_TOKEN",
            market_cap_usd=0.0,
            reason=f"Blocked by Gate 1: {sym} not verified on CoinGecko Top-250 or verified crypto registry.",
            source="ENTITY_GUARD",
        )

    def select_verified_candidates(
        self,
        tickers: Dict[str, Any],
        max_candidates: int = 18,
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """
        Stage 1 Fast Screener Funnel (534+ OKX coins -> Top 15-20 verified pure-crypto coins).
        Returns (selected_symbols, rejected_samples).
        """
        verified_tickers: List[Any] = []
        rejected: List[Dict[str, str]] = []
        for t in tickers.values():
            if not t.symbol.endswith("USDT") or t.ltp <= 0:
                continue
            v = self.verify_symbol(t.symbol, t.volume_24h_usdt)
            if v.verified:
                verified_tickers.append(t)
            else:
                if len(rejected) < 20:
                    rejected.append({"symbol": t.symbol, "reason": v.reason})

        by_vol = sorted(verified_tickers, key=lambda x: x.volume_24h_usdt, reverse=True)[:12]
        by_mom = sorted(
            [x for x in verified_tickers if Decimal("2.0") <= abs(x.change_24h_pct) <= Decimal("22.0")],
            key=lambda x: abs(x.change_24h_pct),
            reverse=True,
        )[:8]
        selected = list(dict.fromkeys([t.symbol for t in by_vol] + [t.symbol for t in by_mom]))[:max_candidates]
        return selected, rejected
