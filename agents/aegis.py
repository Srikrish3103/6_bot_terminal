"""AEGIS — risk officer. Can reject any trade (position size, exposure, max loss)."""
from __future__ import annotations

import pandas as pd

from core.data import asset_class, to_usd
from .base import Agent


class Aegis(Agent):
    name, role, color = "AEGIS", "Risk officer", "#ff9a1f"

    def review(self, sym, plan: dict, book: dict, data: dict, rates: dict):
        ac = self.cfg["aegis"]
        eq = book["equity"]
        d = 1 if plan["direction"] == "LONG" else -1
        entry, stop, target = plan["entry"], plan["stop"], plan["target"]
        risk_unit = to_usd(abs(entry - stop), sym, rates)          # USD risk per unit
        px_usd = to_usd(entry, sym, rates)

        max_loss = eq * ac["risk_per_trade_pct"] / 100
        qty = max_loss / max(risk_unit, 1e-12)
        cap = eq * ac["max_position_pct"] / 100
        if qty * px_usd > cap:
            qty = cap / px_usd
        notional = qty * px_usd
        loss_at_stop = qty * risk_unit
        rr = abs(target - entry) / max(abs(entry - stop), 1e-12)

        opens = book["positions"]
        gross = sum(p["notional_usd"] for p in opens)
        cls = asset_class(sym)
        cls_exp = sum(p["notional_usd"] for p in opens if p["asset_class"] == cls)

        corr_max, corr_with = 0.0, None
        rets = data[sym]["Close"].pct_change().tail(90)
        for p in opens:
            if p["symbol"] in data and p["symbol"] != sym:
                other = data[p["symbol"]]["Close"].pct_change().tail(90)
                j = pd.concat([rets, other], axis=1, join="inner").dropna()
                if len(j) > 30:
                    c = float(j.corr().iloc[0, 1]) * d * (1 if p["side"] == "LONG" else -1)
                    if c > corr_max:
                        corr_max, corr_with = c, p["symbol"]

        checks = {
            "Position size": (qty > 0 and notional <= cap * 1.0001,
                              f"{qty:,.4f} units ≈ ${notional:,.0f} (cap ${cap:,.0f})"),
            "Maximum loss": (loss_at_stop <= max_loss * 1.0001,
                             f"${loss_at_stop:,.0f} at stop (limit ${max_loss:,.0f})"),
            "Correlation": (corr_max <= ac["max_correlation"],
                            f"{corr_max:.2f} vs {corr_with}" if corr_with else "no correlated holdings"),
            "Portfolio exposure": (
                gross + notional <= eq * ac["max_gross_pct"] / 100
                and cls_exp + notional <= eq * ac["max_class_pct"] / 100
                and len(opens) < ac["max_positions"]
                and not any(p["symbol"] == sym for p in opens),
                f"gross {100 * (gross + notional) / eq:.0f}% · {cls} {100 * (cls_exp + notional) / eq:.0f}% · "
                f"{len(opens)}/{ac['max_positions']} open"
                + (" · already holding" if any(p["symbol"] == sym for p in opens) else "")),
            "Risk / reward": (rr >= ac["min_rr"], f"{rr:.2f}R (min {ac['min_rr']})"),
        }
        ok = all(v[0] for v in checks.values())
        fails = [k for k, v in checks.items() if not v[0]]
        notes = ["No trade is also a decision."] if not ok else []
        return self.report(sym, 1 if ok else -1, "APPROVED" if ok else "REJECTED", "HIGH",
                           {"checks": {k: {"pass": v[0], "detail": v[1]} for k, v in checks.items()},
                            "qty": qty, "notional_usd": notional, "loss_at_stop_usd": loss_at_stop,
                            "rr": rr, "fails": fails,
                            "already_holding": any(p["symbol"] == sym for p in opens)}, notes)
