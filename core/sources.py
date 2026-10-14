"""HTTP clients for NSE (India F&O) and Delta Exchange India (crypto derivatives).

Both are public, no API key. Every method is best-effort and returns None/[] on
failure so the desk degrades gracefully. Run `python run.py --check` to test them.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

log = logging.getLogger("sources")

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


def fnum(x, default=0.0):
    try:
        if x is None or x == "" or x == "-":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


# ====================================================================== NSE
class NSE:
    BASE = "https://www.nseindia.com"
    LOTS_URLS = ["https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
                 "https://archives.nseindia.com/content/fo/fo_mktlots.csv"]

    def __init__(self, timeout=10):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
                               "Accept-Language": "en-US,en;q=0.9", "Referer": f"{self.BASE}/option-chain"})
        self._warm_at = 0.0

    def _warm(self, force=False):
        if force or time.time() - self._warm_at > 240:
            try:
                self.s.get(f"{self.BASE}/option-chain", timeout=self.timeout)
                self._warm_at = time.time()
            except Exception as e:  # noqa
                log.debug("NSE warm-up failed: %s", e)

    def get_json(self, path, params=None):
        self._warm()
        for attempt in range(2):
            try:
                r = self.s.get(f"{self.BASE}{path}", params=params, timeout=self.timeout)
                if r.status_code in (401, 403) and attempt == 0:
                    self._warm(force=True)
                    continue
                if r.status_code == 200 and r.text.strip():
                    return r.json()
                log.debug("NSE %s -> HTTP %s", path, r.status_code)
            except Exception as e:  # noqa
                log.debug("NSE %s failed: %s", path, e)
            time.sleep(0.6)
        return None

    def lot_sizes(self) -> dict[str, int]:
        """{SYMBOL: lot size} for every F&O underlying (indices + stocks)."""
        for url in self.LOTS_URLS:
            try:
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code != 200:
                    continue
                out = {}
                for row in csv.reader(io.StringIO(r.text)):
                    if len(row) < 3:
                        continue
                    sym = row[1].strip().upper()
                    if not sym or sym == "SYMBOL" or " " in sym:
                        continue
                    for cell in row[2:]:
                        v = fnum(cell.strip(), 0)
                        if v > 0:
                            out[sym] = int(v)
                            break
                if out:
                    return out
            except Exception as e:  # noqa
                log.debug("lot file %s: %s", url, e)
        return {}

    def expiries(self, symbol: str) -> list[datetime]:
        j = self.get_json("/api/option-chain-contract-info", {"symbol": symbol})
        dates = (j or {}).get("expiryDates") or []
        out = []
        for d in dates:
            try:
                out.append(datetime.strptime(d, "%d-%b-%Y").replace(hour=10, tzinfo=timezone.utc))  # 15:30 IST
            except ValueError:
                pass
        return sorted(out)

    def chain_rows(self, symbol: str, is_index: bool, expiry: datetime | None):
        """Returns (rows, underlying_value, all_expiries). rows: list of NSE strike dicts with CE/PE."""
        exp_str = expiry.strftime("%d-%b-%Y") if expiry else None
        if exp_str:
            j = self.get_json("/api/option-chain-v3",
                              {"type": "Indices" if is_index else "Equity", "symbol": symbol, "expiry": exp_str})
            rows = ((j or {}).get("records") or {}).get("data") or []
            if rows:
                return rows, fnum((j["records"]).get("underlyingValue")), None
        # legacy endpoints (all expiries in one payload)
        j = self.get_json(f"/api/option-chain-{'indices' if is_index else 'equities'}", {"symbol": symbol})
        rec = (j or {}).get("records") or {}
        rows = rec.get("data") or []
        if exp_str:
            rows = [r for r in rows if r.get("expiryDate") == exp_str] or rows
        exps = []
        for d in rec.get("expiryDates") or []:
            try:
                exps.append(datetime.strptime(d, "%d-%b-%Y").replace(hour=10, tzinfo=timezone.utc))
            except ValueError:
                pass
        return rows, fnum(rec.get("underlyingValue")), sorted(exps)


# ====================================================================== Delta Exchange India
class Delta:
    BASE = "https://api.india.delta.exchange/v2"

    def __init__(self, timeout=10):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "sentinel6/1.0", "Accept": "application/json"})

    def _get(self, path, params=None):
        try:
            r = self.s.get(f"{self.BASE}{path}", params=params, timeout=self.timeout)
            if r.status_code == 200:
                j = r.json()
                return j.get("result", j) if isinstance(j, dict) else j
            log.debug("Delta %s -> HTTP %s %s", path, r.status_code, r.text[:200])
        except Exception as e:  # noqa
            log.debug("Delta %s failed: %s", path, e)
        return None

    def tickers(self, contract_types: str, underlying: str | None = None) -> list[dict]:
        p = {"contract_types": contract_types}
        if underlying:
            p["underlying_asset_symbols"] = underlying
        return self._get("/tickers", p) or []

    def products(self, contract_types: str) -> list[dict]:
        return self._get("/products", {"contract_types": contract_types, "states": "live"}) or []

    def candles(self, symbol: str, days: int = 450, resolution: str = "1d") -> pd.DataFrame | None:
        end = int(time.time())
        start = end - days * 86400
        res = self._get("/history/candles", {"resolution": resolution, "symbol": symbol, "start": start, "end": end})
        if not res:
            return None
        df = pd.DataFrame(res)
        if df.empty or "close" not in df:
            return None
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.sort_values("time").set_index("time")
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        for c in ("Open", "High", "Low", "Close", "Volume"):
            df[c] = pd.to_numeric(df.get(c, 0), errors="coerce")
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])


def parse_delta_option_symbol(sym: str):
    """C-BTC-90000-310125 -> ('C', 'BTC', 90000.0, datetime 31-01-2025 12:00 UTC)."""
    try:
        kind, und, strike, ds = sym.split("-")[:4]
        exp = datetime.strptime(ds, "%d%m%y").replace(hour=12, tzinfo=timezone.utc)   # 17:30 IST
        return kind.upper(), und, float(strike), exp
    except Exception:  # noqa
        return None


def now_utc():
    return datetime.now(timezone.utc)


def days_to(exp: datetime) -> float:
    return (exp - now_utc()) / timedelta(days=1)


# ====================================================================== full symbol lists
NASDAQ_FILES = ["https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
                "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"]
_BAD_NAME = re.compile(r"\b(warrants?|rights?|units?|preferred|debentures?|notes due|subordinated|"
                       r"depositary shares? representing|beneficial interests?|acquisition corp)\b|\d%", re.I)


def us_symbols(include_etfs=False, timeout=15) -> dict[str, str]:
    """{yfinance symbol: company name} for every NYSE / Nasdaq / AMEX common stock (+ETFs if asked)."""
    out = {}
    for url in NASDAQ_FILES:
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": BROWSER_UA})
            lines = [l for l in r.text.splitlines() if l and not l.startswith("File Creation Time")]
            hdr = lines[0].split("|")
            for line in lines[1:]:
                row = dict(zip(hdr, line.split("|")))
                sym = row.get("Symbol") or row.get("ACT Symbol") or ""
                name = row.get("Security Name", "")
                if not sym or row.get("Test Issue") == "Y":
                    continue
                if row.get("ETF") == "Y" and not include_etfs:
                    continue
                if any(c in sym for c in "$^+=") or _BAD_NAME.search(name):
                    continue
                if row.get("Financial Status", "N") not in ("N", "", None):
                    continue           # deficient / delinquent / bankrupt
                out[sym.replace(".", "-")] = name.split(" - ")[0].strip()
        except Exception as e:  # noqa
            log.warning("US symbol file %s failed: %s", url, e)
    return out


def nse_equity_symbols(timeout=15) -> dict[str, str]:
    """{SYMBOL: company} for every NSE EQ-series stock."""
    for url in ("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                "https://archives.nseindia.com/content/equities/EQUITY_L.csv"):
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": BROWSER_UA})
            if r.status_code != 200:
                continue
            out = {}
            rd = csv.reader(io.StringIO(r.text))
            hdr = [h.strip().upper() for h in next(rd)]
            for row in rd:
                d = dict(zip(hdr, [c.strip() for c in row]))
                if d.get("SERIES") == "EQ" and d.get("SYMBOL"):
                    out[d["SYMBOL"]] = d.get("NAME OF COMPANY", d["SYMBOL"])
            if out:
                return out
        except Exception as e:  # noqa
            log.debug("NSE equity list %s: %s", url, e)
    return {}


class Binance:
    """Public market data mirror (no key, no geo-restriction)."""
    BASES = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3"]
    STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "USD1", "PYUSD", "EURI", "XUSD",
              "USDE", "BFUSD", "RLUSD", "USDS", "GBP", "TRY", "BRL", "WBTC", "WBETH", "BETH", "PAXG"}

    def __init__(self, timeout=10):
        self.timeout = timeout
        self.s = requests.Session()

    def _get(self, path, params=None):
        for b in self.BASES:
            try:
                r = self.s.get(b + path, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
            except Exception as e:  # noqa
                log.debug("Binance %s%s: %s", b, path, e)
        return None

    def usdt_pairs(self) -> dict[str, dict]:
        """{BASE: {symbol, quote_volume, last}} for every trading spot USDT pair."""
        info = self._get("/exchangeInfo", {"permissions": "SPOT"}) or {}
        ok = {s["symbol"]: s["baseAsset"] for s in info.get("symbols", [])
              if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}
        out = {}
        for t in self._get("/ticker/24hr") or []:
            sym = t.get("symbol")
            base = ok.get(sym)
            if not base or base in self.STABLE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            out[base] = {"symbol": sym, "quote_volume": fnum(t.get("quoteVolume")), "last": fnum(t.get("lastPrice"))}
        return out

    def klines(self, symbol, limit=500) -> pd.DataFrame | None:
        k = self._get("/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
        if not k:
            return None
        df = pd.DataFrame(k, columns=["t", "Open", "High", "Low", "Close", "Volume", "ct", "qv", "n", "tb", "tq", "x"])
        df.index = pd.to_datetime(df["t"], unit="ms")
        return df[["Open", "High", "Low", "Close", "Volume"]].astype(float)

    def prices(self) -> dict[str, float]:
        return {t["symbol"]: fnum(t.get("price")) for t in (self._get("/ticker/price") or [])}
