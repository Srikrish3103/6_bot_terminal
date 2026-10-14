"""SQLite paper-trading ledger. No real orders are ever sent anywhere."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .data import asset_class, to_usd

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL,
  entry REAL, stop REAL, target REAL, asset_class TEXT, opened_at TEXT,
  status TEXT DEFAULT 'OPEN', exit REAL, closed_at TEXT, pnl_usd REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, direction TEXT,
  decision TEXT, composite REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class PaperBook:
    def __init__(self, path: Path, starting_equity: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT v FROM meta WHERE k='start'").fetchone():
            self.db.execute("INSERT INTO meta VALUES('start', ?)", (str(starting_equity),))
            self.db.commit()
        self.start = float(self.db.execute("SELECT v FROM meta WHERE k='start'").fetchone()[0])

    def open_positions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM positions WHERE status='OPEN'")]

    def closed_positions(self, n=20):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE status='CLOSED' ORDER BY id DESC LIMIT ?", (n,))]

    def realized(self):
        return float(self.db.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM positions WHERE status='CLOSED'").fetchone()[0])

    @staticmethod
    def _pnl(p, px, rates):
        d = 1 if p["side"] == "LONG" else -1
        return to_usd((px - p["entry"]) * d * p["qty"], p["symbol"], rates)

    def mark(self, data, rates):
        """Close positions whose stop/target was touched on the latest bar; return book snapshot."""
        now = datetime.now().isoformat(timespec="seconds")
        for p in self.open_positions():
            df = data.get(p["symbol"])
            if df is None:
                continue
            bar = df.iloc[-1]
            hit = None
            if p["side"] == "LONG":
                if bar["Low"] <= p["stop"]:
                    hit = (p["stop"], "STOP")
                elif bar["High"] >= p["target"]:
                    hit = (p["target"], "TARGET")
            else:
                if bar["High"] >= p["stop"]:
                    hit = (p["stop"], "STOP")
                elif bar["Low"] <= p["target"]:
                    hit = (p["target"], "TARGET")
            if hit:
                self.db.execute("UPDATE positions SET status='CLOSED', exit=?, closed_at=?, pnl_usd=?, exit_reason=? WHERE id=?",
                                (hit[0], now, self._pnl(p, hit[0], rates), hit[1], p["id"]))
        self.db.commit()
        return self.snapshot(data, rates)

    def snapshot(self, data, rates):
        opens = []
        unreal = 0.0
        for p in self.open_positions():
            df = data.get(p["symbol"])
            px = float(df["Close"].iloc[-1]) if df is not None else p["entry"]
            u = self._pnl(p, px, rates)
            unreal += u
            opens.append({**p, "last": px, "upnl_usd": u,
                          "notional_usd": to_usd(px * p["qty"], p["symbol"], rates)})
        realized = self.realized()
        return {"start": self.start, "realized": realized, "unrealized": unreal,
                "equity": self.start + realized + unreal, "positions": opens}

    def close(self, p: dict, price: float, reason: str, rates) -> float:
        pnl = self._pnl(p, price, rates)
        self.db.execute("UPDATE positions SET status='CLOSED', exit=?, closed_at=?, pnl_usd=?, exit_reason=? WHERE id=?",
                        (price, datetime.now().isoformat(timespec="seconds"), pnl, reason, p["id"]))
        self.db.commit()
        return pnl

    def open(self, sym, side, qty, entry, stop, target):
        self.db.execute("INSERT INTO positions(symbol,side,qty,entry,stop,target,asset_class,opened_at) VALUES(?,?,?,?,?,?,?,?)",
                        (sym, side, qty, entry, stop, target, asset_class(sym), datetime.now().isoformat(timespec="seconds")))
        self.db.commit()

    def log_decision(self, sym, direction, decision, composite, reason):
        self.db.execute("INSERT INTO decisions(ts,symbol,direction,decision,composite,reason) VALUES(?,?,?,?,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), sym, direction, decision, composite, reason))
        self.db.commit()
