"""AEGIS — risk officer. Sizes every structure in whole lots / units and can veto any trade."""
from __future__ import annotations

import math

import pandas as pd

from core.data import convert, CCY_SIGN
from .base import Agent


class Aegis(Agent):
    name, role, color = "AEGIS", "Risk officer", "#ff9a1f"

    def review(self, u, st: dict, book: dict, data: dict, rates: dict, base: str):
        ac = self.cfg["aegis"]
        cur = CCY_SIGN.get(base, base + " ")
        eq = book["equity"]
        key = u.key

        def b(x):          # instrument ccy -> base ccy
            return convert(x, st["ccy"], base, rates)

        loss_unit = b(st["max_loss"] * st["mult"])          # per lot / per share, in base
        notional_unit = b(st["notional"] * st["mult"])
        max_loss = eq * ac["risk_per_trade_pct"] / 100
        cap = eq * ac["max_position_pct"] / 100

        units = max_loss / max(loss_unit, 1e-12)
        if notional_unit * units > cap:
            units = cap / max(notional_unit, 1e-12)
        if st.get("integer"):
            units = math.floor(units + 1e-9)
        notional = units * notional_unit
        loss_at_stop = units * loss_unit

        opens = book["positions"]
        gross = sum(p["notional_base"] for p in opens)
        seg_exp = sum(p["notional_base"] for p in opens if p["segment"] == u.segment)
        seg_cap = eq * ac.get("max_segment_pct", {}).get(u.segment, 100) / 100
        holding = any(p["key"] == key for p in opens)

        d = 1 if st["direction"] == "LONG" else -1
        corr_max, corr_with = 0.0, None
        rets = data[key]["Close"].pct_change().tail(90)
        for p in opens:
            if p["key"] in data and p["key"] != key:
                other = data[p["key"]]["Close"].pct_change().tail(90)
                j = pd.concat([rets.reset_index(drop=True), other.reset_index(drop=True)], axis=1).dropna()
                if len(j) > 30:
                    c = float(j.corr().iloc[0, 1]) * d * (1 if p["direction"] == "LONG" else -1)
                    if c > corr_max:
                        corr_max, corr_with = c, p["symbol"]

        min_rr = ac["min_rr"].get(st["type"], 2.0)
        rr_ok = st["rr"] >= min_rr
        rr_txt = f"{st['rr']:.2f}R (min {min_rr})"
        if st["type"] == "CREDIT_SPREAD":
            pop_min = self.cfg["options"]["min_pop_credit"]
            ev = st.get("ev", 0.0)
            rr_ok = rr_ok and (st.get("pop") or 0) >= pop_min and ev > 0
            rr_txt += (f" · POP {100 * (st.get('pop') or 0):.0f}% (min {pop_min * 100:.0f}%) · "
                       f"EV {ev / max(st['max_loss'], 1e-9):+.0%} of risk")

        unit_word = "lot" if st["type"] != "SPOT" else "unit"
        size_ok = units > 0
        size_txt = (f"{units:,.0f} {unit_word}{'s' if units != 1 else ''}" if st.get("integer") else f"{units:,.4f} units") \
            + f" · {cur}{notional:,.0f} (cap {cur}{cap:,.0f})"
        if not size_ok:
            size_txt = f"1 {unit_word} risks {cur}{loss_unit:,.0f} > budget {cur}{max_loss:,.0f}"

        checks = {
            "Position size": (size_ok, size_txt),
            "Maximum loss": (size_ok and loss_at_stop <= max_loss * 1.0001,
                             f"{cur}{loss_at_stop:,.0f} at stop (limit {cur}{max_loss:,.0f})"),
            "Correlation": (corr_max <= ac["max_correlation"],
                            f"{corr_max:.2f} vs {corr_with}" if corr_with else "no correlated holdings"),
            "Portfolio exposure": (
                gross + notional <= eq * ac["max_gross_pct"] / 100 and seg_exp + notional <= seg_cap
                and len(opens) < ac["max_positions"] and not holding,
                f"gross {100 * (gross + notional) / eq:.0f}% · {u.label} {100 * (seg_exp + notional) / eq:.0f}%"
                f"/{100 * seg_cap / eq:.0f}% · {len(opens)}/{ac['max_positions']} open"
                + (" · already holding" if holding else "")),
            "Risk / reward": (rr_ok, rr_txt),
        }
        ok = all(v[0] for v in checks.values())
        fails = [k for k, v in checks.items() if not v[0]]
        notes = ["No trade is also a decision."] if not ok else []
        return self.report(key, 1 if ok else -1, "APPROVED" if ok else "REJECTED", "HIGH",
                           {"checks": {k: {"pass": v[0], "detail": v[1]} for k, v in checks.items()},
                            "units": units, "notional_base": notional, "loss_at_stop_base": loss_at_stop,
                            "rr": st["rr"], "fails": fails, "already_holding": holding}, notes)
