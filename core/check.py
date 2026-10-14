"""`python run.py --check` — hit every live source once and show what came back."""
from __future__ import annotations

import time

from rich.table import Table

from .derivs import nse_chain, delta_chain
from .feeds import LiveFeeds
from .instruments import Underlying
from .sources import NSE, Delta, fnum


def run_check(cfg, con):
    t = Table(title="SENTINEL-6 · live source check", header_style="bold")
    for c in ("Source", "Status", "Sample"):
        t.add_column(c, overflow="fold")

    def row(name, fn):
        t0 = time.time()
        try:
            ok, sample = fn()
        except Exception as e:  # noqa
            ok, sample = False, f"{type(e).__name__}: {e}"
        t.add_row(name, ("[green]OK" if ok else "[red]FAIL") + f" [dim]{time.time() - t0:.1f}s", str(sample)[:160])

    def yf_spot():
        import yfinance as yf
        df = yf.download(["AAPL", "RELIANCE.NS", "BTC-USD", "^NSEI", "INR=X"], period="5d", progress=False,
                         group_by="ticker", auto_adjust=True)
        last = {}
        for s in ("AAPL", "RELIANCE.NS", "BTC-USD", "^NSEI", "INR=X"):
            try:
                last[s] = round(float(df[s]["Close"].dropna().iloc[-1]), 2)
            except Exception:  # noqa
                last[s] = None
        return all(v is not None for v in last.values()), last

    nse = NSE(cfg["http_timeout"])

    def nse_lots():
        lots = nse.lot_sizes()
        return bool(lots), f"{len(lots)} F&O underlyings · NIFTY={lots.get('NIFTY')} BANKNIFTY={lots.get('BANKNIFTY')} RELIANCE={lots.get('RELIANCE')}"

    def nse_idx():
        ch = nse_chain(nse, "NIFTY", True, 0, 45)
        if not ch:
            return False, "no chain (NSE blocked / market API changed)"
        m = ch.metrics()
        return True, f"spot {ch.spot} · exp {m['expiry']} · {len(ch.strikes)} strikes · PCR {m['pcr']} · max pain {m['max_pain']} · ATM IV {m['atm_iv']}"

    def nse_stk():
        ch = nse_chain(nse, "RELIANCE", False, 0, 60)
        if not ch:
            return False, "no chain"
        return True, f"spot {ch.spot} · exp {ch.expiry:%d-%b} · {len(ch.strikes)} strikes · ATM IV {ch.atm_iv():.3f}"

    dl = Delta(cfg["http_timeout"])

    def delta_perps():
        ts = dl.tickers("perpetual_futures")
        if not ts:
            return False, "no tickers"
        ts.sort(key=lambda x: fnum(x.get("turnover_usd")), reverse=True)
        top = ts[0]
        keys = ", ".join(sorted(top.keys())[:25])
        return True, f"{len(ts)} perps · top {top.get('symbol')} mark {top.get('mark_price')} funding {top.get('funding_rate')} · keys: {keys}"

    def delta_candles():
        df = dl.candles("BTCUSD", 30)
        return df is not None and len(df) > 5, (f"{len(df)} daily bars, last close {df['Close'].iloc[-1]}" if df is not None else "none")

    def delta_opts():
        ch = delta_chain(dl, "BTC", 0, 40)
        if not ch:
            return False, "no option chain"
        m = ch.metrics()
        return True, f"spot {ch.spot} · exp {m['expiry']} · {len(ch.strikes)} strikes · PCR {m['pcr']} · ATM IV {m['atm_iv']}"

    feeds = LiveFeeds(cfg)
    u = Underlying("us_spot:NVDA", "us_spot", "NVDA", "NVDA", "yf", "USD", 1, "Nvidia")

    def news():
        h = feeds.news(u)
        return bool(h), f"{len(h)} headlines · e.g. {h[0].title if h else ''}"

    def crowd():
        p = feeds.crowd(u)
        return bool(p), f"{len(p)} posts (StockTwits + Reddit)"

    with con.status("Checking sources…"):
        row("yfinance (US / IN / crypto spot, indices, USDINR)", yf_spot)
        row("NSE F&O lot sizes", nse_lots)
        row("NSE option chain · NIFTY", nse_idx)
        row("NSE option chain · RELIANCE", nse_stk)
        row("Delta India perpetuals", delta_perps)
        row("Delta India candles BTCUSD", delta_candles)
        row("Delta India options · BTC", delta_opts)
        row("News (Yahoo + Google News)", news)
        row("Crowd (StockTwits + Reddit)", crowd)
    con.print(t)
    con.print("[dim]Paste this table back to Claude if anything says FAIL.")
