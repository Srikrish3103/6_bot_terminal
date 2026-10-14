"""Universe: what each segment scans, where its candles come from, lot sizes."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .sources import NSE, Delta, fnum

log = logging.getLogger("universe")

SEGMENTS = ["us_spot", "in_spot", "crypto_spot", "in_fno", "crypto_fut", "crypto_opt"]
SEG_LABEL = {"us_spot": "US", "in_spot": "IN", "crypto_spot": "CRYPTO",
             "in_fno": "F&O", "crypto_fut": "C-FUT", "crypto_opt": "C-OPT"}
SEG_COLOR = {"us_spot": "#4fb3ff", "in_spot": "#ff9a1f", "crypto_spot": "#f7c548",
             "in_fno": "#ff6a3d", "crypto_fut": "#b36bff", "crypto_opt": "#3de0c4"}

INDEX_YF = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
            "MIDCPNIFTY": "NIFTY_MID_SELECT.NS", "NIFTYNXT50": "^NSMIDCP"}

# Used only when NSE's lot file can't be fetched (and in --demo). Approximate — live run uses NSE's file.
FNO_FALLBACK = {
    "NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65, "MIDCPNIFTY": 140,
    "RELIANCE": 500, "HDFCBANK": 550, "ICICIBANK": 700, "INFY": 400, "TCS": 175, "SBIN": 750,
    "AXISBANK": 625, "KOTAKBANK": 400, "LT": 175, "ITC": 1600, "BHARTIARTL": 475, "HINDUNILVR": 300,
    "BAJFINANCE": 750, "MARUTI": 50, "TATAMOTORS": 800, "TATASTEEL": 5500, "SUNPHARMA": 350,
    "M&M": 200, "ADANIENT": 300, "HCLTECH": 350, "WIPRO": 3000, "ONGC": 2250, "NTPC": 1500,
    "POWERGRID": 1900, "DLF": 825, "HAL": 150, "BEL": 1425, "VEDL": 1150, "COALINDIA": 1350,
}

# Fallback Delta perps: symbol -> (underlying, contract_value). Live run reads Delta's product list.
DELTA_PERP_FALLBACK = {
    "BTCUSD": ("BTC", 0.001), "ETHUSD": ("ETH", 0.01), "SOLUSD": ("SOL", 1), "XRPUSD": ("XRP", 1),
    "DOGEUSD": ("DOGE", 100), "BNBUSD": ("BNB", 0.1), "ADAUSD": ("ADA", 10), "AVAXUSD": ("AVAX", 1),
    "LINKUSD": ("LINK", 1), "LTCUSD": ("LTC", 0.1), "SUIUSD": ("SUI", 1), "DOTUSD": ("DOT", 1),
}
DELTA_OPT_CONTRACT = {"BTC": 0.001, "ETH": 0.01}


@dataclass
class Underlying:
    key: str                 # unique id e.g. "in_fno:NIFTY"
    segment: str
    symbol: str              # display / exchange symbol
    data_symbol: str         # candle source symbol
    source: str              # "yf" | "delta"
    ccy: str                 # quote currency of the instrument
    lot: float = 1.0         # units per lot / contract value
    name: str = ""           # for news / social search
    is_index: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def label(self):
        return SEG_LABEL[self.segment]


def _yf_ccy(sym):
    return "INR" if sym.endswith(".NS") or sym.endswith(".BO") or sym.startswith("^NS") else "USD"


def _base(sym):
    return sym.replace(".NS", "").replace(".BO", "").replace("-USD", "")


def build_universe(cfg: dict, root: Path, demo: bool = False) -> list[Underlying]:
    seg = cfg["segments"]
    aliases = cfg.get("aliases", {})
    out: list[Underlying] = []

    def name(s):
        return aliases.get(s, s)

    for sname in ("us_spot", "in_spot", "crypto_spot"):
        c = seg.get(sname, {})
        if not c.get("enabled"):
            continue
        for s in c["symbols"]:
            b = _base(s)
            out.append(Underlying(f"{sname}:{b}", sname, b, s, "yf", _yf_ccy(s), 1.0, name(b),
                                  meta={"fractional": c.get("fractional", False)}))

    # ---------------- India F&O
    c = seg.get("in_fno", {})
    if c.get("enabled"):
        lots = {} if demo else _cached(root / "data" / "fno_lots.json", lambda: NSE(cfg["http_timeout"]).lot_sizes())
        if not lots:
            if not demo:
                log.warning("NSE lot file unavailable — using fallback F&O list (%d names)", len(FNO_FALLBACK))
            lots = FNO_FALLBACK
        indices = set(c.get("indices", []))
        for s, lot in lots.items():
            is_idx = s in INDEX_YF
            if is_idx and s not in indices:
                continue
            if not is_idx and c.get("universe", "full") != "full" and s not in FNO_FALLBACK:
                continue
            if not is_idx and any(k in s for k in ("NIFTY", "SENSEX", "BANKEX")):
                continue          # other indices we have no yfinance symbol for
            ysym = INDEX_YF.get(s, f"{s}.NS")
            out.append(Underlying(f"in_fno:{s}", "in_fno", s, ysym, "yf", "INR", float(lot),
                                  name(s) if is_idx else f"{name(s)} share", is_index=is_idx))

    # ---------------- Crypto perpetuals (Delta India)
    c = seg.get("crypto_fut", {})
    if c.get("enabled"):
        perps = {} if demo else _delta_perps(cfg, c.get("scan_top_by_turnover", 40))
        if not perps:
            if not demo:
                log.warning("Delta perps unavailable — using fallback list")
            perps = {k: {"und": u, "cv": cv} for k, (u, cv) in DELTA_PERP_FALLBACK.items()}
        for sym, m in perps.items():
            out.append(Underlying(f"crypto_fut:{sym}", "crypto_fut", sym, sym, "delta", "USD", m["cv"],
                                  name(m["und"]) + " crypto", meta=m))

    # ---------------- Crypto options (Delta India) — scanned on the perp of the underlying
    c = seg.get("crypto_opt", {})
    if c.get("enabled"):
        for u in c.get("underlyings", ["BTC", "ETH"]):
            out.append(Underlying(f"crypto_opt:{u}", "crypto_opt", u, f"{u}USD", "delta", "USD",
                                  DELTA_OPT_CONTRACT.get(u, 1.0), name(u) + " crypto"))
    return out


def _cached(path: Path, fetch):
    """Daily file cache (NSE lot sizes change monthly at most)."""
    try:
        if path.exists():
            j = json.loads(path.read_text())
            if j.get("date") == str(date.today()) and j.get("data"):
                return j["data"]
    except Exception:  # noqa
        pass
    data = fetch()
    if data:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": str(date.today()), "data": data}))
    return data


def _delta_perps(cfg, top) -> dict:
    d = Delta(cfg["http_timeout"])
    ticks = d.tickers("perpetual_futures")
    if not ticks:
        return {}
    cv = {p.get("symbol"): fnum(p.get("contract_value"), 0) for p in d.products("perpetual_futures")}
    ticks.sort(key=lambda t: fnum(t.get("turnover_usd")), reverse=True)
    out = {}
    for t in ticks[:top]:
        sym = t.get("symbol")
        und = t.get("underlying_asset_symbol") or (t.get("underlying_asset") or {}).get("symbol") or sym.replace("USD", "").replace("USDT", "")
        c = cv.get(sym) or fnum(t.get("contract_value"), 0) or DELTA_PERP_FALLBACK.get(sym, (und, 1.0))[1]
        fr = fnum(t.get("funding_rate"), 0)
        out[sym] = {"und": und, "cv": c, "funding": fr, "oi_usd": fnum(t.get("oi_value_usd")),
                    "turnover_usd": fnum(t.get("turnover_usd")), "mark": fnum(t.get("mark_price"))}
    return out
