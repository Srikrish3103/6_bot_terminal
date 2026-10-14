"""Plain pandas indicators (no TA-lib needed)."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def macd(s: pd.Series, f=12, sl=26, sig=9):
    line = ema(s, f) - ema(s, sl)
    signal = ema(line, sig)
    return line, signal, line - signal


def volume_ratio(df: pd.DataFrame, short=5, long=50) -> float:
    v = df["Volume"].astype(float)
    if v.tail(long).sum() <= 0:          # forex has no volume
        return 1.0
    return float(v.tail(short).mean() / max(v.tail(long).mean(), 1e-9))


def kst(s: pd.Series, roc=(10, 15, 20, 30), sma=(10, 10, 10, 15), sig=9):
    """Know Sure Thing (Pring), identical to TradingView's default KST.
    KST = Σ w_i · SMA(ROC(close, roc_i), sma_i), weights 1..4; signal = SMA(KST, sig)."""
    line = sum((i + 1) * (100 * s.pct_change(r)).rolling(m).mean() for i, (r, m) in enumerate(zip(roc, sma)))
    return line, line.rolling(sig).mean()
