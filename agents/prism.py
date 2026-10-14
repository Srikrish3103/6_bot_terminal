"""PRISM — finds the chart setup: trend, volume, support, resistance, entry/stop/target."""
from __future__ import annotations

import math

from core.indicators import ema, rsi, atr, macd, kst
from .base import Agent, clip


class Prism(Agent):
    name, role, color = "PRISM", "Chart technician", "#b36bff"

    def analyse(self, sym, df, direction: str):
        pc = self.cfg["prism"]
        d = 1 if direction == "LONG" else -1
        c = df["Close"]
        px = float(c.iloc[-1])
        e20, e50, e200 = (float(ema(c, n).iloc[-1]) for n in (20, 50, 200))
        r = float(rsi(c).iloc[-1])
        _, _, hist = macd(c)
        h_now, h_prev = float(hist.iloc[-1]), float(hist.iloc[-3])
        k_line, k_sig = kst(c)
        k_now, s_now = float(k_line.iloc[-1]), float(k_sig.iloc[-1])
        k_prev, s_prev = float(k_line.iloc[-2]), float(k_sig.iloc[-2])
        a = float(atr(df).iloc[-1])
        lb = pc["sr_lookback"]
        support = float(df["Low"].tail(lb).min())
        resistance = float(df["High"].tail(lb).max())

        # --- components, each in -1..+1 from the trade direction's point of view
        stack = (1 if e20 > e50 > e200 else -1 if e20 < e50 < e200 else 0) * d
        above = (1 if px > e20 else -1) * d
        mom = (1 if h_now > 0 else -1) * d * (1 if (h_now - h_prev) * d > 0 else 0.5)
        # KST: above its signal (and rising vs zero) = momentum confirms the trade direction
        kst_ok = not (math.isnan(k_now) or math.isnan(s_now))
        if kst_ok:
            gap = (k_now - s_now) * d
            cross = (k_prev - s_prev) * d <= 0 < gap
            k_s = clip((1 if gap > 0 else -1) * (1.0 if (k_now * d > 0) == (gap > 0) else 0.6))
            k_state = ("bullish" if k_now > s_now else "bearish") + (" crossover" if cross else "")
        else:
            gap, cross, k_s, k_state = 0.0, False, 0.0, "n/a (<53 bars)"
        if d == 1:
            rsi_s = 1 if 50 <= r <= 68 else 0.3 if 45 <= r < 50 else -0.5 if r > 75 else -1 if r < 40 else 0
        else:
            rsi_s = 1 if 32 <= r <= 50 else 0.3 if 50 < r <= 55 else -0.5 if r < 25 else -1 if r > 60 else 0
        v = df["Volume"].astype(float)
        if v.tail(50).sum() > 0:
            up = (c.diff() * d > 0).tail(10)
            vol_up = v.tail(10)[up].mean() if up.any() else 0
            vol_dn = v.tail(10)[~up].mean() if (~up).any() else 1e-9
            vol_s = clip((vol_up / max(vol_dn, 1e-9)) - 1)
            vol_txt = "CONFIRMING" if vol_s > 0.1 else "DIVERGING" if vol_s < -0.1 else "FLAT"
        else:
            vol_s, vol_txt = 0.0, "N/A (FX)"

        w = pc.get("weights", {"trend": 0.28, "price_vs_ema20": 0.12, "macd": 0.15, "kst": 0.15, "rsi": 0.15, "volume": 0.15})
        score = clip(w["trend"] * stack + w["price_vs_ema20"] * above + w["macd"] * mom + w["kst"] * k_s
                     + w["rsi"] * rsi_s + w["volume"] * vol_s)

        # --- trade plan
        if d == 1:
            stop = max(support - 0.25 * a, px - 3 * a)
            stop = min(stop, px - pc["atr_stop_mult"] * a * 0.67)
        else:
            stop = min(resistance + 0.25 * a, px + 3 * a)
            stop = max(stop, px + pc["atr_stop_mult"] * a * 0.67)
        risk = abs(px - stop)
        target = px + d * pc["target_rr"] * risk
        breakout = (px >= resistance * 0.995) if d == 1 else (px <= support * 1.005)

        status = "SETUP" if score >= 0.5 else "WATCH" if score >= 0.2 else "NO SETUP"
        notes = []
        if kst_ok:
            side = "above" if k_now > s_now else "below"
            good = gap > 0
            msg = (f"KST {k_now:+.1f} {side} signal {s_now:+.1f}"
                   + (" — fresh crossover" if cross else "")
                   + (f", {'above' if k_now > 0 else 'below'} zero")
                   + (" → momentum confirms, KST supports this pick" if good else " → KST against this direction"))
            notes.insert(0, msg)
        if breakout:
            notes.append("Price at 20-bar extreme — breakout attempt")
        if (d == 1 and r > 75) or (d == -1 and r < 25):
            notes.append(f"RSI {r:.0f} stretched — chasing risk")
        details = {
            "direction": direction, "price": px, "trend": "UP" if stack * d > 0 else "DOWN" if stack * d < 0 else "MIXED",
            "volume": vol_txt, "rsi": round(r, 1), "ema20": e20, "ema50": e50, "ema200": e200,
            "support": support, "resistance": resistance, "atr": a,
            "entry": px, "stop": stop, "target": target, "rr": pc["target_rr"], "breakout": breakout,
            "kst": round(k_now, 2) if kst_ok else None, "kst_signal": round(s_now, 2) if kst_ok else None,
            "kst_state": k_state, "kst_confirms": bool(kst_ok and gap > 0), "kst_cross": bool(cross),
            "macd_hist": round(h_now, 4),
        }
        return self.report(sym, score * d, status, "HIGH" if abs(score) > 0.65 else "MODERATE", details, notes)
