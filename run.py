"""SENTINEL-6 — six-agent market desk (paper trading only).

  python run.py                 # one live cycle
  python run.py --demo          # offline synthetic data (no internet needed)
  python run.py --loop 60       # repeat every 60 minutes
  python run.py --no-exec       # analyse only, don't open paper positions
  python run.py --live          # live terminal dashboard (cycle every 15 min, quotes every 30 s)
  python run.py --live --demo   # live terminal on offline demo data
  python run.py --book          # show paper book
  python run.py --reset         # wipe paper book
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.desk import Desk          # noqa: E402
from core.view import console_report, html_report  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="SENTINEL-6 multi-agent desk (paper only)")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--demo", action="store_true", help="synthetic offline data")
    ap.add_argument("--loop", type=float, default=0, help="minutes between cycles (0 = run once)")
    ap.add_argument("--no-exec", action="store_true", help="don't open paper positions")
    ap.add_argument("--open", action="store_true", help="open HTML report in browser")
    ap.add_argument("--live", action="store_true", help="full-screen live terminal")
    ap.add_argument("--cycle", type=float, default=None, help="live: minutes between full cycles (default 15, demo 2)")
    ap.add_argument("--tick", type=float, default=None, help="live: seconds between quote refreshes (default 30, demo 2)")
    ap.add_argument("--no-screen", action="store_true", help="live: don't use alternate screen")
    ap.add_argument("--book", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    con = Console()

    if a.reset:
        db = ROOT / "data" / ("paper_demo.db" if a.demo else "paper.db")
        db.unlink(missing_ok=True)
        con.print(f"[yellow]Paper book reset: {db.name}")
        return

    if a.live:
        from core.live import run_live
        run_live(cfg, ROOT, demo=a.demo, execute=not a.no_exec,
                 cycle_min=a.cycle or (2 if a.demo else 15),
                 tick_sec=a.tick or (2 if a.demo else 30),
                 pace=0.35 if a.demo else 0.15, screen=not a.no_screen)
        return

    desk = Desk(cfg, ROOT, demo=a.demo)

    if a.book:
        from core.data import fx_rates
        from rich.table import Table
        snap = desk.book.snapshot({}, fx_rates({}))
        t = Table(title=f"Paper book · realized P&L ${snap['realized']:+,.0f}")
        for c in ("Symbol", "Side", "Qty", "Entry", "Stop", "Target", "Opened"):
            t.add_column(c)
        for p in snap["positions"]:
            t.add_row(p["symbol"], p["side"], f"{p['qty']:,.4f}", f"{p['entry']:,.4f}",
                      f"{p['stop']:,.4f}", f"{p['target']:,.4f}", p["opened_at"])
        con.print(t)
        for p in desk.book.closed_positions():
            con.print(f"closed {p['symbol']} {p['side']} {p['exit_reason']} P&L ${p['pnl_usd']:+,.0f}")
        return

    while True:
        try:
            run = desk.cycle(execute=not a.no_exec)
            console_report(run, con)
            out = ROOT / "reports" / f"run_{datetime.now():%Y%m%d_%H%M%S}.html"
            html_report(run, out)
            html_report(run, ROOT / "reports" / "latest.html")
            con.print(f"[dim]Report → {out}")
            if a.open:
                webbrowser.open(out.as_uri())
        except Exception as e:  # keep the loop alive
            con.print(f"[red]Cycle failed: {e}")
            if a.verbose:
                con.print_exception()
        if not a.loop:
            break
        con.print(f"[dim]Sleeping {a.loop} min… (Ctrl+C to stop)")
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
