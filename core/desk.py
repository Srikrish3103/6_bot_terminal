"""The desk: runs the six agents in order for one cycle across all segments."""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from agents.base import Report
from agents.kestrel import Kestrel
from agents.prism import Prism
from agents.herald import Herald
from agents.pulse import Pulse
from agents.aegis import Aegis
from agents.arbiter import Arbiter
from .data import fetch_candles, synthetic_candles, fx_rates, hist_vol, FX_FALLBACK
from .derivs import option_structure, linear_structure
from .feeds import LiveFeeds, DemoFeeds
from .instruments import build_universe
from .paper import PaperBook
from .sources import Delta, fnum

log = logging.getLogger("desk")
SPOT = ("us_spot", "in_spot", "crypto_spot")


class Desk:
    def __init__(self, cfg: dict, root: Path, demo: bool = False):
        self.cfg, self.root, self.demo = cfg, root, demo
        self.kestrel, self.prism = Kestrel(cfg), Prism(cfg)
        self.herald, self.pulse = Herald(cfg), Pulse(cfg)
        self.aegis, self.arbiter = Aegis(cfg), Arbiter(cfg)
        self.feeds = DemoFeeds(cfg) if demo else LiveFeeds(cfg)
        self.base = cfg["account"]["base_currency"]
        db = root / "data" / ("paper_demo_v2.db" if demo else "paper_v2.db")
        self.book = PaperBook(db, cfg["account"]["starting_equity"], self.base)
        self.universe, self.umap, self.data, self.rates = [], {}, {}, dict(FX_FALLBACK)

    # ------------------------------------------------------------------ helpers
    def last_prices(self):
        return {k: float(df["Close"].iloc[-1]) for k, df in self.data.items()}

    def structure(self, u, plan: dict, deriv: dict, df, equity: float = 0.0):
        oc = self.cfg["options"]
        st, note = None, None
        rate = 0.0
        if u.segment in SPOT:
            st = linear_structure(u, plan, "SPOT")
        elif u.segment == "crypto_fut":
            st = linear_structure(u, plan, "FUTURE")
        elif u.segment in ("in_fno", "crypto_opt"):
            crypto = u.segment == "crypto_opt"
            rate = oc["rate_crypto"] if crypto else oc["rate_in"]
            ch = deriv.get("chain")
            if ch is not None:
                from .data import convert
                budget = convert(equity * self.cfg["aegis"]["risk_per_trade_pct"] / 100, self.base, u.ccy, self.rates)
                st = option_structure(u, plan, ch, hist_vol(df, crypto=crypto), oc, rate, budget or None)
                if st is None:
                    note = "no liquid strikes for a defined-risk structure"
            else:
                note = deriv.get("error") or "options disabled"
            if st is None and u.segment == "in_fno":
                st = linear_structure(u, plan, "FUTURE")
                note = f"{note} → futures"
        if st is None:
            return None, note
        st["direction"] = plan["direction"]
        st["rate"] = rate
        st.setdefault("und_entry", plan["entry"])
        st.setdefault("und_stop", plan["stop"])
        st.setdefault("und_target", plan["target"])
        return st, note

    def live_quotes(self, keys) -> dict:
        """Latest underlying prices for the live terminal tick."""
        if self.demo:
            out = {}
            for k in keys:
                df = self.data.get(k)
                if df is None:
                    continue
                last = getattr(self, "_demo_last", {}).get(k, float(df["Close"].iloc[-1]))
                vol = float((df["High"] - df["Low"]).tail(14).mean() / df["Close"].iloc[-1]) / 6
                out[k] = last * (1 + random.gauss(0, vol))
            self._demo_last = {**getattr(self, "_demo_last", {}), **out}
            return out
        out = {}
        yf_map = {self.umap[k].data_symbol: k for k in keys if k in self.umap and self.umap[k].source == "yf"}
        dl = {k for k in keys if k in self.umap and self.umap[k].source == "delta"}
        if yf_map:
            import yfinance as yf
            try:
                raw = yf.download(list(yf_map), period="5d", interval="1m", group_by="ticker",
                                  auto_adjust=True, threads=True, progress=False)
                for s, k in yf_map.items():
                    try:
                        c = (raw[s] if len(yf_map) > 1 or hasattr(raw.columns, "levels") else raw)["Close"].dropna()
                        if len(c):
                            out[k] = float(c.iloc[-1])
                    except Exception:  # noqa
                        pass
            except Exception as e:  # noqa
                log.debug("yf quotes: %s", e)
        if dl:
            marks = {t.get("symbol"): fnum(t.get("mark_price")) for t in
                     Delta(self.cfg["http_timeout"]).tickers("perpetual_futures")}
            for k in dl:
                v = marks.get(self.umap[k].data_symbol)
                if v:
                    out[k] = v
        return out

    # ------------------------------------------------------------------ cycle
    def cycle(self, execute: bool = True, on=None, pace: float = 0.0) -> dict:
        """on(agent, state, msg) receives progress events (used by the live terminal)."""
        emit = on or (lambda *a: None)

        def step(agent, msg):
            emit(agent, "work", msg)
            if pace:
                time.sleep(pace)

        t0 = datetime.now()
        step("KESTREL", "Building universe (spot · F&O · crypto derivs)…")
        self.universe = build_universe(self.cfg, self.root, self.demo)
        self.umap = {u.key: u for u in self.universe}
        step("KESTREL", f"Downloading {len(self.universe)} markets…")
        data = synthetic_candles(self.universe) if self.demo else fetch_candles(
            self.universe, self.cfg["kestrel"]["history"], self.cfg["http_timeout"])
        if not data:
            emit("KESTREL", "error", "No market data")
            raise RuntimeError("No market data — check internet / yfinance / Delta")
        self.data = data
        self.rates = fx_rates(live=not self.demo)
        prices = self.last_prices()
        highs = {k: float(df["High"].iloc[-1]) for k, df in data.items()}
        lows = {k: float(df["Low"].iloc[-1]) for k, df in data.items()}
        for p, why, pnl in self.book.check_exits(prices, self.rates, highs, lows):
            emit("BOOK", "trade", f"CLOSED {p['label']} at {why} → P&L {self.base} {pnl:+,.0f}")
        snap = self.book.snapshot(prices, self.rates)

        # 1) KESTREL
        step("KESTREL", f"Ranking {len(data)} assets…")
        scan_rows, picks = self.kestrel.run(data, self.umap)
        seg_count = {}
        for r in scan_rows:
            seg_count[r["seg"]] = seg_count.get(r["seg"], 0) + 1
        emit("KESTREL", "done", f"{len(data)} scanned ({', '.join(f'{k} {v}' for k, v in seg_count.items())}) · "
                                f"{len(picks)} picks")

        # 3+4) HERALD / PULSE / derivatives fetches in parallel (network bound)
        step("HERALD", f"Fetching news for {len(picks)} picks…")
        step("PULSE", "Reading crowd + option chains / funding…")
        pu = [self.umap[p.symbol] for p in picks]
        with ThreadPoolExecutor(max_workers=6) as ex:
            news = dict(zip([u.key for u in pu], ex.map(self.feeds.news, pu)))
            crowd = dict(zip([u.key for u in pu], ex.map(self.feeds.crowd, pu)))
            derivs = dict(zip([u.key for u in pu], ex.map(lambda u: self.feeds.deriv(u, data[u.key]), pu)))

        results = []
        for k in picks:
            key = k.symbol
            u = self.umap[key]
            tag = f"{u.label} {u.symbol}"
            direction = k.details["direction"]
            reps = {"kestrel": k}
            step("PRISM", f"{tag}: reading chart ({direction})")
            reps["prism"] = self.prism.analyse(key, data[key], direction)                     # 2) PRISM
            emit("PRISM", "done", f"{tag}: {reps['prism'].verdict} {reps['prism'].score:+.2f}")
            step("HERALD", f"{tag}: scoring {len(news.get(key, []))} headlines")
            reps["herald"] = self.herald.analyse(key, news.get(key, []))                      # 3) HERALD
            emit("HERALD", "done", f"{tag}: {reps['herald'].verdict} {reps['herald'].score:+.2f}")
            step("PULSE", f"{tag}: {len(crowd.get(key, []))} posts + positioning")
            reps["pulse"] = self.pulse.analyse(key, crowd.get(key, []), data[key], derivs.get(key))  # 4) PULSE
            emit("PULSE", "done", f"{tag}: {reps['pulse'].verdict} {reps['pulse'].score:+.2f}")

            st, note = self.structure(u, reps["prism"].details, derivs.get(key) or {}, data[key], snap["equity"])
            step("AEGIS", f"{tag}: sizing {st['name'] if st else 'n/a'}")
            if st:
                reps["aegis"] = self.aegis.review(u, st, snap, data, self.rates, self.base)    # 5) AEGIS
            else:
                reps["aegis"] = Report("AEGIS", "Risk officer", key, -1, "REJECTED", "HIGH",
                                       {"checks": {"Structure": {"pass": False, "detail": note}},
                                        "fails": [f"No structure ({note})"], "already_holding": False, "units": 0})
            fails = reps["aegis"].details["fails"]
            emit("AEGIS", "done", f"{tag}: {reps['aegis'].verdict}" + (f" ({', '.join(fails)})" if fails else ""))
            step("ARBITER", f"{tag}: weighing 5 reports")
            final = self.arbiter.decide(key, direction, reps)                                  # 6) ARBITER
            if st and note:
                final.details["reason"] += f" · {note}"
            reps["arbiter"] = final
            emit("ARBITER", "decision", f"{tag} {direction} → {final.verdict} ({final.details['composite']:+.2f}) "
                                        f"{st['label'] if st else ''} · {final.details['reason']}")
            self.book.log_decision(key, direction, st["type"] if st else None, final.verdict,
                                   final.details["composite"], final.details["reason"])
            if execute and final.verdict == "TRADE" and st:
                a = reps["aegis"].details
                self.book.open(u, direction, st, a["units"], a["loss_at_stop_base"], st.get("rate", 0))
                emit("BOOK", "trade", f"OPENED {st['name']} {st['label']} ×{a['units']:,.4g} · "
                                      f"max loss {self.base} {a['loss_at_stop_base']:,.0f}")
                snap = self.book.snapshot(prices, self.rates)   # later picks see the new exposure
            results.append({"key": key, "symbol": u.symbol, "segment": u.segment, "seg": u.label,
                            "direction": direction, "structure": st, "structure_note": note,
                            "positioning": (reps["pulse"].details or {}).get("positioning"),
                            "reports": {k2: v.to_dict() for k2, v in reps.items()}})

        for a in ("HERALD", "PULSE", "PRISM", "AEGIS", "ARBITER"):
            emit(a, "idle", "")
        return {"ts": t0.isoformat(timespec="seconds"), "demo": self.demo, "base": self.base,
                "secs": round((datetime.now() - t0).total_seconds(), 1),
                "scanned": len(scan_rows), "scan": scan_rows[:30], "results": results,
                "book": self.book.snapshot(prices, self.rates), "closed": self.book.closed_positions(10)}
