"""HERALD — reads the news; every catalyst gets scored before the trade."""
from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from .base import Agent, clip

log = logging.getLogger("herald")

# (regex, impact score, catalyst label) — impact >= 0.5 counts as HIGH-IMPACT
CATALYSTS = [
    (r"beat[s]? (earnings|estimates|expectations)|tops estimates", 0.7, "Earnings beat"),
    (r"miss(es|ed)? (earnings|estimates|expectations)", -0.7, "Earnings miss"),
    (r"raise[sd]? (guidance|outlook|forecast)|guidance (raise|boost)", 0.7, "Guidance raised"),
    (r"(cut|lower)[s]? (guidance|outlook|forecast)", -0.7, "Guidance cut"),
    (r"upgrade[sd]?", 0.5, "Analyst upgrade"),
    (r"downgrade[sd]?", -0.5, "Analyst downgrade"),
    (r"demand (forecast )?(rise|rises|surge|jump)|record (revenue|sales|profit)", 0.5, "Demand rising"),
    (r"partnership|deal with|contract win|wins contract|acquire[sd]?", 0.35, "Deal/partnership"),
    (r"buyback|repurchase|dividend (hike|increase)", 0.4, "Capital return"),
    (r"etf (approval|approved|inflows?)", 0.6, "ETF flows"),
    (r"probe|investigation|subpoena|antitrust|sec charges", -0.6, "Regulatory probe"),
    (r"lawsuit|sued|class action", -0.4, "Litigation"),
    (r"hack|exploit|breach|outage", -0.6, "Security/outage"),
    (r"recall|ban[s]?\b|sanction", -0.5, "Ban/recall/sanction"),
    (r"layoff|job cuts|restructur", -0.25, "Restructuring"),
    (r"rate (hike|hikes)|hawkish", -0.35, "Hawkish rates"),
    (r"rate cut|dovish", 0.35, "Dovish rates"),
    (r"energy supply risk|oil (spike|surge)", -0.3, "Energy risk"),
    (r"policy update|pending|awaits?|ahead of", 0.0, "Pending event"),
]


class Herald(Agent):
    name, role, color = "HERALD", "News desk", "#ff6a3d"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.vader = SentimentIntensityAnalyzer()

    def score_headline(self, title: str):
        t = title.lower()
        hits = [(imp, lab) for rx, imp, lab in CATALYSTS if re.search(rx, t)]
        vader = self.vader.polarity_scores(title)["compound"]
        if hits:
            imp, lab = max(hits, key=lambda h: abs(h[0]))
            s = 0.7 * imp + 0.3 * vader
        else:
            imp, lab, s = 0.0, None, 0.5 * vader
        return clip(s), lab, abs(imp) >= 0.5

    def _llm_scores(self, titles: list[str]) -> list[float] | None:
        lc = self.cfg.get("llm", {})
        if not lc.get("enabled") or not titles:
            return None
        prompt = ("Score each headline's likely short-term price impact for the asset from -1 (very bearish) "
                  "to 1 (very bullish). Reply ONLY with a JSON array of numbers.\n" + json.dumps(titles))
        try:
            r = requests.post(lc["url"], json={"model": lc["model"], "prompt": prompt, "stream": False},
                              timeout=60)
            arr = json.loads(re.search(r"\[.*\]", r.json()["response"], re.S).group(0))
            return [clip(x) for x in arr] if len(arr) == len(titles) else None
        except Exception as e:  # noqa
            log.warning("LLM scoring failed, using rules: %s", e)
            return None

    def analyse(self, sym, headlines):
        max_age = self.cfg["herald"]["max_age_hours"]
        now = datetime.now(timezone.utc)
        fresh = [h for h in headlines if (now - h.published).total_seconds() / 3600 <= max_age]
        if not fresh:
            return self.report(sym, 0, "NO NEWS", "LOW", {"headlines": 0})

        llm = self._llm_scores([h.title for h in fresh])
        scored, by_cat = [], defaultdict(set)
        num = den = 0.0
        for i, h in enumerate(fresh):
            s, lab, high = self.score_headline(h.title)
            if llm:
                s = clip(0.5 * s + 0.5 * llm[i])
            age_h = (now - h.published).total_seconds() / 3600
            w = math.exp(-age_h / 24) * (2.0 if high else 1.0)
            num += s * w
            den += w
            if lab:
                by_cat[lab].add(h.source)
            scored.append({"title": h.title, "source": h.source, "age_h": round(age_h, 1),
                           "score": round(s, 2), "catalyst": lab, "high_impact": high})
        score = num / den if den else 0.0

        top = max(scored, key=lambda x: abs(x["score"]) * (2 if x["high_impact"] else 1))
        cat = top["catalyst"]
        catalyst = None
        if top["high_impact"]:
            srcs = by_cat.get(cat, set())
            catalyst = {"headline": top["title"], "type": cat,
                        "source_check": "PASS" if len(srcs) >= 2 else "SINGLE SOURCE",
                        "market_impact": "HIGH", "time_sensitivity": "24H" if top["age_h"] < 24 else "72H"}
        verdict = "BULLISH" if score > 0.15 else "BEARISH" if score < -0.15 else "WATCH"
        conf = "HIGH" if len(fresh) >= 6 and catalyst and catalyst["source_check"] == "PASS" else \
               "LOW" if len(fresh) < 3 else "MODERATE"
        scored.sort(key=lambda x: -abs(x["score"]))
        return self.report(sym, score, verdict, conf,
                           {"headlines": len(fresh), "catalyst": catalyst, "top": scored[:6],
                            "llm": bool(llm)})
