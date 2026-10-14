"""KESTREL — scans every segment and returns one ranked watchlist (top N per segment)."""
from __future__ import annotations

import math

import numpy as np

from core.indicators import ema, atr, volume_ratio
from .base import Agent, clip


class Kestrel(Agent):
    name, role, color = "KESTREL", "Market scanner", "#4fb3ff"

    def scan_one(self, u, df):
        c = df["Close"]
        rets = c.pct_change().dropna()
        sigma = float(rets.tail(60).std()) or 1e-9
        ret20 = float(c.iloc[-1] / c.iloc[-21] - 1)
        mom = ret20 / (sigma * math.sqrt(20))                       # vol-normalised momentum (z)
        e50, e200 = ema(c, 50).iloc[-1], ema(c, 200).iloc[-1]
        px = c.iloc[-1]
        trend = 1 if (px > e50 > e200) else -1 if (px < e50 < e200) else 0
        hi20, lo20 = df["High"].tail(20).max(), df["Low"].tail(20).min()
        prox = (px - lo20) / max(hi20 - lo20, 1e-9) * 2 - 1        # -1 at 20d low, +1 at 20d high
        vr = volume_ratio(df)
        d = np.sign(mom) or 1
        raw = (0.40 * math.tanh(mom / 2) + 0.25 * trend + 0.15 * prox
               + 0.20 * math.tanh(vr - 1) * d)
        return {
            "key": u.key, "symbol": u.symbol, "segment": u.segment, "seg": u.label, "ccy": u.ccy,
            "price": float(px), "chg_1d": float(c.iloc[-1] / c.iloc[-2] - 1), "ret_20d": ret20,
            "momentum_z": round(mom, 2), "trend": trend, "vol_ratio": round(vr, 2),
            "atr_pct": float(atr(df).iloc[-1]) / px, "direction": "LONG" if raw >= 0 else "SHORT",
            "raw": raw, "score": int(round(min(1.0, abs(raw)) * 100)),
        }

    def run(self, data: dict, umap: dict) -> tuple[list[dict], list]:
        rows = []
        for k, df in data.items():
            try:
                rows.append(self.scan_one(umap[k], df))
            except Exception:  # noqa
                continue
        segs = self.cfg["segments"]
        # spot segments without shorting (cash market) only take LONG ideas
        rows = [r for r in rows if not (r["segment"] in ("us_spot", "in_spot", "crypto_spot")
                                       and r["direction"] == "SHORT" and not segs[r["segment"]].get("allow_short", False))]
        rows.sort(key=lambda r: r["score"], reverse=True)
        picks, count = [], {}
        for r in rows:
            n = segs[r["segment"]].get("top_n", 2)
            if r["score"] >= self.cfg["kestrel"]["min_score"] and count.get(r["segment"], 0) < n:
                count[r["segment"]] = count.get(r["segment"], 0) + 1
                picks.append(r)
        reports = [self.report(r["key"], clip(r["raw"]), f"#{i + 1} · {r['score']}",
                               "HIGH" if r["score"] >= 70 else "MODERATE", details=r)
                   for i, r in enumerate(picks)]
        return rows, reports
