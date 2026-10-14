"""Full-market universe + price history.

Once a day (universe_refresh_hours) for every enabled segment:
  1. download the complete symbol list (NYSE/Nasdaq/AMEX, NSE EQ, Binance USDT, NSE F&O, Delta perps/options)
  2. liquidity pre-screen (price + average traded value) on a light 1-month download
  3. download 2 years of daily history for the survivors only, cached in data/cache/
Every cycle afterwards only the last few bars are refreshed, so 15-minute cycles stay fast.
"""
from __future__ import annotations

import json
import logging
import pickle
import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .data import synthetic_candles
from .instruments import (DELTA_OPT_CONTRACT, DELTA_PERP_FALLBACK, FNO_FALLBACK, INDEX_YF, SEGMENTS,
                          Underlying)
from .sources import NSE, Binance, Delta, fnum, nse_equity_symbols, parse_delta_option_symbol, us_symbols

log = logging.getLogger("universe")
CHUNK = 250


def _clean(df, min_bars=1):
    if df is None or len(df) == 0:
        return None
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"]).copy()
    df["Volume"] = df["Volume"].fillna(0)
    return df if len(df) >= min_bars else None


def yf_chunks(symbols, period, progress=None, chunk=CHUNK) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    out: dict[str, pd.DataFrame] = {}
    symbols = list(dict.fromkeys(symbols))
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        for attempt in range(2):
            try:
                raw = yf.download(part, period=period, interval="1d", group_by="ticker",
                                  auto_adjust=True, threads=True, progress=False)
                break
            except Exception as e:  # noqa
                log.debug("yf chunk failed (%s), retrying", e)
                raw = None
                time.sleep(3)
        if raw is not None and len(raw):
            for s in part:
                try:
                    df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                    df = _clean(df)
                    if df is not None:
                        out[s] = df
                except KeyError:
                    pass
        if progress:
            progress(min(i + chunk, len(symbols)), len(symbols))
    return out


def adv(df, n=20):
    t = df.tail(n)
    return float((t["Close"] * t["Volume"]).mean()) if len(t) else 0.0


def merge(old: pd.DataFrame | None, new: pd.DataFrame | None):
    if old is None:
        return new
    if new is None or not len(new):
        return old
    if getattr(old.index, "tz", None) is not None and getattr(new.index, "tz", None) is None:
        new.index = new.index.tz_localize(old.index.tz)
    elif getattr(old.index, "tz", None) is None and getattr(new.index, "tz", None) is not None:
        new.index = new.index.tz_localize(None)
    df = pd.concat([old, new])
    return df[~df.index.duplicated(keep="last")].sort_index()


class MarketData:
    def __init__(self, cfg: dict, root: Path, demo: bool = False):
        self.cfg, self.demo = cfg, demo
        self.dir = root / "data" / "cache"
        self.dir.mkdir(parents=True, exist_ok=True)
        tag = "demo_" if demo else ""
        self.f_univ = self.dir / f"{tag}universe.json"
        self.f_hist = self.dir / f"{tag}history.pkl"
        self.universe: list[Underlying] = []
        self.stats: dict = {}
        self.history: dict[str, pd.DataFrame] = {}
        self.built_at = 0.0
        self.timeout = cfg.get("http_timeout", 10)

    # ------------------------------------------------------------------ public
    def load(self, emit=None) -> tuple[list[Underlying], dict[str, pd.DataFrame], dict]:
        emit = emit or (lambda *a: None)
        hours = self.cfg.get("universe_refresh_hours", 20)
        if not self.universe:
            self._load_cache()
        if not self.universe or time.time() - self.built_at > hours * 3600:
            self._build(emit)
        elif not self.demo:
            self._refresh_recent(emit)
        data = {u.key: self.history[u.key] for u in self.universe
                if u.key in self.history and len(self.history[u.key]) >= 120}
        return self.universe, data, self.stats

    # ------------------------------------------------------------------ cache
    def _load_cache(self):
        try:
            j = json.loads(self.f_univ.read_text())
            self.universe = [Underlying(**u) for u in j["universe"]]
            self.stats, self.built_at = j["stats"], j["built_at"]
            with open(self.f_hist, "rb") as f:
                self.history = pickle.load(f)
        except Exception:  # noqa
            self.universe, self.history = [], {}

    def _save_cache(self):
        self.f_univ.write_text(json.dumps({"built_at": self.built_at, "stats": self.stats,
                                           "universe": [asdict(u) for u in self.universe]}))
        with open(self.f_hist, "wb") as f:
            pickle.dump(self.history, f)

    # ------------------------------------------------------------------ daily build
    def _build(self, emit):
        segs = self.cfg["segments"]
        t0 = time.time()
        universe, history, stats = [], {}, {}
        for seg in SEGMENTS:
            c = segs.get(seg, {})
            if not c.get("enabled"):
                continue
            emit("KESTREL", "work", f"{seg}: loading full symbol list…")
            try:
                us, hist, listed = getattr(self, f"_build_{seg}")(c, emit)
            except Exception as e:  # noqa
                log.exception("universe build failed for %s", seg)
                emit("KESTREL", "error", f"{seg}: universe build failed ({e})")
                continue
            universe += us
            history.update(hist)
            with_data = sum(1 for u in us if u.key in hist and len(hist[u.key]) >= 120)
            stats[seg] = {"listed": listed, "liquid": len(us), "with_data": with_data}
            emit("KESTREL", "info", f"{seg}: {listed:,} listed → {len(us):,} liquid → {with_data:,} with 2y history")
        self.universe, self.history, self.stats = universe, history, stats
        self.built_at = time.time()
        self._save_cache()
        emit("KESTREL", "info", f"Universe rebuilt in {time.time() - t0:.0f}s: "
                                f"{sum(s['listed'] for s in stats.values()):,} listed, "
                                f"{sum(s['with_data'] for s in stats.values()):,} tradeable")

    def _prog(self, emit, label):
        def f(done, total):
            emit("KESTREL", "work", f"{label}: {done:,}/{total:,}")
        return f

    def _yf_segment(self, seg, c, emit, names: dict, ccy, suffix=""):
        """names: {yf symbol: company}. Pre-screen on 1 month, then 2y for survivors."""
        listed = len(names)
        if c.get("universe", "full") == "full" and not self.demo:
            light = yf_chunks(list(names), "1mo", self._prog(emit, f"{seg} pre-screen"))
            keep = [s for s, df in light.items()
                    if len(df) >= 10 and df["Close"].iloc[-1] >= c.get("min_price", 0) and adv(df) >= c.get("min_adv", 0)]
            emit("KESTREL", "work", f"{seg}: {len(keep):,} of {listed:,} pass liquidity — fetching 2y history…")
            full = yf_chunks(keep, self.cfg["kestrel"].get("history", "2y"), self._prog(emit, f"{seg} history"))
        else:
            keep = list(names)
            full = {} if self.demo else yf_chunks(keep, self.cfg["kestrel"].get("history", "2y"), self._prog(emit, f"{seg} history"))
        us, hist = [], {}
        for s in keep:
            base = s.replace(suffix, "") if suffix else s.replace("-USD", "")
            u = Underlying(f"{seg}:{base}", seg, base, s, "yf", ccy, 1.0, names.get(s, base),
                           meta={"fractional": c.get("fractional", False)})
            us.append(u)
            if s in full:
                hist[u.key] = full[s]
        return us, hist, listed

    # ---- per segment
    def _build_us_spot(self, c, emit):
        if c.get("universe", "full") == "full" and not self.demo:
            names = us_symbols(c.get("include_etfs", False), self.timeout)
            if not names:
                emit("KESTREL", "error", "US symbol files unavailable — using config list")
                names = {s: s for s in c["symbols"]}
        else:
            names = self._demo_names("us_spot", c) if self.demo else {s: s for s in c["symbols"]}
        return self._finish_demo("us_spot", c, *self._yf_segment("us_spot", c, emit, names, "USD"))

    def _build_in_spot(self, c, emit):
        if c.get("universe", "full") == "full" and not self.demo:
            raw = nse_equity_symbols(self.timeout)
            names = {f"{k}.NS": v for k, v in raw.items()}
            if not names:
                emit("KESTREL", "error", "NSE equity list unavailable — using config list")
                names = {s: s for s in c["symbols"]}
        else:
            names = self._demo_names("in_spot", c) if self.demo else {s: s for s in c["symbols"]}
        return self._finish_demo("in_spot", c, *self._yf_segment("in_spot", c, emit, names, "INR", ".NS"))

    def _build_crypto_spot(self, c, emit):
        if c.get("universe", "full") == "full" and not self.demo:
            pairs = Binance(self.timeout).usdt_pairs()
            if pairs:
                keep = {b: p for b, p in pairs.items() if p["quote_volume"] >= c.get("min_adv", 0)}
                emit("KESTREL", "work", f"crypto_spot: {len(keep)} of {len(pairs)} Binance pairs liquid — fetching history…")
                bn = Binance(self.timeout)
                with ThreadPoolExecutor(max_workers=6) as ex:
                    res = dict(zip(keep, ex.map(lambda b: bn.klines(keep[b]["symbol"], 500), keep)))
                us, hist = [], {}
                for b, p in keep.items():
                    u = Underlying(f"crypto_spot:{b}", "crypto_spot", b, p["symbol"], "binance", "USD", 1.0,
                                   self.cfg.get("aliases", {}).get(b, b) + " crypto",
                                   meta={"fractional": True, "qv": p["quote_volume"]})
                    us.append(u)
                    if res.get(b) is not None:
                        hist[u.key] = res[b]
                return us, hist, len(pairs)
            emit("KESTREL", "error", "Binance unavailable — using config list via yfinance")
        names = self._demo_names("crypto_spot", c) if self.demo else {s: s for s in c["symbols"]}
        return self._finish_demo("crypto_spot", c, *self._yf_segment("crypto_spot", c, emit, names, "USD"))

    def _build_in_fno(self, c, emit):
        lots = {} if self.demo else NSE(self.timeout).lot_sizes()
        if not lots:
            if not self.demo:
                emit("KESTREL", "error", "NSE lot file unavailable — fallback F&O list")
            lots = FNO_FALLBACK
        indices = set(c.get("indices", []))
        names, lot_of = {}, {}
        for s, lot in lots.items():
            is_idx = s in INDEX_YF
            if (is_idx and s not in indices) or (not is_idx and any(k in s for k in ("NIFTY", "SENSEX", "BANKEX"))):
                continue
            ys = INDEX_YF.get(s, f"{s}.NS")
            names[ys], lot_of[ys] = s, lot
        full = {} if self.demo else yf_chunks(list(names), self.cfg["kestrel"].get("history", "2y"),
                                                self._prog(emit, "in_fno history"))
        us, hist = [], {}
        for ys, s in names.items():
            is_idx = s in INDEX_YF
            nm = self.cfg.get("aliases", {}).get(s, s)
            u = Underlying(f"in_fno:{s}", "in_fno", s, ys, "yf", "INR", float(lot_of[ys]),
                           nm if is_idx else f"{nm} share", is_index=is_idx)
            us.append(u)
            if ys in full:
                hist[u.key] = full[ys]
        if self.demo:
            hist = synthetic_candles(us)
        return us, hist, len(lots)

    def _build_crypto_fut(self, c, emit):
        d = Delta(self.timeout)
        perps = {}
        listed = 0
        if not self.demo:
            ticks = d.tickers("perpetual_futures")
            listed = len(ticks)
            cv = {p.get("symbol"): fnum(p.get("contract_value"), 0) for p in d.products("perpetual_futures")}
            for t in ticks:
                if fnum(t.get("turnover_usd")) < c.get("min_adv", 0):
                    continue
                sym = t.get("symbol")
                und = t.get("underlying_asset_symbol") or sym.replace("USDT", "").replace("USD", "")
                perps[sym] = {"und": und, "cv": cv.get(sym) or fnum(t.get("contract_value"), 0) or
                              DELTA_PERP_FALLBACK.get(sym, (und, 1.0))[1],
                              "funding": fnum(t.get("funding_rate")), "oi_usd": fnum(t.get("oi_value_usd")),
                              "turnover_usd": fnum(t.get("turnover_usd"))}
        if not perps:
            if not self.demo:
                emit("KESTREL", "error", "Delta perps unavailable — fallback list")
            perps = {k: {"und": u, "cv": cv} for k, (u, cv) in DELTA_PERP_FALLBACK.items()}
            listed = listed or len(perps)
        aliases = self.cfg.get("aliases", {})
        us = [Underlying(f"crypto_fut:{s}", "crypto_fut", s, s, "delta", "USD", m["cv"],
                         aliases.get(m["und"], m["und"]) + " crypto", meta=m) for s, m in perps.items()]
        if self.demo:
            return us, synthetic_candles(us), listed
        emit("KESTREL", "work", f"crypto_fut: {len(us)} perps — fetching history…")
        with ThreadPoolExecutor(max_workers=6) as ex:
            res = dict(zip([u.key for u in us], ex.map(lambda u: d.candles(u.data_symbol, 450), us)))
        return us, {k: v for k, v in res.items() if v is not None}, listed

    def _build_crypto_opt(self, c, emit):
        d = Delta(self.timeout)
        unds = {}
        if c.get("universe", "full") == "full" and not self.demo:
            for t in d.tickers("call_options"):
                p = parse_delta_option_symbol(t.get("symbol", ""))
                if p:
                    unds[p[1]] = unds.get(p[1], 0) + 1
            unds = {u: n for u, n in unds.items() if n >= c.get("min_strikes", 10)}
        listed = len(unds)
        if not unds:
            unds = {u: 0 for u in c.get("underlyings", ["BTC", "ETH"])}
            listed = listed or len(unds)
        aliases = self.cfg.get("aliases", {})
        us = [Underlying(f"crypto_opt:{u}", "crypto_opt", u, f"{u}USD", "delta", "USD",
                         DELTA_OPT_CONTRACT.get(u, 1.0), aliases.get(u, u) + " crypto") for u in unds]
        if self.demo:
            return us, synthetic_candles(us), listed
        with ThreadPoolExecutor(max_workers=4) as ex:
            res = dict(zip([u.key for u in us], ex.map(lambda u: d.candles(u.data_symbol, 450), us)))
        return us, {k: v for k, v in res.items() if v is not None}, listed

    # ------------------------------------------------------------------ cycle refresh
    def _refresh_recent(self, emit):
        """Append the latest bars (today's bar included) to the cached history."""
        yf_items = [u for u in self.universe if u.source == "yf"]
        bn_items = [u for u in self.universe if u.source == "binance"]
        dl_items = {u.data_symbol: u for u in self.universe if u.source == "delta"}
        if yf_items:
            new = yf_chunks([u.data_symbol for u in yf_items], "5d", self._prog(emit, "refresh prices"))
            for u in yf_items:
                if u.data_symbol in new:
                    self.history[u.key] = merge(self.history.get(u.key), new[u.data_symbol])
        if bn_items:
            bn = Binance(self.timeout)
            with ThreadPoolExecutor(max_workers=6) as ex:
                res = list(ex.map(lambda u: bn.klines(u.data_symbol, 5), bn_items))
            for u, df in zip(bn_items, res):
                self.history[u.key] = merge(self.history.get(u.key), df)
        if dl_items:
            d = Delta(self.timeout)
            with ThreadPoolExecutor(max_workers=6) as ex:
                res = dict(zip(dl_items, ex.map(lambda s: d.candles(s, 5), dl_items)))
            for s, df in res.items():
                for u in self.universe:
                    if u.source == "delta" and u.data_symbol == s:
                        self.history[u.key] = merge(self.history.get(u.key), df)

    # ------------------------------------------------------------------ demo
    DEMO_LISTED = {"us_spot": 6200, "in_spot": 2150, "crypto_spot": 410}

    def _demo_names(self, seg, c):
        names = {s: s.replace(".NS", "").replace("-USD", "") for s in c["symbols"]}
        rng = random.Random(seg)
        n_extra = {"us_spot": 1400, "in_spot": 700, "crypto_spot": 150}[seg]
        for i in range(n_extra):
            if seg == "us_spot":
                s = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(rng.choice([3, 4])))
            elif seg == "in_spot":
                s = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(rng.choice([5, 6, 7]))) + ".NS"
            else:
                s = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3)) + "-USD"
            names.setdefault(s, s)
        return names

    def _finish_demo(self, seg, c, us, hist, listed):
        if not self.demo:
            return us, hist, listed
        hist = synthetic_candles(us, seed={"us_spot": 11, "in_spot": 12, "crypto_spot": 13}.get(seg, 7))
        return us, hist, self.DEMO_LISTED.get(seg, listed)

