"""Candles for every underlying (yfinance + Delta), FX conversion to the base currency."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd


log = logging.getLogger("data")

FX_FALLBACK = {"USD": 1.0, "INR": 88.0}       # units per 1 USD
CCY_SIGN = {"INR": "₹", "USD": "$"}


def fx_rates(live: bool = True) -> dict[str, float]:
    rates = dict(FX_FALLBACK)
    if live:
        try:
            import yfinance as yf
            c = yf.download("INR=X", period="5d", interval="1d", progress=False, auto_adjust=True)["Close"].dropna()
            if len(c):
                rates["INR"] = float(np.asarray(c).ravel()[-1])
        except Exception as e:  # noqa
            log.debug("USDINR fetch failed: %s", e)
    return rates


def convert(amount: float, ccy: str, base: str, rates: dict) -> float:
    if ccy == base:
        return amount
    return amount / rates.get(ccy, 1.0) * rates.get(base, 1.0)


def hist_vol(df: pd.DataFrame, n: int = 20, crypto: bool = False) -> float:
    r = np.log(df["Close"]).diff().dropna().tail(n)
    return float(r.std() * np.sqrt(365 if crypto else 252)) if len(r) > 5 else 0.0


# ------------------------------------------------------------------ demo data
BASE_PX = {"us_spot": 250, "in_spot": 1500, "crypto_spot": 2000, "in_fno": 1800,
           "crypto_fut": 3000, "crypto_opt": 60000}
VOL = {"us_spot": 0.02, "in_spot": 0.015, "crypto_spot": 0.035, "in_fno": 0.016,
       "crypto_fut": 0.035, "crypto_opt": 0.03}
DEMO_LEVELS = {"in_fno:NIFTY": 25000, "in_fno:BANKNIFTY": 56000, "in_fno:FINNIFTY": 26500,
               "in_fno:MIDCPNIFTY": 13000, "crypto_opt:BTC": 110000, "crypto_opt:ETH": 4000,
               "crypto_fut:BTCUSD": 110000, "crypto_fut:ETHUSD": 4000}


def synthetic_candles(universe, seed: int = 7, bars: int = 400) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=bars)
    out = {}
    for u in universe:
        s = u.segment
        vol = VOL[s] * (0.6 if u.is_index else 1.0)
        drift = rng.choice([-0.0015, 0.0, 0.0012, 0.0025])
        r = rng.normal(drift, vol, bars)
        target = DEMO_LEVELS.get(u.key, BASE_PX[s] * (0.3 + 1.5 * rng.random()))
        close = np.exp(np.cumsum(r))
        close = close / close[-1] * target
        spread = np.abs(rng.normal(0, vol, bars)) * close
        op = np.r_[close[0], close[:-1]]
        volu = rng.lognormal(0, 0.3, bars) * 1e6 * (1 + np.clip(r, 0, None) * 30)
        out[u.key] = pd.DataFrame({"Open": op, "High": np.maximum(close + spread, op),
                                   "Low": np.minimum(close - spread, op), "Close": close, "Volume": volu}, index=idx)
    return out
