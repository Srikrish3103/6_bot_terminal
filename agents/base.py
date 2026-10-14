from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Report:
    agent: str
    role: str
    symbol: str
    score: float              # -1..+1 (sign = bullish/bearish view)
    verdict: str              # short status word shown in the UI
    confidence: str = "MODERATE"
    details: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class Agent:
    name = "AGENT"
    role = "generic"
    color = "#ffffff"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def report(self, symbol, score, verdict, confidence="MODERATE", details=None, notes=None) -> Report:
        return Report(self.name, self.role, symbol, round(float(score), 3), verdict,
                      confidence, details or {}, notes or [])


def clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, float(x)))
