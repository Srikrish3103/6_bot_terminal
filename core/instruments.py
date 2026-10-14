"""Universe: what each segment scans, where its candles come from, lot sizes."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
