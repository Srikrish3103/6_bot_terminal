"""Live terminal: full-screen dashboard that refreshes in place.

A background engine thread owns the Desk (and its SQLite book):
  * every `cycle_min` minutes  -> full 6-agent cycle (progress streamed to the screen)
  * every `tick_sec` seconds   -> fresh quotes, live P&L, live stop/target exits
The main thread only renders.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .data import CCY_SIGN
from .desk import Desk
from .instruments import SEG_COLOR, SEG_LABEL
from .view import html_report

log = logging.getLogger("live")

AGENTS = [("KESTREL", "#4fb3ff", "Scanner"), ("PRISM", "#b36bff", "Charts"), ("HERALD", "#ff6a3d", "News"),
          ("PULSE", "#3de0c4", "Crowd"), ("AEGIS", "#ff9a1f", "Risk"), ("ARBITER", "#ffd23d", "Decision")]
ACOL = {n: c for n, c, _ in AGENTS} | {"BOOK": "#4dff8a", "TICK": "#8d96c4", "SYSTEM": "#8d96c4"}
DEC = {"TRADE": "bold #4dff8a", "WAIT": "bold #ffd23d", "REJECT": "bold #ff5b5b", "HOLD": "bold #3de0c4"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def fp(x):
    return f"{x:,.2f}" if abs(x) >= 10 else f"{x:,.4f}"


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.agents = {n: {"state": "idle", "msg": "waiting", "t": time.time()} for n, _, _ in AGENTS}
        self.log = deque(maxlen=300)
        self.scan, self.results = [], []
        self.quotes: dict[str, dict] = {}       # sym -> px, prev, ref
        self.positions, self.realized, self.start, self.base = [], 0.0, 0.0, "INR"
        self.cycles, self.last_cycle, self.next_cycle, self.next_tick = 0, None, 0.0, 0.0
        self.busy, self.demo = False, False
        self.run = None                          # last full cycle (funnel, results, actions)

    def event(self, agent, state, msg):
        with self.lock:
            if agent in self.agents:
                self.agents[agent] = {"state": state, "msg": msg or self.agents[agent]["msg"], "t": time.time()}
            if state in ("done", "decision", "trade", "error", "info") and msg:
                self.log.append((datetime.now().strftime("%H:%M:%S"), agent, state, msg))


class Engine(threading.Thread):
    def __init__(self, cfg, root: Path, st: State, demo, execute, cycle_min, tick_sec, pace):
        super().__init__(daemon=True)
        self.cfg, self.root, self.st = cfg, root, st
        self.demo, self.execute, self.cycle_min, self.tick_sec, self.pace = demo, execute, cycle_min, tick_sec, pace
        self.stop_flag = threading.Event()

    # ---------------------------------------------------------------- quotes
    def tick(self):
        st, book = self.st, self.desk.book
        opens = book.open_positions()
        keys = sorted({r["key"] for r in st.scan[:14]} | {p["key"] for p in opens})
        if not keys:
            return
        try:
            new = self.desk.live_quotes(keys)
        except Exception as e:  # noqa
            st.event("TICK", "error", f"quote refresh failed: {e}")
            return
        with st.lock:
            for k, px in new.items():
                q = st.quotes.get(k)
                st.quotes[k] = {"px": px, "prev": q["px"] if q else px, "ref": q["ref"] if q else px, "t": time.time()}
        for p, why, pnl in book.check_exits(new, self.desk.rates):
            st.event("BOOK", "trade", f"CLOSED {p['name']} {p['label']} at {why} → P&L {st.base} {pnl:+,.0f}")
        self._refresh_book()

    def _refresh_book(self):
        st = self.st
        prices = self.desk.last_prices()
        with st.lock:
            prices.update({k: q["px"] for k, q in st.quotes.items()})
        snap = self.desk.book.snapshot(prices, self.desk.rates)
        with st.lock:
            st.positions, st.realized, st.start, st.base = snap["positions"], snap["realized"], snap["start"], snap["base"]

    # ---------------------------------------------------------------- cycle
    def do_cycle(self):
        st = self.st
        st.busy = True
        st.event("SYSTEM", "info", f"Cycle #{st.cycles + 1} started")
        try:
            run = self.desk.cycle(execute=self.execute, on=st.event, pace=self.pace)
            with st.lock:
                st.scan, st.results, st.run = run["scan"], run["results"], run
                for r in run["scan"]:
                    st.quotes[r["key"]] = {"px": r["price"], "prev": r["price"], "ref": r["price"], "t": time.time()}
                st.cycles += 1
                st.last_cycle = datetime.now()
            self._refresh_book()
            html_report(run, self.root / "reports" / "latest.html")
            st.event("SYSTEM", "info", f"Cycle done in {run['secs']}s · report → reports/latest.html")
        except Exception as e:  # noqa
            st.event("SYSTEM", "error", f"Cycle failed: {e}")
        finally:
            st.busy = False

    def run(self):
        self.desk = Desk(self.cfg, self.root, demo=self.demo)    # created in this thread (SQLite)
        self.st.demo = self.demo
        self._refresh_book()
        while not self.stop_flag.is_set():
            now = time.time()
            if now >= self.st.next_cycle:
                self.do_cycle()
                self.st.next_cycle = time.time() + self.cycle_min * 60
                self.st.next_tick = time.time() + self.tick_sec
            elif now >= self.st.next_tick:
                try:
                    self.tick()
                except Exception as e:  # noqa
                    self.st.event("TICK", "error", str(e))
                self.st.next_tick = time.time() + self.tick_sec
            time.sleep(0.2)


# ==================================================================== render
def _mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def render(st: State) -> Layout:
    now = time.time()
    with st.lock:
        cur = CCY_SIGN.get(st.base, st.base)
        unreal = sum(p["upnl_base"] for p in st.positions)
        equity = st.start + st.realized + unreal
        pct = (equity / st.start - 1) * 100 if st.start else 0

        # header
        blink = "●" if int(now * 2) % 2 else "○"
        hdr = Text.assemble(
            ("SENTINEL-6 ", "bold #ffffff"), (f"{blink} LIVE ", "bold #ff5b5b"),
            (datetime.now().strftime("%d %b %H:%M:%S  "), "#8d96c4"),
            ("│ ", "#26305c"),
            ("CYCLE RUNNING " if st.busy else f"cycle #{st.cycles} · next in {_mmss(st.next_cycle - now)} ", "#ffd23d"),
            ("│ ", "#26305c"), (f"quotes in {_mmss(st.next_tick - now)} ", "#8d96c4"), ("│ ", "#26305c"),
            ("Equity ", "#8d96c4"), (f"{cur}{equity:,.0f} ", "bold #ffffff"),
            (f"({pct:+.2f}%) ", "#4dff8a" if pct >= 0 else "#ff5b5b"),
            ("│ DEMO DATA" if st.demo else "│ PAPER ONLY", "#ff9a1f"))

        # agents strip
        strip = Table.grid(expand=True)
        for _ in AGENTS:
            strip.add_column(ratio=1)
        cells = []
        for n, c, role in AGENTS:
            a = st.agents[n]
            if a["state"] == "work":
                body = Text(f"{SPIN[int(now * 10) % 10]} {a['msg']}", style=c)
            elif a["state"] in ("done", "decision"):
                body = Text(f"✓ {a['msg']}", style="#e8ecff")
            elif a["state"] == "error":
                body = Text(f"✗ {a['msg']}", style="#ff5b5b")
            else:
                body = Text(f"· {a['msg']}", style="#5a6390")
            body.truncate(60, overflow="ellipsis")
            active = a["state"] == "work"
            cells.append(Panel(body, title=f"[bold {c}]{n}[/] [#8d96c4]{role}", title_align="left",
                               border_style=c if active else "#26305c", height=4))
        strip.add_row(*cells)

        # watchlist
        wt = Table(expand=True, box=None, header_style="bold #8d96c4", pad_edge=False)
        for col, j in (("#", "right"), ("Seg", "left"), ("Symbol", "left"), ("Live", "right"), ("", "left"),
                       ("vs scan", "right"), ("Dir", "left"), ("Score", "right")):
            wt.add_column(col, justify=j, no_wrap=True)
        for i, r in enumerate(st.scan[:14], 1):
            q = st.quotes.get(r["key"], {"px": r["price"], "prev": r["price"], "ref": r["price"], "t": 0})
            up = q["px"] > q["prev"]
            dn = q["px"] < q["prev"]
            fresh = now - q.get("t", 0) < 1.5
            arrow = ("▲", "#4dff8a") if up else ("▼", "#ff5b5b") if dn else ("•", "#5a6390")
            chg = q["px"] / q["ref"] - 1 if q["ref"] else 0
            px_style = ("bold black on #4dff8a" if up else "bold white on #c0392b") if fresh and (up or dn) else "#e8ecff"
            wt.add_row(str(i), Text(SEG_LABEL[r["segment"]], style=SEG_COLOR[r["segment"]]), f"[bold]{r['symbol']}",
                       Text(fp(q["px"]), style=px_style),
                       Text(arrow[0], style=arrow[1]), Text(f"{chg:+.2%}", style="#4dff8a" if chg >= 0 else "#ff5b5b"),
                       Text(r["direction"], style="#4dff8a" if r["direction"] == "LONG" else "#ff5b5b"),
                       f"[bold #4fb3ff]{r['score']}")
        watch = Panel(wt if st.scan else Text("Scanning…", style="#5a6390"),
                      title="[bold #4fb3ff]KESTREL · RANKED WATCHLIST", border_style="#26305c")

        # decisions
        dt = Table(expand=True, box=None, header_style="bold #8d96c4", pad_edge=False)
        for col in ("Seg", "Symbol", "Dir", "P/H/Pu", "Structure", "AEGIS", "ARBITER", "Why"):
            dt.add_column(col, no_wrap=col not in ("Why", "Structure"), overflow="ellipsis")
        for res in st.results:
            r = res["reports"]
            a = r["arbiter"]
            sc = lambda k: Text(f"{r[k]['score']:+.2f}", style="#4dff8a" if r[k]["score"] > 0 else "#ff5b5b" if r[k]["score"] < 0 else "#8d96c4")  # noqa
            stc = res.get("structure")
            phu = Text.assemble(sc("prism"), " ", sc("herald"), " ", sc("pulse"))
            units = r["aegis"]["details"].get("units") or 0
            dt.add_row(Text(SEG_LABEL[res["segment"]], style=SEG_COLOR[res["segment"]]), f"[bold]{res['symbol']}",
                       res["direction"], phu,
                       Text(f"{stc['name']} {stc['label']}" if stc else "none", style="#e8ecff" if stc else "#ff5b5b"),
                       Text(("PASS" if r["aegis"]["verdict"] == "APPROVED" else "VETO") + (f" ×{units:,.4g}" if units else ""),
                            style="#4dff8a" if r["aegis"]["verdict"] == "APPROVED" else "#ff5b5b"),
                       Text(a["verdict"], style=DEC.get(a["verdict"], "")), Text(a["details"]["reason"], style="#8d96c4"))
        decisions = Panel(dt if st.results else Text("Waiting for first cycle…", style="#5a6390"),
                          title="[bold #ffd23d]ARBITER · DECISIONS", border_style="#26305c")

        # book
        bt = Table(expand=True, box=None, header_style="bold #8d96c4", pad_edge=False)
        for col, j in (("Seg", "left"), ("Position", "left"), ("×", "right"), ("Und", "right"),
                       ("to Stop", "right"), ("to Tgt", "right"), (f"uPnL {cur}", "right")):
            bt.add_column(col, justify=j, no_wrap=col != "Position", overflow="ellipsis")
        for p in st.positions:
            ds = abs(p["last"] - p["und_stop"]) / p["last"]
            dtg = abs(p["und_target"] - p["last"]) / p["last"]
            bt.add_row(Text(SEG_LABEL[p["segment"]], style=SEG_COLOR[p["segment"]]),
                       Text(f"{p['name']} {p['label']}", style="#4dff8a" if p["direction"] == "LONG" else "#ff5b5b"),
                       f"{p['units']:,.4g}", fp(p["last"]), Text(f"{ds:.1%}", style="#ff9a1f" if ds < 0.02 else "#8d96c4"),
                       f"{dtg:.1%}", Text(f"{p['upnl_base']:+,.0f}", style="bold #4dff8a" if p["upnl_base"] >= 0 else "bold #ff5b5b"))
        book = Panel(Group(Text.assemble(("Realized ", "#8d96c4"), (f"{cur}{st.realized:+,.0f}   ", "bold"),
                                         ("Open ", "#8d96c4"), (f"{cur}{unreal:+,.0f}", "bold #4dff8a" if unreal >= 0 else "bold #ff5b5b")),
                           bt if st.positions else Text("No open positions", style="#5a6390")),
                     title="[bold #4dff8a]PAPER BOOK", border_style="#26305c")

        # log
        lines = Text()
        for ts, ag, state, msg in list(st.log)[-16:]:
            lines.append(f"{ts} ", "#5a6390")
            lines.append(f"{ag:<8} ", f"bold {ACOL.get(ag, '#ffffff')}")
            style = "#ff5b5b" if state == "error" else "bold #4dff8a" if state == "trade" else "#e8ecff"
            if state == "decision":
                for k, v in DEC.items():
                    if f"→ {k}" in msg:
                        style = v
            lines.append(msg[:110] + "\n", style)
        events = Panel(lines, title="[bold #8d96c4]EVENT LOG", border_style="#26305c")

    lay = Layout()
    lay.split_column(Layout(Panel(hdr, border_style="#26305c"), size=3), Layout(strip, size=4),
                     Layout(name="body"), Layout(Text("Ctrl+C to quit · research tool · paper trading only",
                                                      style="#5a6390", justify="center"), size=1))
    lay["body"].split_row(Layout(name="left", ratio=3), Layout(name="right", ratio=2))
    lay["left"].split_column(Layout(watch, ratio=3), Layout(decisions, ratio=2))
    lay["right"].split_column(Layout(book, ratio=2), Layout(events, ratio=3))
    return lay


def run_live(cfg, root, demo=False, execute=True, cycle_min=15, tick_sec=30, pace=0.2, screen=True):
    st = State()
    eng = Engine(cfg, root, st, demo, execute, cycle_min, tick_sec, pace)
    eng.start()
    try:
        with Live(render(st), refresh_per_second=8, screen=screen) as live:
            while True:
                live.update(render(st))
                time.sleep(0.125)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop_flag.set()
