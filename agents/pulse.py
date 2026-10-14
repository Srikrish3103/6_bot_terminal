"""PULSE — measures the crowd: bullish, bearish, or just hype."""
from __future__ import annotations

from datetime import datetime, timezone

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from core.indicators import ema, atr
from .base import Agent, clip

SLANG = {"moon": 2.5, "mooning": 2.5, "bullish": 2.0, "bearish": -2.0, "dump": -2.0, "dumping": -2.0,
         "pump": 1.0, "rekt": -2.5, "short": -1.0, "shorting": -1.5, "calls": 1.0, "puts": -1.0,
         "breakout": 1.5, "rug": -3.0, "hodl": 1.0, "loading": 1.0, "overvalued": -1.5}


class Pulse(Agent):
    name, role, color = "PULSE", "Crowd sentiment", "#3de0c4"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.vader = SentimentIntensityAnalyzer()
        self.vader.lexicon.update(SLANG)

    def analyse(self, sym, posts, df):
        pc = self.cfg["pulse"]
        now = datetime.now(timezone.utc)
        if len(posts) < 5:
            return self.report(sym, 0, "QUIET", "LOW", {"posts": len(posts)},
                               ["Too little crowd data — ignored"])

        vals, tagged = [], 0
        for p in posts:
            if p.tag in ("Bullish", "Bearish"):
                vals.append(0.8 if p.tag == "Bullish" else -0.8)
                tagged += 1
            else:
                vals.append(self.vader.polarity_scores(p.text)["compound"])
        mood = sum(vals) / len(vals)

        ages = [(now - p.created).total_seconds() / 3600 for p in posts]
        last24 = sum(a <= 24 for a in ages)
        span_days = max(1.0, min(7.0, max(ages) / 24))
        velocity = last24 / max(len(posts) / span_days, 1e-9)

        c = df["Close"]
        ext = (float(c.iloc[-1]) - float(ema(c, 20).iloc[-1])) / max(float(atr(df).iloc[-1]), 1e-9)
        same_side = (ext > 0) == (mood > 0)
        hype = velocity >= pc["hype_velocity"] and abs(ext) >= pc["hype_extension_atr"] and same_side

        notes, score = [], mood
        contrarian = "OK"
        if hype:
            score *= 0.3
            notes.append(f"HYPE: mentions x{velocity:.1f} and price {ext:+.1f} ATR from EMA20")
        if abs(mood) > 0.6:
            contrarian = "CAUTION"
            notes.append("Crowd one-sided — popular ≠ correct")
            score *= 0.7
        crowd = "BULLISH" if mood > 0.15 else "BEARISH" if mood < -0.15 else "NEUTRAL"
        conf = "HIGH" if len(posts) >= 40 and not hype else "LOW" if len(posts) < 15 else "MODERATE"
        return self.report(sym, clip(score), "HYPE" if hype else crowd, conf,
                           {"posts": len(posts), "tagged": tagged, "mood": round(mood, 2),
                            "velocity": round(velocity, 2), "extension_atr": round(ext, 2),
                            "crowd": crowd, "contrarian": contrarian, "hype": hype}, notes)
