"""News + crowd feeds. Live = free public sources; Demo = canned offline data.

Every live call is best-effort: a blocked/failed source returns [] and the
agent simply reports lower confidence instead of crashing the run.
"""
from __future__ import annotations

import logging
import random
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from .data import hist_vol
from .derivs import nse_chain, delta_chain, synthetic_chain
from .sources import NSE, Delta, fnum

log = logging.getLogger("feeds")
UA = {"User-Agent": "sentinel6/1.0 (personal research script)"}


@dataclass
class Headline:
    title: str
    source: str
    published: datetime
    url: str = ""


@dataclass
class Post:
    text: str
    created: datetime
    tag: str | None = None   # "Bullish"/"Bearish" when the platform provides it


def _now():
    return datetime.now(timezone.utc)


class LiveFeeds:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.timeout = cfg.get("http_timeout", 8)
        self.aliases = cfg.get("aliases", {})
        self.nse = NSE(self.timeout)
        self.delta = Delta(self.timeout)

    # ------------------------------------------------------------ derivatives
    def deriv(self, u, df) -> dict:
        """Option chain / funding for derivative segments. {} for spot."""
        seg = self.cfg["segments"].get(u.segment, {})
        if u.segment == "in_fno":
            if seg.get("instrument", "options") != "options":
                return {}
            ch = nse_chain(self.nse, u.symbol, u.is_index, seg.get("min_dte", 3), seg.get("max_dte", 45))
            return {"chain": ch} if ch else {"error": "NSE option chain unavailable"}
        if u.segment == "crypto_opt":
            ch = delta_chain(self.delta, u.symbol, seg.get("min_dte", 2), seg.get("max_dte", 35))
            return {"chain": ch} if ch else {"error": "Delta option chain unavailable"}
        if u.segment == "crypto_fut":
            fr = u.meta.get("funding")
            if fr is None:
                t = [x for x in self.delta.tickers("perpetual_futures") if x.get("symbol") == u.symbol]
                fr = fnum(t[0].get("funding_rate")) if t else None
            return {"funding": fr, "oi_usd": u.meta.get("oi_usd")}
        return {}

    # ------------------------------------------------------------ news
    def news(self, u) -> list[Headline]:
        items: list[Headline] = []
        if u.source == "yf" and u.segment != "in_fno":
            items += self._yf_news(u.data_symbol)
        if self.cfg["herald"].get("google_news", True):
            items += self._google_news(u.name or u.symbol)
        seen, uniq = set(), []
        for h in items:
            k = h.title.lower()[:80]
            if k not in seen:
                seen.add(k)
                uniq.append(h)
        return uniq

    def _yf_news(self, sym: str) -> list[Headline]:
        try:
            import yfinance as yf
            raw = yf.Ticker(sym).news or []
        except Exception as e:  # noqa
            log.debug("yf news %s: %s", sym, e)
            return []
        out = []
        for n in raw:
            c = n.get("content", n)
            title = c.get("title")
            if not title:
                continue
            ts = c.get("pubDate") or c.get("providerPublishTime")
            try:
                pub = (datetime.fromtimestamp(ts, timezone.utc) if isinstance(ts, (int, float))
                       else datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
            except Exception:  # noqa
                pub = _now()
            prov = c.get("provider", {})
            src = prov.get("displayName") if isinstance(prov, dict) else c.get("publisher", "Yahoo")
            url = (c.get("canonicalUrl") or {}).get("url", "") if isinstance(c.get("canonicalUrl"), dict) else c.get("link", "")
            out.append(Headline(title, src or "Yahoo", pub, url))
        return out

    def _google_news(self, query: str) -> list[Headline]:
        try:
            import feedparser
            q = urllib.parse.quote(f"{query} when:3d")
            url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, headers=UA, timeout=self.timeout)
            feed = feedparser.parse(r.content)
        except Exception as e:  # noqa
            log.debug("google news %s: %s", query, e)
            return []
        out = []
        for e in feed.entries[:30]:
            try:
                pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:  # noqa
                pub = _now()
            src = getattr(e, "source", {}).get("title", "Google News") if hasattr(e, "source") else "Google News"
            title = e.title.rsplit(" - ", 1)[0]
            out.append(Headline(title, src, pub, e.link))
        return out

    # ------------------------------------------------------------ crowd
    def crowd(self, u) -> list[Post]:
        posts: list[Post] = []
        if self.cfg["pulse"].get("stocktwits", True) and u.segment in ("us_spot", "crypto_spot", "crypto_fut", "crypto_opt"):
            st = u.symbol if u.segment == "us_spot" else (u.meta.get("und") or u.symbol.replace("USD", "")) + ".X"
            posts += self._stocktwits(st)
        if self.cfg["pulse"].get("reddit", True):
            posts += self._reddit(u.name or u.symbol)
        return posts

    def _stocktwits(self, st: str) -> list[Post]:
        try:
            r = requests.get(f"https://api.stocktwits.com/api/2/streams/symbol/{st}.json",
                             headers=UA, timeout=self.timeout)
            if r.status_code != 200:
                return []
            out = []
            for m in r.json().get("messages", []):
                ent = (m.get("entities") or {}).get("sentiment") or {}
                created = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
                out.append(Post(m.get("body", ""), created, ent.get("basic")))
            return out
        except Exception as e:  # noqa
            log.debug("stocktwits %s: %s", st, e)
            return []

    def _reddit(self, query: str) -> list[Post]:
        try:
            r = requests.get("https://www.reddit.com/search.json",
                             params={"q": query, "sort": "new", "t": "week", "limit": 100},
                             headers=UA, timeout=self.timeout)
            if r.status_code != 200:
                return []
            out = []
            for ch in r.json().get("data", {}).get("children", []):
                d = ch["data"]
                out.append(Post(f"{d.get('title', '')} {d.get('selftext', '')[:300]}",
                                datetime.fromtimestamp(d["created_utc"], timezone.utc)))
            return out
        except Exception as e:  # noqa
            log.debug("reddit %s: %s", query, e)
            return []


class DemoFeeds:
    """Deterministic offline feeds so the whole pipeline can be tested without internet."""
    GOOD = ["{n} beats earnings estimates, raises full-year guidance",
            "Analysts upgrade {n} on strong demand outlook",
            "{n} announces strategic partnership to expand AI capacity",
            "{n} record revenue as chip demand forecast rises"]
    BAD = ["{n} faces regulatory probe over disclosures",
           "{n} misses revenue estimates, cuts outlook",
           "Analyst downgrades {n} citing slowing growth",
           "{n} hit by lawsuit over product defects"]
    NEUTRAL = ["{n} to present at industry conference next week",
               "What to watch for {n} this week",
               "{n} policy update pending, investors wait"]
    CROWD_BULL = ["{n} to the moon, loading more", "breakout on {n} looks clean", "{n} bullish af"]
    CROWD_BEAR = ["{n} is overvalued, shorting", "{n} dumping hard", "selling my {n}, looks weak"]

    def __init__(self, cfg: dict, seed: int = 11):
        self.cfg = cfg
        self.aliases = cfg.get("aliases", {})
        self.seed = seed

    def _rng(self, sym):
        return random.Random(f"{self.seed}-{sym}")

    def deriv(self, u, df):
        rng = self._rng(u.key + "d")
        if u.segment in ("in_fno", "crypto_opt"):
            crypto = u.segment == "crypto_opt"
            ch = synthetic_chain(u, float(df["Close"].iloc[-1]), hist_vol(df, crypto=crypto), self.seed, crypto)
            return {"chain": ch}
        if u.segment == "crypto_fut":
            return {"funding": rng.gauss(0.01, 0.03), "oi_usd": rng.uniform(1e6, 5e8)}
        return {}

    def news(self, u):
        sym = u.key
        rng, n = self._rng(sym), u.name or u.symbol
        bias = rng.choice([-1, 0, 1, 1])
        pool = (self.GOOD if bias > 0 else self.BAD if bias < 0 else self.NEUTRAL) + self.NEUTRAL
        srcs = ["Reuters", "Bloomberg", "CNBC", "Economic Times", "CoinDesk"]
        return [Headline(rng.choice(pool).format(n=n), rng.choice(srcs),
                         _now() - timedelta(hours=rng.uniform(1, 60))) for _ in range(rng.randint(3, 8))]

    def crowd(self, u):
        sym = u.key
        rng, n = self._rng(sym + "c"), u.name or u.symbol
        bull = rng.random()
        spike = rng.random() < 0.25
        posts = []
        for _ in range(rng.randint(20, 60)):
            b = rng.random() < bull
            age = rng.uniform(0, 24) if (spike and rng.random() < 0.7) else rng.uniform(0, 168)
            posts.append(Post(rng.choice(self.CROWD_BULL if b else self.CROWD_BEAR).format(n=n),
                              _now() - timedelta(hours=age), "Bullish" if b else "Bearish"))
        return posts
