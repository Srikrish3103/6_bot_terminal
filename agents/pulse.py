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

    @staticmethod
    def positioning(deriv: dict, spot: float):
        """Derivatives positioning score (-1..+1, bullish +) from option OI or perp funding."""
        notes, parts, info = [], [], {}
        ch = deriv.get("chain")
        if ch is not None:
            m = ch.metrics()
            info.update(m)
            if m["pcr"] is not None:
                pcr = m["pcr"]
                s = clip((pcr - 1.0) * 1.5)                 # put writers > call writers = support
                if pcr > 1.6:
                    s *= 0.4
                    notes.append(f"PCR {pcr:.2f} extreme — contrarian risk")
                parts.append(s)
            if m["max_pain"]:
                pull = (m["max_pain"] - spot) / spot
                parts.append(clip(pull * 25) * (1.0 if m["dte"] <= 7 else 0.4))
            if m["net_put_writing"]:
                parts.append(clip(m["net_put_writing"] / max(1.0, sum(q.oi for q in ch.puts.values()) * 0.05)))
        fr = deriv.get("funding")
        if fr is not None:
            fr = fr / 100                                   # Delta reports funding in % per 8h
            info["funding"] = fr
            parts.append(-clip(fr / 0.0005) * 0.8)          # crowded longs pay -> contrarian bearish
            if abs(fr) >= 0.0005:
                notes.append(f"Funding {fr * 100:+.3f}%/8h — crowded {'longs' if fr > 0 else 'shorts'}")
        if not parts:
            return None, info, notes
        return clip(sum(parts) / len(parts)), info, notes

    def analyse(self, sym, posts, df, deriv=None, buzz=None):
        pc = self.cfg["pulse"]
        now = datetime.now(timezone.utc)
        pos_score, pos_info, pos_notes = self.positioning(deriv or {}, float(df["Close"].iloc[-1]))
        if buzz:
            pos_info = {**(pos_info or {}), "buzz": buzz}
        if len(posts) < 5 and buzz and buzz.get("mentions_24h_ago"):
            # no readable posts, but we know how loud the crowd is: use it for the hype filter
            vel = buzz["mentions"] / max(buzz["mentions_24h_ago"], 1.0)
            c = df["Close"]
            ext = (float(c.iloc[-1]) - float(ema(c, 20).iloc[-1])) / max(float(atr(df).iloc[-1]), 1e-9)
            notes = [f"Mentions {buzz['mentions']:.0f} vs {buzz['mentions_24h_ago']:.0f} 24h ago (x{vel:.1f}, rank #{buzz.get('rank')})"]
            hype = vel >= pc["hype_velocity"] and abs(ext) >= pc["hype_extension_atr"]
            score = pos_score if pos_score is not None else 0.0
            if hype:
                score *= 0.3
                notes.append(f"HYPE: mention spike x{vel:.1f} with price {ext:+.1f} ATR from EMA20")
            v = "HYPE" if hype else "BULLISH" if score > 0.15 else "BEARISH" if score < -0.15 else "NEUTRAL"
            return self.report(sym, clip(score), v, "MODERATE" if pos_score is not None else "LOW",
                               {"posts": len(posts), "velocity": round(vel, 2), "extension_atr": round(ext, 2),
                                "hype": hype, "positioning": pos_info,
                                "pos_score": None if pos_score is None else round(pos_score, 2)}, notes + pos_notes)
        if len(posts) < 5:
            if pos_score is not None:
                v = "BULLISH" if pos_score > 0.15 else "BEARISH" if pos_score < -0.15 else "NEUTRAL"
                return self.report(sym, pos_score, v, "MODERATE",
                                   {"posts": len(posts), "positioning": pos_info, "pos_score": round(pos_score, 2)},
                                   ["No social data — positioning only"] + pos_notes)
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
        if buzz and buzz.get("mentions_24h_ago"):
            velocity = max(velocity, buzz["mentions"] / max(buzz["mentions_24h_ago"], 1.0))

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
        if pos_score is not None:
            w = pc.get("positioning_weight", 0.5)
            score = (1 - w) * score + w * pos_score
            notes += pos_notes
        crowd = "BULLISH" if mood > 0.15 else "BEARISH" if mood < -0.15 else "NEUTRAL"
        conf = "HIGH" if len(posts) >= 40 and not hype else "LOW" if len(posts) < 15 else "MODERATE"
        return self.report(sym, clip(score), "HYPE" if hype else crowd, conf,
                           {"posts": len(posts), "tagged": tagged, "mood": round(mood, 2),
                            "velocity": round(velocity, 2), "extension_atr": round(ext, 2),
                            "crowd": crowd, "contrarian": contrarian, "hype": hype,
                            "positioning": pos_info, "pos_score": None if pos_score is None else round(pos_score, 2)}, notes)
