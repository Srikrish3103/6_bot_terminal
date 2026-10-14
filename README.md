# SENTINEL-6 — six-agent market desk (paper trading only)

Six agents scan six market segments every cycle and paper-trade the results. Nothing is ever sent to a broker or exchange.

## Segments

| Seg | Market | Data source | Paper instrument |
|---|---|---|---|
| `US` | US stocks (spot) | yfinance | Shares (fractional) |
| `IN` | NSE stocks (spot) | yfinance | Shares (whole) |
| `CRYPTO` | Crypto spot | yfinance | Coins (fractional) |
| `F&O` | **Full NSE F&O list** + NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY / NIFTYNXT50 | yfinance candles · NSE lot file + option chain | Options (futures if no chain) |
| `C-FUT` | Crypto perpetuals — top 40 by turnover | Delta Exchange India | Perpetual contracts |
| `C-OPT` | BTC / ETH options | Delta Exchange India | Options |

Spot segments are long-only (no overnight cash shorting). Derivative segments can go short.

## Agents

| # | Agent | Role | What it does |
|---|-------|------|--------------|
| 1 | **KESTREL** | Market scanner | Scores every underlying 0-100 (vol-normalised momentum, EMA50/200 trend, 20-bar range position, volume surge) and passes the top N **per segment** with a LONG/SHORT bias. |
| 2 | **PRISM** | Chart technician | EMA stack, RSI, MACD, up/down volume, support/resistance → `SETUP / WATCH / NO SETUP` + entry, ATR stop, 2.5R target on the underlying. |
| 3 | **HERALD** | News desk | Yahoo + Google News headlines → catalyst lexicon + VADER, recency-weighted; high-impact catalysts need ≥2 sources. |
| 4 | **PULSE** | Crowd + positioning | StockTwits / Reddit mood, mention velocity, hype filter — blended with **derivatives positioning**: option PCR, max pain, net put writing (F&O, C-OPT) and funding rate (C-FUT, contrarian). |
| 5 | **AEGIS** | Risk officer | Sizes in whole lots / contracts so loss at stop ≤ 1% equity. Checks position size, max loss, correlation, segment + gross exposure, R:R (credit spreads: POP + EV). **Any FAIL = veto.** |
| 6 | **ARBITER** | Final call | Weighted composite → `TRADE / WAIT / REJECT / HOLD`. |

## Option structure (F&O and C-OPT)

PRISM's underlying plan is turned into an option trade by comparing ATM implied vol with 20-day realised vol:

| IV / HV | Structure | Strikes |
|---|---|---|
| ≤ 1.10 (cheap) | **Long call / put** | ATM, walks up to 3 strikes OTM to fit the 1% risk budget |
| 1.10 – 1.50 | **Debit spread** (bull call / bear put) | Long ATM, short strike nearest the target that fits the budget |
| > 1.50 (rich) | **Credit spread** (bull put / bear call) | Short strike at the stop level; needs POP ≥ 60% and positive EV using realised vol |

Every structure has a known max loss. If one lot still risks more than the budget, AEGIS vetoes it. Positions exit when the underlying hits stop/target or at expiry; option legs are marked with Black-Scholes, anchored to the premium paid.

## Setup (Windows)
```bat
cd 6_bot_terminal
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run.py --check          :: test NSE, Delta, yfinance, news feeds
python run.py --web --demo     :: dashboard on offline demo data
python run.py --web            :: dashboard on live data  -> http://localhost:8765
```
Or double-click `run.bat` — it installs everything on first run and opens the dashboard in your browser.

## Web dashboard (`--web`)
Opens `http://localhost:8765` (local only, nothing is exposed to the internet). It refreshes every 1.5 s:

| Section | Shows |
|---|---|
| Header | Cycle status + countdown, equity, open / realized P&L, **Run cycle now** button |
| Agents | What each of the 6 agents is doing right now |
| Funnel | How many instruments survive each stage (universe → scored → shortlisted → setup → news/crowd agree → risk approved → TRADE), overall and per segment |
| Suggested trades | One card per TRADE / HOLD: exact order lines (e.g. `BUY 1 lot (500) RELIANCE 1,980 CE 05 Nov @ ₹15.94`), exit rule, risk ₹, reward R, agent score bars, and an expandable "why" with every agent report and AEGIS check |
| On watch / Rejected | Setups waiting for confirmation, and what the desk rejected, with reasons |
| Paper book | Open positions with live P&L and distance to stop, closed trades |
| Watchlist + event log | KESTREL ranking with live prices, running agent log |

Filter the cards by segment with the tabs (US / IN / CRYPTO / F&O / C-FUT / C-OPT).

## Commands
| Command | Effect |
|---|---|
| `python run.py --web` | **web dashboard** (recommended): `--port 8765`, `--no-browser` |
| `python run.py --live` | live terminal: quotes every 30 s, full cycle every 15 min, live stop/target/expiry exits |
| `python run.py --live --demo` | same on offline synthetic data (2 s ticks, 2 min cycles) |
| `--cycle M --tick S` | live: change cycle minutes / quote seconds |
| `python run.py` | one cycle → console + `reports/latest.html` |
| `python run.py --check` | test every live data source and show sample fields |
| `--no-exec` | analyse only, don't open paper positions |
| `--loop N` | repeat every N minutes |
| `--book` | show paper positions & closed P&L |
| `--reset` | wipe paper book (`--demo --reset` for the demo book) |
| `-v` | debug logs (see which feeds failed) |

## Config highlights (`config.yaml`)
- `account.base_currency` — `INR` (default, ₹10,00,000) or `USD`
- `segments.*.enabled / top_n` — switch segments on/off, picks per segment
- `segments.in_fno.instrument` — `options` or `futures`
- `options.iv_cheap / iv_rich / min_pop_credit` — structure selection
- `aegis.risk_per_trade_pct`, `max_segment_pct`, `min_rr` — risk limits

## Files
```
agents/            kestrel, prism, herald, pulse, aegis, arbiter
core/instruments.py  universe per segment, F&O lot sizes, Delta perps
core/sources.py      NSE + Delta Exchange India HTTP clients
core/derivs.py       option chains, PCR/max pain, Black-Scholes, structurer
core/data.py         candles (yfinance + Delta), FX to base currency
core/feeds.py        news, crowd, derivatives feeds (+ offline demo)
core/paper.py        multi-leg SQLite paper book
core/desk.py         pipeline orchestration
core/view.py         console + HTML report
core/live.py         live engine + full-screen terminal
core/web.py          local web server for the dashboard
core/suggest.py      funnel counts + plain-English trade instructions
web/dashboard.html   the dashboard page
core/check.py        --check source tester
data/paper_v2.db     your paper book (not committed)
```

## Notes
- NSE blocks heavy scraping: the option chain is only fetched for the top F&O picks each cycle. If NSE refuses, F&O falls back to futures for that pick.
- Delta funding is read as % per 8h; positive funding (crowded longs) counts against longs.
- F&O lot sizes come from NSE's `fo_mktlots.csv` (cached daily in `data/fno_lots.json`); a small fallback list is used if it can't be fetched.
- Research tool only. Not financial advice.
