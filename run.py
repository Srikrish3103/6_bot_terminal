"""SENTINEL-6 — six-agent market desk (paper trading only).

  python run.py                 # one live cycle
  python run.py --demo          # offline synthetic data (no internet needed)
  python run.py --loop 60       # repeat every 60 minutes
  python run.py --no-exec       # analyse only, don't open paper positions
  python run.py --live          # live terminal dashboard (cycle every 15 min, quotes every 30 s)
  python run.py --live --demo   # live terminal on offline demo data
  python run.py --check         # test every live data source (NSE, Delta, yfinance, news)
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
    ap.add_argument("--check", action="store_true", help="test live data sources and exit")
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
        db = ROOT / "data" / ("paper_demo_v2.db" if a.demo else "paper_v2.db")
        db.unlink(missing_ok=True)
        con.print(f"[yellow]Paper book reset: {db.name}")
        return

    if a.check:
        from core.check import run_check
        run_check(cfg, con)
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
        from rich.table import Table
        from core.data import CCY_SIGN
        cur = CCY_SIGN.get(desk.base, desk.base)
        t = Table(title=f"Paper book · realized P&L {cur}{desk.book.realized():+,.0f}")
        for c in ("Seg", "Position", "Units", "Und entry", "Stop", "Target", f"Max loss {cur}", "Opened"):
            t.add_column(c)
        for p in desk.book.open_positions():
            t.add_row(p["segment"], f"{p['name']} {p['label']}", f"{p['units']:,.4g}", f"{p['und_entry']:,.4f}",
                      f"{p['und_stop']:,.4f}", f"{p['und_target']:,.4f}", f"{p['max_loss_base']:,.0f}", p["opened_at"])
        con.print(t)
        for p in desk.book.closed_positions():
            con.print(f"closed {p['name']} {p['label']} · {p['exit_reason']} · P&L {cur}{p['pnl_base']:+,.0f}")
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
