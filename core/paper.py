"""SQLite paper-trading ledger (multi-leg). No real orders are ever sent anywhere.

Every position = a structure (spot / future / option / spread) on one underlying.
Value per underlying unit = Σ side × leg price; P&L = Δvalue × units × multiplier.
Option legs are marked with Black-Scholes at the live underlying price, anchored to
the market premium paid at entry. Exits: underlying hits stop/target, or expiry.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .data import convert
from .derivs import structure_value, first_expiry
from .sources import now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, segment TEXT, symbol TEXT, direction TEXT,
  structure TEXT, name TEXT, label TEXT, legs TEXT, units REAL, mult REAL, ccy TEXT,
  und_entry REAL, und_stop REAL, und_target REAL, entry_value REAL, max_loss_base REAL,
  rate REAL DEFAULT 0, opened_at TEXT, status TEXT DEFAULT 'OPEN',
  und_exit REAL, exit_value REAL, closed_at TEXT, pnl_base REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, key TEXT, direction TEXT, structure TEXT,
  decision TEXT, composite REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class PaperBook:
    def __init__(self, path: Path, starting_equity: float, base: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT v FROM meta WHERE k='start'").fetchone():
            self.db.executemany("INSERT INTO meta VALUES(?, ?)", [("start", str(starting_equity)), ("base", base)])
            self.db.commit()
        self.start = float(self.db.execute("SELECT v FROM meta WHERE k='start'").fetchone()[0])
        self.base = base

    # ------------------------------------------------------------------ queries
    def open_positions(self):
        rows = [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE status='OPEN'")]
        for r in rows:
            r["legs"] = json.loads(r["legs"])
        return rows

    def closed_positions(self, n=20):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (n,))]

    def realized(self):
        return float(self.db.execute("SELECT COALESCE(SUM(pnl_base),0) FROM positions WHERE status='CLOSED'").fetchone()[0])

    # ------------------------------------------------------------------ valuation
    def pnl(self, p, und_px, rates, now=None):
        v = structure_value(p["legs"], und_px, now, p.get("rate", 0) or 0)
        return convert((v - p["entry_value"]) * p["units"] * p["mult"], p["ccy"], self.base, rates), v

    def notional(self, p, und_px, rates):
        t = p["structure"]
        if t in ("SPOT", "FUTURE"):
            per = und_px
        elif t == "CREDIT_SPREAD":
            return p["max_loss_base"]                      # margin ≈ max loss
        else:
            per = abs(structure_value(p["legs"], und_px, None, p.get("rate", 0) or 0))
        return convert(per * p["units"] * p["mult"], p["ccy"], self.base, rates)

    def snapshot(self, prices: dict, rates):
        """prices: key -> latest underlying price."""
        opens, unreal = [], 0.0
        for p in self.open_positions():
            px = prices.get(p["key"], p["und_entry"])
            u, _ = self.pnl(p, px, rates)
            unreal += u
            opens.append({**p, "last": px, "upnl_base": u, "notional_base": self.notional(p, px, rates)})
        realized = self.realized()
        return {"start": self.start, "base": self.base, "realized": realized, "unrealized": unreal,
                "equity": self.start + realized + unreal, "positions": opens}

    # ------------------------------------------------------------------ lifecycle
    def check_exits(self, prices: dict, rates, highs: dict | None = None, lows: dict | None = None):
        """Close positions whose underlying touched stop/target (bar high/low if given) or expired."""
        closed = []
        now = now_utc()
        for p in self.open_positions():
            px = prices.get(p["key"])
            if px is None:
                continue
            hi = (highs or {}).get(p["key"], px)
            lo = (lows or {}).get(p["key"], px)
            long_ = p["direction"] == "LONG"
            hit = None
            if (lo <= p["und_stop"]) if long_ else (hi >= p["und_stop"]):
                hit = (p["und_stop"], "STOP")
            elif (hi >= p["und_target"]) if long_ else (lo <= p["und_target"]):
                hit = (p["und_target"], "TARGET")
            exp = first_expiry(p["legs"])
            if not hit and exp and exp <= now:
                hit = (px, "EXPIRY")
            if hit:
                pnl = self.close(p, hit[0], hit[1], rates)
                closed.append((p, hit[1], pnl))
        return closed

    def close(self, p, und_px, reason, rates) -> float:
        pnl, v = self.pnl(p, und_px, rates)
        self.db.execute("UPDATE positions SET status='CLOSED', und_exit=?, exit_value=?, closed_at=?, pnl_base=?, "
                        "exit_reason=? WHERE id=?",
                        (und_px, v, datetime.now().isoformat(timespec="seconds"), pnl, reason, p["id"]))
        self.db.commit()
        return pnl

    def open(self, u, direction, st, units, max_loss_base, rate=0.0):
        self.db.execute(
            "INSERT INTO positions(key,segment,symbol,direction,structure,name,label,legs,units,mult,ccy,"
            "und_entry,und_stop,und_target,entry_value,max_loss_base,rate,opened_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (u.key, u.segment, u.symbol, direction, st["type"], st["name"], st["label"], json.dumps(st["legs"]),
             units, st["mult"], st["ccy"], st["und_entry"], st["und_stop"], st["und_target"], st["entry_value"],
             max_loss_base, rate, datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def log_decision(self, key, direction, structure, decision, composite, reason):
        self.db.execute("INSERT INTO decisions(ts,key,direction,structure,decision,composite,reason) VALUES(?,?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), key, direction, structure, decision, composite, reason))
        self.db.commit()
