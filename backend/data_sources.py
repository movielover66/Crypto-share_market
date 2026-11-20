"""
data_sources.py  (put next to main.py in /backend)

Collects HARD facts about a coin (no AI guessing here). Every function is best-effort:
it never raises, and returns {} / [] / None when a source is down or has nothing.
Sources: CoinPaprika (profile, supply, rank), GitHub (dev activity), DefiLlama (TVL), Google News RSS (headlines).
Optional env: GITHUB_TOKEN (raises the GitHub limit from 60 to 5000 requests/hour).
"""
import asyncio
import datetime
import os
import re
import time
from typing import Any, Dict, List, Optional

import feedparser
import httpx

UA = {"User-Agent": "ApexQuantResearch/1.0"}
_GH_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.-]+))?", re.I)
_LLAMA: Dict[str, Any] = {"ts": 0.0, "rows": None}


async def _get_json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None,
                    headers: Optional[dict] = None, timeout: float = 10.0) -> Any:
    try:
        r = await client.get(url, params=params, headers=headers or UA, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _age_days(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return max(0, (datetime.datetime.now(datetime.timezone.utc) - d).days)
    except Exception:
        return None


# ------------------------------------------------------------ CoinPaprika
async def paprika_profile(client: httpx.AsyncClient, symbol: str) -> Dict[str, Any]:
    sym = symbol.upper()
    s = await _get_json(client, "https://api.coinpaprika.com/v1/search",
                        {"q": sym, "c": "currencies", "limit": 10})
    cands = [c for c in ((s or {}).get("currencies") or [])
             if str(c.get("symbol", "")).upper() == sym and c.get("is_active", True) is not False]
    if not cands:
        return {}
    best = min(cands, key=lambda c: c.get("rank") or 10 ** 9)       # rank 0/None = unranked -> last
    cid = best.get("id")
    if not cid:
        return {}
    coin, tick = await asyncio.gather(
        _get_json(client, f"https://api.coinpaprika.com/v1/coins/{cid}"),
        _get_json(client, f"https://api.coinpaprika.com/v1/tickers/{cid}"))
    out: Dict[str, Any] = {"id": cid, "name": str(best.get("name") or sym)[:60],
                           "rank": best.get("rank") or None}
    if isinstance(coin, dict):
        gh = ""
        for l in coin.get("links_extended") or []:
            u = str((l or {}).get("url") or "")
            if (l or {}).get("type") == "source_code" and "github.com" in u.lower():
                gh = u
                break
        out.update({
            "description": str(coin.get("description") or "")[:500],
            "started_at": coin.get("started_at"),
            "age_days": _age_days(coin.get("started_at")),
            "type": coin.get("type"),
            "tags": [str(t.get("name")) for t in (coin.get("tags") or []) if t.get("name")][:8],
            "team": [{"name": str(p.get("name") or "")[:40], "position": str(p.get("position") or "")[:40]}
                     for p in (coin.get("team") or [])[:6]],
            "open_source": coin.get("open_source"),
            "website": ((coin.get("links") or {}).get("website") or [None])[0],
            "github_url": gh,
        })
    if isinstance(tick, dict):
        q = (tick.get("quotes") or {}).get("USD") or {}
        price, mc = _num(q.get("price")), _num(q.get("market_cap"))
        supply = _num(tick.get("max_supply")) or _num(tick.get("total_supply"))
        fdv = price * supply if price and supply else None
        out.update({
            "rank": tick.get("rank") or out.get("rank"),
            "market_cap_usd": mc or None,
            "volume_24h_usd": _num(q.get("volume_24h")),
            "fdv_usd": fdv,
            "fdv_mc_ratio": round(fdv / mc, 2) if fdv and mc else None,
            "from_ath_pct": _num(q.get("percent_from_price_ath")),
        })
    return out


# ------------------------------------------------------------ GitHub
async def github_activity(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    m = _GH_RE.search(url or "")
    if not m:
        return {}
    owner, repo = m.group(1), (m.group(2) or "")
    if repo.endswith(".git"):
        repo = repo[:-4]
    h = {"Accept": "application/vnd.github+json", "User-Agent": "ApexQuantResearch/1.0"}
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if not repo:        # only an org/user page was listed: use its most recently pushed repo
        repos = await _get_json(client, f"https://api.github.com/users/{owner}/repos",
                                {"sort": "pushed", "per_page": 5}, h)
        if not isinstance(repos, list) or not repos:
            return {}
        repo = str(repos[0].get("name") or "")
        if not repo:
            return {}
    info = await _get_json(client, f"https://api.github.com/repos/{owner}/{repo}", None, h)
    if not isinstance(info, dict):
        return {}
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = await _get_json(client, f"https://api.github.com/repos/{owner}/{repo}/commits",
                              {"since": since, "per_page": 100}, h)
    return {"repo": f"{owner}/{repo}",
            "commits_90d": len(commits) if isinstance(commits, list) else None,   # 100 means "100 or more"
            "pushed_at": info.get("pushed_at"), "stars": info.get("stargazers_count"),
            "archived": bool(info.get("archived"))}


# ------------------------------------------------------------ DefiLlama TVL (DeFi projects only)
def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


async def defillama_tvl(client: httpx.AsyncClient, symbol: str, name: str) -> Dict[str, Any]:
    now = time.time()
    if not _LLAMA["rows"] or now - _LLAMA["ts"] > 86400:
        d = await _get_json(client, "https://api.llama.fi/protocols", timeout=25.0)
        if isinstance(d, list):
            _LLAMA["rows"] = [{k: p.get(k) for k in ("name", "symbol", "tvl", "change_7d", "change_1m", "category")}
                              for p in d if isinstance(p, dict)]
            _LLAMA["ts"] = now
    nn = _norm(name)
    hits = []
    for p in _LLAMA["rows"] or []:
        pn = _norm(p.get("name"))
        if (str(p.get("symbol") or "").upper() == symbol.upper() and _num(p.get("tvl")) and pn and nn
                and (pn in nn or nn in pn)):            # symbol AND name must match: tickers collide a lot
            hits.append(p)
    if not hits:
        return {}
    p = max(hits, key=lambda x: _num(x.get("tvl")) or 0)
    return {"protocol": p.get("name"), "tvl_usd": _num(p.get("tvl")), "tvl_7d_pct": _num(p.get("change_7d")),
            "tvl_30d_pct": _num(p.get("change_1m")), "category": p.get("category")}


# ------------------------------------------------------------ Google News RSS (no key needed)
async def news_headlines(client: httpx.AsyncClient, name: str, symbol: str) -> List[Dict[str, str]]:
    q = f'"{name}" crypto when:7d' if name and name.upper() != symbol.upper() else f"{symbol} crypto coin when:7d"
    try:
        r = await client.get("https://news.google.com/rss/search", headers=UA, timeout=10.0,
                             params={"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        if r.status_code != 200:
            return []
        feed = feedparser.parse(r.content)
    except Exception:
        return []
    out = []
    for e in feed.entries[:8]:
        src = e.get("source")
        out.append({"title": str(e.get("title") or "")[:160],
                    "source": str((src.get("title") if hasattr(src, "get") else "") or "")[:40],
                    "published": str(e.get("published") or "")[:32]})
    return out


# ------------------------------------------------------------ everything together
async def collect(client: httpx.AsyncClient, symbol: str, dcx_inr: Optional[Dict[str, float]] = None,
                  deep: bool = False) -> Dict[str, Any]:
    sym = symbol.upper()
    prof = await paprika_profile(client, sym)
    name = prof.get("name") or sym
    jobs: Dict[str, Any] = {"news": news_headlines(client, name, sym)}
    if deep:
        if prof.get("github_url"):
            jobs["github"] = github_activity(client, prof["github_url"])
        jobs["tvl"] = defillama_tvl(client, sym, name)
    res = await asyncio.gather(*jobs.values(), return_exceptions=True)
    got = {k: (None if isinstance(v, BaseException) else v) for k, v in zip(jobs.keys(), res)}
    gh, tvl = got.get("github") or {}, got.get("tvl") or {}
    facts = dict(prof)
    facts.update({
        "symbol": sym, "name": name,
        "coindcx_inr": (sym.lower() in dcx_inr) if dcx_inr else None,
        "headlines": got.get("news") or [],
        "repo": gh.get("repo"), "commits_90d": gh.get("commits_90d"), "repo_archived": gh.get("archived"),
        "repo_pushed_at": gh.get("pushed_at"),
        "tvl_usd": tvl.get("tvl_usd"), "tvl_30d_pct": tvl.get("tvl_30d_pct"), "tvl_category": tvl.get("category"),
        "has_paprika": bool(prof),
    })
    return facts
