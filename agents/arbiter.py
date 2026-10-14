"""ARBITER — reads all five reports and makes the final call: TRADE / WAIT / REJECT."""
from __future__ import annotations

from .base import Agent


class Arbiter(Agent):
    name, role, color = "ARBITER", "Final decision", "#ffd23d"

    def decide(self, sym, direction, reps: dict):
        ac = self.cfg["arbiter"]
        d = 1 if direction == "LONG" else -1
        w = ac["weights"]
        aligned = {k: reps[k].score * d for k in ("kestrel", "prism", "herald", "pulse")}
        # low-confidence desks count half
        eff = {k: w[k] * (0.5 if reps[k].confidence == "LOW" else 1.0) for k in aligned}
        composite = sum(aligned[k] * eff[k] for k in aligned) / max(sum(eff.values()), 1e-9)

        conflicts = [reps[k].agent for k in ("herald", "pulse")
                     if aligned[k] < ac["conflict_threshold"] and reps[k].confidence != "LOW"]
        aegis = reps["aegis"]

        if aegis.details.get("already_holding"):
            dec, why = "HOLD", "Position already open — managed by stop/target"
        elif aegis.verdict == "REJECTED":
            dec, why = "REJECT", f"AEGIS veto: {', '.join(aegis.details['fails'])}"
        elif reps["prism"].verdict != "SETUP":
            dec, why = "WAIT", f"PRISM: {reps['prism'].verdict} — no confirmed setup"
        elif conflicts:
            dec, why = "WAIT", f"Signals conflict ({', '.join(conflicts)} against) — wait for confirmation"
        elif composite >= ac["trade_threshold"]:
            dec, why = "TRADE", "Consensus reached"
        elif composite < 0:
            dec, why = "REJECT", "Desks lean against the direction"
        else:
            dec, why = "WAIT", "No consensus. No trade."
        return self.report(sym, composite, dec,
                           "HIGH" if composite > 0.55 else "MODERATE" if composite > 0.3 else "LOW",
                           {"direction": direction, "composite": round(composite, 3),
                            "aligned": {k: round(v, 2) for k, v in aligned.items()}, "reason": why})
