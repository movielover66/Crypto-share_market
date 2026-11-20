"""
stock_live.py  (put next to main.py in /backend)

LIVE intraday momentum radar for NSE stocks, fed by Angel One ticks (angel_feed.py).
  * LiveEngine      : pure logic. Takes ticks, spots "boom" style momentum, tracks every alert with Multi-Step Trailing SL & T1->T2 progression.
  * StockLiveRunner : starts/stops the feed on market hours (Mon-Fri), watchdog, holiday notice, end-of-day report.

Env:
  INTRADAY_RISK_INR    (default 300.0 -> max risk per trade)
  INTRADAY_MAX_POS_INR (default 24500.0 -> 5x leverage cap on 5000 INR margin)
  STOCK_ALERTS_PER_HOUR (default 8)
"""
import asyncio
import datetime
import html
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

import httpx

try:
    from . import angel_feed as af
    from . import stock_premarket
    from . import universe
except ImportError:
    import angel_feed as af
    import stock_premarket
    import universe

OPEN_M, ORB_END_M, ENTRY_END_M, CLOSE_M = 555, 570, 900, 930      # 9:15, 9:30, 15:00, 15:30 (minutes after midnight)
MIN_TURNOVER = 5e7                                                  # avg daily traded value >= Rs 5 crore
COOLDOWN_S = 45 * 60
MAX_PER_STOCK = 2
_JOURNAL = Path(__file__).resolve().parent / "intraday_journal.json"
SHAPE = [(555, 570, 3.0), (570, 630, 1.5), (630, 810, 0.8), (810, 885, 0.9), (885, 930, 1.4)]
_TOTAL = sum((e - s) * f for s, e, f in SHAPE)


def _intensity(m: float) -> float:
    for s, e, f in SHAPE:
        if s <= m < e:
            return f
    return 0.0


def cum_fraction(m: float) -> float:
    acc = 0.0
    for s, e, f in SHAPE:
        if m >= e:
            acc += (e - s) * f
        elif m > s:
            acc += (m - s) * f
    return acc / _TOTAL


def exp5(m: float) -> float:
    return _intensity(m) * 5 / _TOTAL


def compute_baseline(rows: List[List[Any]]) -> Optional[Dict[str, float]]:
    c = []
    for r in rows or []:
        try:
            c.append({"h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])})
        except (TypeError, ValueError, IndexError):
            continue
    if len(c) < 16:
        return None
    trs = []
    for i in range(len(c) - 14, len(c)):
        pc = c[i - 1]["c"]
        trs.append(max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - pc), abs(c[i]["l"] - pc)))
    vols = [x["v"] for x in c[-20:]]
    last, avgvol = c[-1], sum(vols) / len(vols)
    if last["c"] <= 0 or avgvol <= 0:
        return None
    return {"pc": last["c"], "pdh": last["h"], "pdl": last["l"], "atr": sum(trs) / 14, "avgvol": avgvol,
            "turnover": avgvol * last["c"]}


def _esc(v: Any) -> str:
    return html.escape(str(v), quote=False)


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


A_WINDOWS = [(570, 690), (825, 885)]       # A-grade alerts only 9:30-11:30 and 1:45-2:45 PM
CLOSED = ("WIN", "LOSS", "TIME")


def _is_a(a: Dict[str, Any]) -> bool:
    return bool(a.get("alerted", True))


class LiveEngine:
    """Spots momentum, grades it (A = sent to Telegram, B = silent shadow signal),
    and tracks every signal with progressive Trailing SL (Break-even -> +0.5R -> T1 Lock -> T2 Target)."""

    def __init__(self, send: Optional[Callable[[str], Awaitable[Any]]] = None,
                 clock: Callable[[], float] = time.time,
                 research: Optional[Callable[[str], Awaitable[Optional[Dict[str, str]]]]] = None) -> None:
        self.send, self.clock, self.research = send, clock, research
        self.base: Dict[str, Dict[str, float]] = {}
        self.tok2sym: Dict[str, str] = {}
        self.state: Dict[str, Dict[str, Any]] = {}
        self.alerts: List[Dict[str, Any]] = []
        self.cool: Dict[str, float] = {}
        self.stamps: List[float] = []
        self.nifty: Dict[str, Any] = {"ltp": None, "pc": None, "hist": deque(maxlen=500), "last_alert": 0.0}
        self.date = ""
        self.eod_done = False
        self.pause_until = 0.0
        self.loss_notice = False
        self.outbox: List[str] = []
        self._tasks: set = set()

    # ---------------------------------------------------------------- setup
    def setup(self, base: Dict[str, Dict[str, float]], tok2sym: Dict[str, str], nifty_pc: Optional[float]) -> None:
        self.base, self.tok2sym = base, tok2sym
        self.nifty["pc"] = nifty_pc
        for sym in base:
            self.state.setdefault(sym, {"hist": deque(maxlen=400), "ltp": None, "ts": 0.0, "vol": 0.0,
                                        "last_eval": 0.0, "orb_hi": None, "orb_lo": None, "orb_ok": False})

    def reset_day(self, date_str: str) -> None:
        self.date, self.eod_done = date_str, False
        self.alerts, self.cool, self.stamps = [], {}, []
        self.pause_until, self.loss_notice = 0.0, False
        self.state = {}
        self.nifty.update(ltp=None, last_alert=0.0)
        self.nifty["hist"].clear()
        try:
            d = json.loads(_JOURNAL.read_text(encoding="utf-8"))
            if d.get("date") == date_str and isinstance(d.get("alerts"), list):
                self.alerts = d["alerts"]
                for a in self.alerts:
                    if a.get("status") == "PENDING":
                        a["status"], a["skip"] = "SKIPPED", "রিস্টার্টের কারণে বাতিল"
                    self.cool[a["sym"]] = max(self.cool.get(a["sym"], 0.0), a["ts"] + COOLDOWN_S)
                self.eod_done = bool(d.get("eod_done"))
        except Exception:
            pass

    def _save(self) -> None:
        try:
            _JOURNAL.write_text(json.dumps({"date": self.date, "eod_done": self.eod_done, "alerts": self.alerts},
                                           ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _emit(self, text: str) -> None:
        self.outbox.append(text)
        del self.outbox[:-50]
        if self.send:
            try:
                self._spawn(self.send(text))
            except RuntimeError:
                pass

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- ticks
    def nifty_chg(self) -> Optional[float]:
        n = self.nifty
        return (n["ltp"] / n["pc"] - 1) * 100 if n["ltp"] and n["pc"] else None

    def on_tick(self, t: Dict[str, Any], ist: Optional[datetime.datetime] = None) -> None:
        ist = ist or af.ist_now()
        m, now = ist.hour * 60 + ist.minute, self.clock()
        if t["token"] == af.NIFTY_WS_TOKEN:
            self._nifty_tick(t["ltp"], m, now)
            return
        sym = self.tok2sym.get(t["token"])
        st = self.state.get(sym) if sym else None
        if st is None:
            return
        ltp = t["ltp"]
        st["ltp"], st["ts"] = ltp, now
        if t.get("vol") is not None:
            st["vol"] = t["vol"]
        if t.get("avg"):
            st["vwap"] = t["avg"]
        if t.get("open"):
            st["day_open"] = t["open"]
        if not st["hist"] or now - st["hist"][-1][0] >= 5:
            st["hist"].append((now, ltp, st["vol"]))
        if OPEN_M <= m < ORB_END_M:
            st["orb_hi"] = ltp if st["orb_hi"] is None else max(st["orb_hi"], ltp)
            st["orb_lo"] = ltp if st["orb_lo"] is None else min(st["orb_lo"], ltp)
            st["orb_ok"] = True
        self._track(sym, ltp, now)
        if not (ORB_END_M <= m <= ENTRY_END_M) or now - st["last_eval"] < 2:
            return
        st["last_eval"] = now
        sig = self.evaluate(sym, m, now)
        if sig:
            self._fire(sig, now, ist.strftime("%H:%M"), m)

    def _nifty_tick(self, ltp: float, m: int, now: float) -> None:
        n = self.nifty
        n["ltp"] = ltp
        h: Deque = n["hist"]
        if not h or now - h[-1][0] >= 5:
            h.append((now, ltp))
        if not (ORB_END_M <= m <= CLOSE_M) or now - n["last_alert"] < 1800:
            return
        ref = next((x for x in reversed(h) if now - x[0] >= 890), None)
        if ref and ref[1] > 0:
            mv = (ltp / ref[1] - 1) * 100
            if abs(mv) >= 0.6:
                n["last_alert"] = now
                self._emit(f"{'📈' if mv > 0 else '📉'} <b>NIFTY ১৫ মিনিটে {mv:+.2f}%</b>\n"
                           f"Nifty এখন {ltp:,.1f}। বাজারের দিক বদলাচ্ছে, লং এন্ট্রিতে সাবধান।")

    # ---------------------------------------------------------------- signal
    def evaluate(self, sym: str, m: int, now: float) -> Optional[Dict[str, Any]]:
        b, st = self.base.get(sym), self.state.get(sym)
        if not b or not st or st["ltp"] is None or b["turnover"] < MIN_TURNOVER:
            return None
        ltp, vwap, vol = st["ltp"], st.get("vwap"), st.get("vol", 0.0)
        if not vwap or vol <= 0:
            return None
        ref = next((x for x in reversed(st["hist"]) if now - x[0] >= 285), None)
        if ref is None or now - ref[0] > 900 or ref[1] <= 0:
            return None
        move5 = (ltp / ref[1] - 1) * 100
        exp = b["avgvol"] * exp5(m)
        r5 = max(0.0, vol - ref[2]) / exp if exp > 0 else 0.0
        cf = cum_fraction(m)
        rv = vol / (b["avgvol"] * cf) if cf > 0.02 else 0.0
        orb_hi = st["orb_hi"] if st.get("orb_ok") else None
        broke_orb = orb_hi is not None and ltp > orb_hi * 1.0005
        broke_pdh = ltp > b["pdh"]
        trigger = broke_orb if orb_hi is not None else broke_pdh
        day_open = st.get("day_open") or b["pc"]
        ext = (ltp / day_open - 1) * 100
        if not (trigger and ltp > vwap and move5 > 0 and r5 >= 1.8) or ext > 6 or ltp > vwap * 1.03:
            return None
        nchg = self.nifty_chg()
        stock_chg = (ltp / b["pc"] - 1) * 100
        s, why = 0.0, []
        if r5 >= 4:
            s += 25; why.append(f"গত ৫ মিনিটে ভলিউম স্বাভাবিকের {r5:.1f} গুণ")
        elif r5 >= 2.5:
            s += 18; why.append(f"গত ৫ মিনিটে ভলিউম স্বাভাবিকের {r5:.1f} গুণ")
        else:
            s += 10; why.append(f"ভলিউম বাড়ছে ({r5:.1f} গুণ)")
        if rv >= 3:
            s += 15; why.append(f"আজকের মোট ভলিউম এই সময়ের তুলনায় {rv:.1f} গুণ")
        elif rv >= 2:
            s += 10; why.append(f"আজকের মোট ভলিউম {rv:.1f} গুণ")
        elif rv >= 1.5:
            s += 5
        if broke_orb:
            s += 15; why.append("৯:১৫-৯:৩০ রেঞ্জের উপরে ব্রেকআউট")
        if broke_pdh:
            s += 10; why.append("গতকালের হাই ভেঙেছে")
        s += 10; why.append("VWAP-এর উপরে")
        if move5 >= 0.8:
            s += 15
        elif move5 >= 0.4:
            s += 10
        elif move5 >= 0.25:
            s += 5
        why.append(f"৫ মিনিটে {move5:+.2f}%")
        if nchg is not None:
            rs = stock_chg - nchg
            if rs >= 1:
                s += 10; why.append("Nifty-র চেয়ে অনেক শক্তিশালী")
            elif rs > 0:
                s += 5
            if nchg <= -1.0:
                s -= 15; why.append(f"⚠️ Nifty আজ {nchg:.1f}% নিচে, বাজার দুর্বল")
        s = round(max(0.0, min(100.0, s)), 1)
        if s < 70:
            return None
        atr_pct = b["atr"] / b["pc"]
        risk = min(max(0.5 * atr_pct, 0.006), 0.018)
        sl = round(ltp * (1 - risk), 2)
        r = ltp - sl
        return {"sym": sym, "kind": "BOOM" if (s >= 80 and r5 >= 4) else "MOMENTUM", "score": s, "entry": ltp,
                "sl": sl, "orig_sl": sl, "t1": round(ltp + 1.5 * r, 2), "t2": round(ltp + 2.5 * r, 2),
                "t1_hit": False, "tsl_stage": 0, "chg": stock_chg, "vol_x": round(max(r5, rv), 1),
                "nifty": nchg, "why": why[:6], "r5": round(r5, 2), "risk": risk}

    @staticmethod
    def _qty(entry: float, sl: float) -> int:
        risk_budget = _envf("INTRADAY_RISK_INR", 300.0)
        max_pos_inr = _envf("INTRADAY_MAX_POS_INR", 24500.0)
        if entry <= 0:
            return 1
        qty_by_cap = int(max_pos_inr / entry)
        qty_by_risk = int(risk_budget / (entry - sl)) if entry > sl else qty_by_cap
        return max(1, min(qty_by_cap, qty_by_risk))

    def _a_blocked(self, m: int, now: float) -> str:
        if not any(s <= m < e for s, e in A_WINDOWS):
            return "সময়ের বাইরে"
        nchg = self.nifty_chg()
        if nchg is not None and nchg <= -0.8:
            return "Nifty দুর্বল"
        mine = [a for a in self.alerts if _is_a(a) and a["status"] != "SKIPPED"]
        if len(mine) >= int(_envf("STOCK_DAILY_CAP", 5)):
            return "দৈনিক সীমা"
        if sum(1 for x in self.stamps if now - x < 3600) >= 2:
            return "ঘণ্টার সীমা"
        if sum(1 for a in mine if a["status"] in ("PENDING", "OPEN")) >= int(_envf("STOCK_MAX_OPEN", 3)):
            return "খোলা ট্রেড বেশি"
        if now < self.pause_until:
            return "পরপর SL-এর পর বিরতি"
        if sum(a.get("r") or 0 for a in mine if a["status"] in CLOSED) <= -3:
            return "দৈনিক লস সীমা"
        return ""

    def _fire(self, sig: Dict[str, Any], now: float, tstr: str, m: int) -> Optional[Dict[str, Any]]:
        sym = sig["sym"]
        if self.cool.get(sym, 0.0) > now:
            return None
        grade = "A" if (sig["score"] >= _envf("STOCK_MIN_SCORE", 80) and sig["r5"] >= 2.5) else "B"
        why_not = ""
        if grade == "A":
            why_not = self._a_blocked(m, now)
            if why_not:
                grade = "B"
        if any(a["sym"] == sym and (a.get("grade", "A") == grade) for a in self.alerts):
            return None
        if grade == "B" and sum(1 for a in self.alerts if a.get("grade") == "B") >= 60:
            return None
        a = {**sig, "id": f"{self.date}-{len(self.alerts) + 1}", "ts": now, "time": tstr, "grade": grade,
             "alerted": grade == "A", "in_window": any(s <= m < e for s, e in A_WINDOWS), "blocked": why_not,
             "status": "PENDING" if grade == "A" else "OPEN", "exit": None, "r": None, "signal_px": sig["entry"],
             "qty": self._qty(sig["entry"], sig["sl"]), "research": None}
        self.alerts.append(a)
        self.cool[sym] = now + COOLDOWN_S
        self._save()
        if grade == "A":
            self.stamps = [x for x in self.stamps if now - x < 3600] + [now]
            if self.research:
                try:
                    self._spawn(self._finish_a(a))
                    return a
                except RuntimeError:
                    pass
            self._activate_a(a, None)
        return a

    async def _finish_a(self, a: Dict[str, Any]) -> None:
        flag = None
        try:
            flag = await asyncio.wait_for(self.research(a["sym"]), timeout=8.0)
        except Exception:
            flag = None
        self._activate_a(a, flag)

    def _activate_a(self, a: Dict[str, Any], flag: Optional[Dict[str, str]]) -> None:
        if a["status"] != "PENDING":
            return
        if flag:
            flag = {"sev": str(flag.get("sev", "")), "note": html.unescape(str(flag.get("note", ""))),
                    "pos": html.unescape(str(flag.get("pos", "")))}
        st = self.state.get(a["sym"]) or {}
        ltp = st.get("ltp")
        reason = ""
        if flag and flag["sev"] == "high":
            reason = "খবর-ঝুঁকি: " + (flag["note"] or "রিসার্চে বড় ঝুঁকি")
        elif ltp is None or ltp < a["signal_px"] * 0.997:
            reason = "সিগন্যালের পর দাম ফিরে গেছে"
        if reason:
            a["status"], a["skip"] = "SKIPPED", reason
            self._save()
            self._emit(f"🚫 <b>{_esc(a['sym'])}</b> সিগন্যাল এসেছিল, কিন্তু বাদ দিলাম: {_esc(reason)}")
            return
        entry = ltp
        sl = round(entry * (1 - a["risk"]), 2)
        r = entry - sl
        a.update(entry=entry, sl=sl, orig_sl=sl, t1=round(entry + 1.5 * r, 2), t2=round(entry + 2.5 * r, 2),
                 t1_hit=False, tsl_stage=0, qty=self._qty(entry, sl), status="OPEN",
                 research=flag or {"sev": "unknown", "note": "", "pos": ""})
        self._save()
        self._emit(self._alert_text(a))

    def _research_line(self, a: Dict[str, Any]) -> str:
        f = a.get("research") or {}
        if f.get("sev") == "unknown" or not f:
            return "🔎 খবর-রিসার্চ: পাওয়া যায়নি, নিজে একবার দেখে নিও"
        bits = []
        if f.get("note"):
            bits.append(f"⚠️ {_esc(f['note'])}")
        if f.get("pos"):
            bits.append(f"✨ {_esc(f['pos'])}")
        return "🔎 খবর-রিসার্চ: " + (" ".join(bits) if bits else "✅ বড় কোনো ঝুঁকি পাওয়া যায়নি")

    def _alert_text(self, a: Dict[str, Any]) -> str:
        risk = a["entry"] - a["sl"]
        budget = _envf("INTRADAY_RISK_INR", 300.0)
        head = "🚀 <b>BOOM মোমেন্টাম</b>" if a["kind"] == "BOOM" else "⚡ <b>মোমেন্টাম অ্যালার্ট</b>"
        nf = f"\nNifty আজ: {a['nifty']:+.2f}%" if a.get("nifty") is not None else ""
        return (f"{head}: <b>{_esc(a['sym'])}</b> (স্কোর {a['score']}, A-গ্রেড)\n"
                f"দাম ₹{a['entry']:,.2f} ({a['chg']:+.2f}% আজ) | ভলিউম {a['vol_x']}x\n"
                f"Entry ₹{a['entry']:,.2f} | SL ₹{a['sl']:,.2f} ({-(risk / a['entry'] * 100):.2f}%)\n"
                f"T1 ₹{a['t1']:,.2f} | T2 ₹{a['t2']:,.2f}\n"
                f"কেন: " + " • ".join(_esc(w) for w in a["why"]) + nf + "\n"
                + self._research_line(a) + "\n"
                f"₹{budget:,.0f} রিস্কে (5x) ≈ {a['qty']} শেয়ার | ⏱ অটো ট্রেলিং SL + T1→T2 চালু\n"
                f"<i>স্ক্যানার সিগন্যাল, গ্যারান্টি নয়। SL ছাড়া ঢুকো না।</i>")

    # ---------------------------------------------------------------- outcomes & Trailing SL (T1 -> T2)
    def _track(self, sym: str, ltp: float, now: float) -> None:
        for a in self.alerts:
            if a["sym"] != sym or a["status"] != "OPEN":
                continue

            entry = a["entry"]
            t1 = a["t1"]
            t2 = a.get("t2") or round(entry + 2.5 * (entry - a.get("orig_sl", a["sl"])), 2)
            orig_sl = a.setdefault("orig_sl", a["sl"])
            risk = entry - orig_sl
            stage = a.get("tsl_stage", 0)

            if risk > 0:
                # Stage 1: Reached 50% of T1 -> Move SL to Break-Even (Entry + 0.05% buffer)
                if stage < 1 and ltp >= entry + 0.5 * (t1 - entry):
                    new_sl = round(entry * 1.0005, 2)
                    if new_sl > a["sl"]:
                        a["sl"] = new_sl
                        a["tsl_stage"] = 1
                        self._save()
                        if _is_a(a):
                            self._emit(f"🛡️ <b>{_esc(sym)}</b> ৫০% T1 পথে! SL টেনে <b>₹{new_sl:,.2f} (Cost-to-Cost)</b>-এ আনা হলো।")

                # Stage 2: Reached 80% of T1 -> Lock +0.5R profit
                if stage < 2 and ltp >= entry + 0.8 * (t1 - entry):
                    lock_sl = round(entry + 0.5 * risk, 2)
                    if lock_sl > a["sl"]:
                        a["sl"] = lock_sl
                        a["tsl_stage"] = 2
                        self._save()
                        if _is_a(a):
                            self._emit(f"🔒 <b>{_esc(sym)}</b> লাভে সুরক্ষিত! SL তুলে <b>₹{lock_sl:,.2f} (+0.5R)</b> লক করা হলো।")

                # Stage 3: T1 Hit! DO NOT CLOSE — Lock SL at T1 (minus tiny 0.1% wiggle room) and trail towards T2!
                if stage < 3 and ltp >= t1:
                    t1_lock = round(t1 * 0.999, 2)
                    if t1_lock > a["sl"]:
                        a["sl"] = t1_lock
                    a["t1_hit"] = True
                    a["tsl_stage"] = 3
                    self._save()
                    if _is_a(a):
                        self._emit(f"🎯 <b>{_esc(sym)}</b> T1 (₹{t1:,.2f}) ছুঁয়েছে! প্রফিট লক করে SL <b>₹{a['sl']:,.2f}</b>-এ বসানো হলো — এবার টার্গেট <b>T2 (₹{t2:,.2f})</b>!")

                # Stage 4: Halfway between T1 and T2 -> Trail SL to halfway between T1 and T2
                if stage == 3 and ltp >= t1 + 0.5 * (t2 - t1):
                    mid_lock = round(t1 + 0.4 * (t2 - t1), 2)
                    if mid_lock > a["sl"]:
                        a["sl"] = mid_lock
                        a["tsl_stage"] = 4
                        self._save()
                        if _is_a(a):
                            self._emit(f"🚀 <b>{_esc(sym)}</b> T2-এর দিকে ছুটছে! SL আরও উপরে তুলে <b>₹{mid_lock:,.2f}</b> লক করা হলো।")

            # Exit conditions: Either Trailing/Initial SL hit OR Final T2 Target hit
            if ltp <= a["sl"]:
                # If T1 was already hit OR trailing SL is above entry, it closes as a WIN!
                status = "WIN" if (a.get("t1_hit") or a["sl"] >= entry) else "LOSS"
                self._close(a, status, ltp, now)
            elif ltp >= t2:
                self._close(a, "WIN", ltp, now)

    def _close(self, a: Dict[str, Any], status: str, px: float, now: float) -> None:
        orig_sl = a.get("orig_sl", a["sl"])
        risk = a["entry"] - orig_sl
        a["status"], a["exit"], a["closed_at"] = status, px, now
        a["r"] = round((px - a["entry"]) / risk, 2) if risk > 0 else 0.0
        self._save()
        if not _is_a(a):
            return
        money = f" · ₹{(px - a['entry']) * (a.get('qty') or 1):+,.0f}"
        if status == "WIN":
            if px >= (a.get("t2") or 1e18):
                self._emit(f"🏆 <b>{_esc(a['sym'])}</b> ফাইনাল টার্গেট <b>T2</b> ছুঁয়েছে ({a['r']:+.1f}R{money})! সম্পূর্ণ প্রফিট বুকড।")
            elif a.get("t1_hit"):
                self._emit(f"✅ <b>{_esc(a['sym'])}</b> T1 প্রফিট লক করা ট্রেলিং SL-এ বুক হয়েছে ({a['r']:+.1f}R{money})।")
            else:
                self._emit(f"🛡️ <b>{_esc(a['sym'])}</b> ট্রেলিং SL-এ নিরাপদে বন্ধ হয়েছে ({a['r']:+.1f}R{money})।")
        elif status == "LOSS":
            self._emit(f"❌ <b>{_esc(a['sym'])}</b> SL ছুঁয়েছে ({a['r']:+.1f}R{money})। প্ল্যান অনুযায়ী বের হও।")
        done = sorted((x for x in self.alerts if _is_a(x) and x["status"] in CLOSED), key=lambda x: x.get("closed_at", 0))
        if status == "LOSS" and len(done) >= 2 and done[-1]["status"] == "LOSS" and done[-2]["status"] == "LOSS":
            self.pause_until = now + 3600
            self._emit("⏸️ পরপর দুটো SL লেগেছে। ১ ঘণ্টা নতুন অ্যালার্ট বন্ধ, মাথা ঠান্ডা রাখো।")
        if sum(x.get("r") or 0 for x in done) <= -3 and not self.loss_notice:
            self.loss_notice = True
            self._emit("🛑 আজকের লস সীমা (−3R) ছুঁয়েছে। আজ আর নতুন অ্যালার্ট আসবে না।")

    def _live(self, a: Dict[str, Any]) -> Optional[Dict[str, float]]:
        if a["status"] in CLOSED:
            px = a.get("exit")
        elif a["status"] == "OPEN":
            px = (self.state.get(a["sym"]) or {}).get("ltp") or a["entry"]
        else:
            return None
        if px is None or a["entry"] <= 0:
            return None
        qty = a.get("qty") or self._qty(a["entry"], a["sl"])
        orig_sl = a.get("orig_sl", a["sl"])
        risk = a["entry"] - orig_sl
        return {"px": px, "pnl": (px - a["entry"]) * qty, "pct": (px / a["entry"] - 1) * 100,
                "r": (px - a["entry"]) / risk if risk > 0 else 0.0}

    def summary(self) -> Dict[str, Any]:
        mine = [a for a in self.alerts if _is_a(a)]
        done = [a for a in mine if a["status"] in CLOSED]
        wins = [a for a in done if a["status"] == "WIN"]
        real = unreal = 0.0
        for a in mine:
            lv = self._live(a)
            if lv:
                if a["status"] in CLOSED:
                    real += lv["pnl"]
                else:
                    unreal += lv["pnl"]
        return {"total": len([a for a in mine if a["status"] != "SKIPPED"]), "open": sum(1 for a in mine if a["status"] in ("OPEN", "PENDING")),
                "win": len(wins), "loss": sum(1 for a in done if a["status"] == "LOSS"),
                "time_exit": sum(1 for a in done if a["status"] == "TIME"),
                "skipped": sum(1 for a in mine if a["status"] == "SKIPPED"),
                "total_r": round(sum(a["r"] or 0 for a in done), 2),
                "win_rate": round(len(wins) / len(done) * 100) if done else None,
                "realized": round(real), "unrealized": round(unreal), "pnl": round(real + unreal)}

    def grade_stats(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for g in ("A", "B"):
            xs = [a for a in self.alerts if a.get("grade", "A") == g and a["status"] in CLOSED]
            w = sum(1 for a in xs if a["status"] == "WIN")
            out[g] = {"n": len(xs), "win": w, "loss": sum(1 for a in xs if a["status"] == "LOSS"),
                      "avg_r": round(sum(a["r"] or 0 for a in xs) / len(xs), 2) if xs else None,
                      "total": sum(1 for a in self.alerts if a.get("grade", "A") == g)}
        return out

    def finish_day(self) -> str:
        now = self.clock()
        for a in self.alerts:
            if a["status"] == "OPEN":
                px = (self.state.get(a["sym"]) or {}).get("ltp") or a["entry"]
                status = "WIN" if (a.get("t1_hit") or px > a["entry"]) else "TIME"
                self._close(a, status, px, now)
            elif a["status"] == "PENDING":
                a["status"], a["skip"] = "SKIPPED", "বাজার বন্ধ"
        self.eod_done = True
        self._save()
        s, g = self.summary(), self.grade_stats()
        mine = [a for a in self.alerts if _is_a(a) and a["status"] in CLOSED]
        if not mine:
            return "📊 আজ কোনো A-গ্রেড অ্যালার্ট হয়নি (কড়া ফিল্টার, তাই কম সুযোগ)।" + \
                   (f"\nছায়া (B) সিগন্যাল: {g['B']['total']}টি, শুধু পরিসংখ্যানের জন্য।" if g["B"]["total"] else "")
        icon = {"WIN": "✅", "LOSS": "❌", "TIME": "⏱"}
        lines = "\n".join(f"{icon.get(a['status'], '•')} {_esc(a['sym'])} {a['time']} → {a['r']:+.1f}R · ₹{self._live(a)['pnl']:+,.0f}"
                          for a in mine)
        wr = f"{s['win_rate']}%" if s["win_rate"] is not None else "-"
        b = g["B"]
        return (f"📊 <b>আজকের ইনট্রাডে ফলাফল</b>\nA-অ্যালার্ট {s['total']} | ✅ {s['win']} | ❌ {s['loss']} | ⏱ {s['time_exit']} | "
                f"win-rate {wr} | {s['total_r']:+.1f}R\n<b>মোট P&L ₹{s['pnl']:+,}</b> (ফি/ট্যাক্স ছাড়া)\n{lines}\n"
                f"ছায়া (B) সিগন্যাল: {b['total']}টি, বন্ধ {b['n']}টির জয় {b['win']}, হার {b['loss']}")

    def journal(self) -> Dict[str, Any]:
        rows = []
        for a in self.alerts:
            if not _is_a(a):
                continue
            lv = self._live(a) or {}
            f = a.get("research") or {}
            rows.append({"id": a["id"], "sym": a["sym"], "grade": "A", "kind": a.get("kind"), "score": a["score"],
                         "time": a["time"], "entry": a["entry"], "sl": a["sl"], "t1": a["t1"], "t2": a["t2"],
                         "t1_hit": a.get("t1_hit", False), "tsl_stage": a.get("tsl_stage", 0),
                         "status": a["status"], "qty": a.get("qty"), "ltp": lv.get("px"), "pnl": lv.get("pnl"),
                         "pct": lv.get("pct"), "r": lv.get("r") if a["status"] == "OPEN" else a.get("r"),
                         "why": a.get("why"), "skip": a.get("skip"), "note": f.get("note") or f.get("pos") or ""})
        return {"date": self.date, "summary": self.summary(), "grades": self.grade_stats(), "alerts": rows,
                "risk_inr": _envf("INTRADAY_RISK_INR", 300.0)}


# ======================================================================= runner
class StockLiveRunner:
    def __init__(self, send: Callable[[str], Awaitable[Any]], feed_factory: Optional[Callable[[], Any]] = None,
                 symbols: Optional[List[str]] = None) -> None:
        self.send = send
        self.engine = LiveEngine(send, research=None if os.getenv("STOCK_RESEARCH", "1") == "0" else self._research)
        self.feed_factory = feed_factory or af.AngelFeed
        self.symbols = symbols or universe.NSE_LIVE_UNIVERSE
        self.feed: Any = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.day = ""
        self.prepared = self.streaming = False
        self.tries, self.restarts, self.last_try, self.stream_started = 0, 0, 0.0, 0.0
        self.error = ""
        self.stage = ""
        self.skipped: Dict[str, List[str]] = {}
        self.sent: set = set()
        self._cache: Tuple[str, Dict, Dict, Optional[float]] = ("", {}, {}, None)

    async def run_forever(self) -> None:
        self.loop = asyncio.get_running_loop()
        while True:
            try:
                await self.step()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:160]
                print("stock live error:", repr(exc))
            await asyncio.sleep(15)

    async def _research(self, sym: str) -> Optional[Dict[str, str]]:
        day = af.ist_now().strftime("%d %b %Y, %A")
        return (await stock_premarket.gemini_flags(day, [sym])).get(sym)

    def _stage(self, text: str) -> None:
        self.stage = text
        if text:
            print("stock live:", text)

    async def _warn(self, key: str, text: str) -> None:
        if key not in self.sent:
            self.sent.add(key)
            await self.send(text)

    async def step(self, ist: Optional[datetime.datetime] = None) -> None:
        ist = ist or af.ist_now()
        m, day = ist.hour * 60 + ist.minute, ist.strftime("%Y-%m-%d")
        if day != self.day:
            await self._stop_stream()
            self.day, self.prepared, self.tries, self.restarts, self.sent = day, False, 0, 0, set()
            self.last_try = 0.0
            self.engine.reset_day(day)
        if ist.weekday() >= 5 or not af.configured():
            return
        if not self.prepared and 520 <= m < 920 and self.tries < 12 \
                and time.time() - self.last_try >= (60 if self.tries < 3 else 300):
            await self._prepare()
        if self.prepared and not self.streaming and 535 <= m < 935:
            await self._start_stream()
        if not self.streaming:
            return
        feed = self.feed
        if feed.n_ticks > 0:
            await self._warn("live_ok", f"🟢 Angel One থেকে লাইভ ডেটা আসছে, {len(self.engine.base)}টি শেয়ার নজরে।")
        if m >= 565 and feed.n_ticks == 0 and time.time() - self.stream_started > 300:
            await self._warn("quiet", "ℹ️ বাজার খোলার পরেও কোনো লাইভ ডেটা আসেনি। আজ NSE ছুটি হতে পারে, অথবা Angel ফিড সমস্যা করছে।")
        if 560 <= m < CLOSE_M and feed.n_ticks > 0 and time.time() - max(feed.last_tick, self.stream_started) > 120:
            if self.restarts < 6:
                self.restarts += 1
                await self._stop_stream()
                self.prepared = False
                self.last_try = 0.0
                await self._warn("restart", "⚠️ লাইভ ফিড ২ মিনিট নীরব ছিল, আবার কানেক্ট করছি।")
        if m >= 920 and not self.engine.eod_done:
            await self.send(self.engine.finish_day())
        if m >= 935:
            await self._stop_stream()

    async def _prepare(self) -> None:
        self.tries += 1
        self.last_try = time.time()
        try:
            self._stage("Angel One-এ লগইন হচ্ছে")
            feed = self.feed_factory()
            await asyncio.to_thread(feed.login)
            day, base, tok2sym, npc = self._cache
            if day != self.day or not base:
                self._stage("শেয়ারের তালিকা আনা হচ্ছে")
                async with httpx.AsyncClient() as client:
                    tokens = await af.load_equity_tokens(client, self.symbols)
                base, tok2sym, npc = await asyncio.to_thread(self._load_baselines, feed, tokens)
                if len(base) < 20:
                    raise RuntimeError(f"only {len(base)} stocks had usable daily data")
                self._cache = (self.day, base, tok2sym, npc)
            self.engine.setup(base, tok2sym, npc)
            self.feed, self.prepared, self.error = feed, True, ""
            self._stage("")
            print(f"stock live: ready, {len(base)} stocks")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:160]
            self._stage("")
            print("stock live prepare failed:", self.error)
            if self.tries in (1, 4, 8):
                await self.send(f"⚠️ Angel One লাইভ ফিড চালু হয়নি: {_esc(self.error)}\n"
                                f"(Render env-এ ANGEL_* মান আর TOTP secret ঠিক আছে কি না দেখো)")

    def _load_baselines(self, feed: Any, tokens: Dict[str, str]) -> Tuple[Dict, Dict, Optional[float]]:
        ist = af.ist_now()
        frm = (ist - datetime.timedelta(days=45)).strftime("%Y-%m-%d 09:15")
        to = (ist - datetime.timedelta(days=1)).strftime("%Y-%m-%d 15:30")
        base, tok2sym = {}, {}
        for n, (sym, tok) in enumerate(tokens.items(), 1):
            self.stage = f"আগের দিনের ডেটা {n}/{len(tokens)}"
            b = compute_baseline(feed.historical(tok, "ONE_DAY", frm, to))
            if b:
                base[sym], tok2sym[tok] = b, sym
            time.sleep(0.35)
        for sym, tok in [(s, t) for s, t in tokens.items() if s not in base]:
            self.stage = f"ফেল হওয়াগুলো আবার চেষ্টা: {sym}"
            b = compute_baseline(feed.historical(tok, "ONE_DAY", frm, to))
            if b:
                base[sym], tok2sym[tok] = b, sym
            time.sleep(1.0)
        self.skipped = {"no_token": [s for s in self.symbols if s not in tokens][:60],
                        "no_data": [s for s in tokens if s not in base][:60]}
        rows = feed.historical(af.NIFTY_HIST_TOKEN, "ONE_DAY", frm, to)
        try:
            npc = float(rows[-1][4]) if rows else None
        except (TypeError, ValueError, IndexError):
            npc = None
        return base, tok2sym, npc

    async def _start_stream(self) -> None:
        loop = self.loop or asyncio.get_running_loop()
        engine = self.engine

        def cb(t: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(engine.on_tick, t)

        try:
            self.feed.start(list(engine.tok2sym.keys()), [af.NIFTY_WS_TOKEN], cb)
            self.streaming, self.stream_started = True, time.time()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:160]
            self.prepared = False

    async def _stop_stream(self) -> None:
        if self.feed is not None:
            await asyncio.to_thread(self.feed.stop)
        self.streaming = False

    def live_price(self, sym: str) -> Optional[float]:
        st = self.engine.state.get(sym)
        if st and st.get("ltp") and self.engine.clock() - st["ts"] < 45:
            return st["ltp"]
        return None

    def snapshot(self) -> Dict[str, Any]:
        e = self.engine
        now = e.clock()
        px = {s: round(st["ltp"], 2) for s, st in e.state.items() if st.get("ltp") and now - st["ts"] < 90}
        return {"ts": int(now), "st": self.status(), "j": e.journal(), "px": px, "nifty": e.nifty["ltp"]}

    def status(self) -> Dict[str, Any]:
        f = self.feed
        age = round(time.time() - f.last_tick) if f is not None and f.last_tick else None
        return {"configured": af.configured(), "prepared": self.prepared, "streaming": self.streaming,
                "connected": bool(f.connected) if f is not None else False, "symbols": len(self.engine.base),
                "ticks": f.n_ticks if f is not None else 0, "last_tick_age_s": age,
                "alerts_today": self.engine.summary()["total"], "nifty": self.engine.nifty["ltp"],
                "stage": self.stage, "tries": self.tries, "skipped": self.skipped,
                "error": self.error or (f.error if f is not None else "")}
