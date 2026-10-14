"""Market data (yfinance) + FX conversion helpers."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger("data")

FX_FALLBACK = {"INR": 88.0, "JPY": 150.0, "CAD": 1.38, "EUR": 0.92, "GBP": 0.78, "AUD": 1.52}


def asset_class(sym: str) -> str:
    if sym.endswith("=X"):
        return "forex"
    if sym.endswith("-USD") or sym.endswith("-USDT"):
        return "crypto"
    if sym.endswith(".NS") or sym.endswith(".BO"):
        return "stocks_in"
    return "stocks_us"


def quote_ccy(sym: str) -> str:
    c = asset_class(sym)
    if c == "forex":
        return sym.replace("=X", "")[3:6]
    if c == "stocks_in":
        return "INR"
    return "USD"


def fetch_ohlcv(symbols: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    try:
        raw = yf.download(symbols, period=period, interval="1d", group_by="ticker",
                          auto_adjust=True, threads=True, progress=False)
    except Exception as e:  # noqa
        log.error("yfinance download failed: %s", e)
        return out
    for s in symbols:
        try:
            df = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            df["Volume"] = df["Volume"].fillna(0)
            if len(df) >= 120:
                out[s] = df
            else:
                log.warning("%s: only %d bars, skipped", s, len(df))
        except Exception as e:  # noqa
            log.warning("%s: no data (%s)", s, e)
    return out


def fx_rates(data: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Units of CCY per 1 USD."""
    rates = dict(FX_FALLBACK)
    rates["USD"] = 1.0
    for ccy in list(FX_FALLBACK):
        usd_x, x_usd = f"USD{ccy}=X", f"{ccy}USD=X"
        if usd_x in data:
            rates[ccy] = float(data[usd_x]["Close"].iloc[-1])
        elif x_usd in data:
            rates[ccy] = 1 / float(data[x_usd]["Close"].iloc[-1])
    return rates


def to_usd(amount: float, sym: str, rates: dict[str, float]) -> float:
    return amount / rates.get(quote_ccy(sym), 1.0)


# ---------------------------------------------------------------- demo data
def synthetic_ohlcv(symbols: list[str], seed: int = 7, bars: int = 400) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=bars)
    base = {"forex": 1.2, "crypto": 3000, "stocks_in": 1500, "stocks_us": 200}
    vol = {"forex": 0.005, "crypto": 0.035, "stocks_in": 0.015, "stocks_us": 0.02}
    out = {}
    for i, s in enumerate(symbols):
        c = asset_class(s)
        drift = rng.choice([-0.0015, 0.0, 0.0012, 0.0025]) * (0.3 if c == "forex" else 1)
        r = rng.normal(drift, vol[c], bars)
        close = base[c] * (1 + 0.3 * rng.random()) * np.exp(np.cumsum(r))
        spread = np.abs(rng.normal(0, vol[c], bars)) * close
        high, low = close + spread, close - spread
        op = np.r_[close[0], close[:-1]]
        v = 0 if c == "forex" else 1e6
        volu = (rng.lognormal(0, 0.3, bars) * v) * (1 + np.clip(r, 0, None) * 30)
        out[s] = pd.DataFrame({"Open": op, "High": np.maximum(high, op), "Low": np.minimum(low, op),
                               "Close": close, "Volume": volu}, index=idx)
    return out
