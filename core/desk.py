"""The desk: runs the six agents in order for one cycle."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from agents.kestrel import Kestrel
from agents.prism import Prism
from agents.herald import Herald
from agents.pulse import Pulse
from agents.aegis import Aegis
from agents.arbiter import Arbiter
from .data import fetch_ohlcv, synthetic_ohlcv, fx_rates
from .feeds import LiveFeeds, DemoFeeds
from .paper import PaperBook

log = logging.getLogger("desk")


class Desk:
    def __init__(self, cfg: dict, root: Path, demo: bool = False):
        self.cfg, self.root, self.demo = cfg, root, demo
        self.kestrel, self.prism = Kestrel(cfg), Prism(cfg)
        self.herald, self.pulse = Herald(cfg), Pulse(cfg)
        self.aegis, self.arbiter = Aegis(cfg), Arbiter(cfg)
        self.feeds = DemoFeeds(cfg) if demo else LiveFeeds(cfg)
        db = root / "data" / ("paper_demo.db" if demo else "paper.db")
        self.book = PaperBook(db, cfg["account"]["starting_equity"])
        self.data, self.rates = {}, {}

    def universe(self):
        return [s for group in self.cfg["universe"].values() for s in group]

    def cycle(self, execute: bool = True, on=None, pace: float = 0.0) -> dict:
        """on(agent, state, msg) receives progress events (used by the live terminal)."""
        emit = on or (lambda *a: None)

        def step(agent, msg):
            emit(agent, "work", msg)
            if pace:
                time.sleep(pace)

        t0 = datetime.now()
        syms = self.universe()
        step("KESTREL", f"Downloading {len(syms)} markets…")
        data = synthetic_ohlcv(syms) if self.demo else fetch_ohlcv(syms, self.cfg["kestrel"]["history"])
        if not data:
            emit("KESTREL", "error", "No market data")
            raise RuntimeError("No market data — check internet / yfinance")
        rates = fx_rates(data)
        self.data, self.rates = data, rates
        snap = self.book.mark(data, rates)

        # 1) KESTREL
        step("KESTREL", f"Ranking {len(data)} assets…")
        scan_rows, picks = self.kestrel.run(data)
        emit("KESTREL", "done", f"{len(data)} scanned · top: " + ", ".join(p.symbol for p in picks[:3]))

        # 3+4) HERALD / PULSE fetches in parallel (network bound)
        step("HERALD", f"Fetching news for {len(picks)} picks…")
        step("PULSE", "Reading the crowd…")
        with ThreadPoolExecutor(max_workers=8) as ex:
            news = dict(zip([p.symbol for p in picks], ex.map(self.feeds.news, [p.symbol for p in picks])))
            crowd = dict(zip([p.symbol for p in picks], ex.map(self.feeds.crowd, [p.symbol for p in picks])))

        results = []
        for k in picks:
            s = k.symbol
            direction = k.details["direction"]
            reps = {"kestrel": k}
            step("PRISM", f"{s}: reading chart ({direction})")
            reps["prism"] = self.prism.analyse(s, data[s], direction)                 # 2) PRISM
            emit("PRISM", "done", f"{s}: {reps['prism'].verdict} {reps['prism'].score:+.2f}")
            step("HERALD", f"{s}: scoring {len(news.get(s, []))} headlines")
            reps["herald"] = self.herald.analyse(s, news.get(s, []))                  # 3) HERALD
            emit("HERALD", "done", f"{s}: {reps['herald'].verdict} {reps['herald'].score:+.2f}")
            step("PULSE", f"{s}: {len(crowd.get(s, []))} posts")
            reps["pulse"] = self.pulse.analyse(s, crowd.get(s, []), data[s])          # 4) PULSE
            emit("PULSE", "done", f"{s}: {reps['pulse'].verdict} {reps['pulse'].score:+.2f}")
            step("AEGIS", f"{s}: risk review")
            reps["aegis"] = self.aegis.review(s, reps["prism"].details, snap, data, rates)  # 5) AEGIS
            fails = reps["aegis"].details["fails"]
            emit("AEGIS", "done", f"{s}: {reps['aegis'].verdict}" + (f" ({', '.join(fails)})" if fails else ""))
            step("ARBITER", f"{s}: weighing 5 reports")
            final = self.arbiter.decide(s, direction, reps)                           # 6) ARBITER
            reps["arbiter"] = final
            emit("ARBITER", "decision", f"{s} {direction} → {final.verdict} ({final.details['composite']:+.2f}) "
                                        f"{final.details['reason']}")
            self.book.log_decision(s, direction, final.verdict, final.details["composite"], final.details["reason"])
            if execute and final.verdict == "TRADE":
                p = reps["prism"].details
                self.book.open(s, direction, reps["aegis"].details["qty"], p["entry"], p["stop"], p["target"])
                emit("BOOK", "trade", f"OPENED {direction} {s} @ {p['entry']:,.4g} stop {p['stop']:,.4g} "
                                      f"target {p['target']:,.4g}")
                snap = self.book.snapshot(data, rates)        # later picks see the new exposure
            results.append({"symbol": s, "direction": direction, "reports": {k2: v.to_dict() for k2, v in reps.items()}})

        for a in ("HERALD", "PULSE", "PRISM", "AEGIS", "ARBITER"):
            emit(a, "idle", "")
        return {"ts": t0.isoformat(timespec="seconds"), "demo": self.demo,
                "secs": round((datetime.now() - t0).total_seconds(), 1),
                "scanned": len(scan_rows), "scan": scan_rows[:15], "results": results,
                "book": self.book.snapshot(data, rates), "closed": self.book.closed_positions(10)}
