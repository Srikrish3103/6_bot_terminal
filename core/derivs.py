"""Option chains (NSE + Delta), positioning metrics, Black-Scholes, and the trade structurer.

The structurer turns PRISM's underlying plan (direction, entry, stop, target) into the
actual paper instrument for each segment:

  spot            -> SPOT
  crypto_fut      -> FUTURE (perpetual)
  in_fno          -> LONG_OPTION / DEBIT_SPREAD / CREDIT_SPREAD   (FUTURE if no chain / instrument: futures)
  crypto_opt      -> LONG_OPTION / DEBIT_SPREAD / CREDIT_SPREAD

Options choice (IV / realised vol of the underlying):
  ratio <= iv_cheap          -> buy ATM call/put           (premium is cheap)
  iv_cheap < ratio <= iv_rich -> debit vertical to target   (cheapen an expensive long)
  ratio > iv_rich            -> credit vertical at the stop (sell rich premium, defined risk)
Every structure has a known max loss, so AEGIS can size it in whole lots.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .sources import NSE, Delta, fnum, parse_delta_option_symbol, now_utc, days_to


# ====================================================================== Black-Scholes
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, iv, kind, r=0.0):
    intrinsic = max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    if T <= 0 or iv <= 0 or S <= 0:
        return intrinsic
    sd = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / sd
    d2 = d1 - sd
    if kind == "C":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def prob_itm(S, K, T, iv, kind, r=0.0):
    if T <= 0 or iv <= 0:
        return 1.0 if (S > K if kind == "C" else S < K) else 0.0
    d2 = (math.log(S / K) + (r - 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    return _ncdf(d2) if kind == "C" else _ncdf(-d2)


# ====================================================================== chain model
@dataclass
class Quote:
    price: float
    iv: float
    oi: float = 0.0
    chg_oi: float = 0.0
    symbol: str = ""


@dataclass
class Chain:
    underlying: str
    spot: float
    expiry: datetime
    calls: dict[float, Quote] = field(default_factory=dict)
    puts: dict[float, Quote] = field(default_factory=dict)
    source: str = ""
    expiries: list = field(default_factory=list)

    @property
    def strikes(self):
        return sorted(set(self.calls) | set(self.puts))

    @property
    def dte(self):
        return days_to(self.expiry)

    def atm(self):
        ks = self.strikes
        return min(ks, key=lambda k: abs(k - self.spot)) if ks else None

    def atm_iv(self):
        k = self.atm()
        ivs = [q.iv for q in (self.calls.get(k), self.puts.get(k)) if q and q.iv > 0]
        if ivs:
            return sum(ivs) / len(ivs)
        allv = [q.iv for q in list(self.calls.values()) + list(self.puts.values()) if q.iv > 0]
        return sorted(allv)[len(allv) // 2] if allv else 0.0

    def metrics(self):
        coi = sum(q.oi for q in self.calls.values())
        poi = sum(q.oi for q in self.puts.values())
        pcr = poi / coi if coi > 0 else None
        ks = self.strikes
        max_pain = None
        if ks and (coi + poi) > 0:
            def pain(x):
                return (sum(q.oi * max(0, x - k) for k, q in self.calls.items())
                        + sum(q.oi * max(0, k - x) for k, q in self.puts.items()))
            max_pain = min(ks, key=pain)
        above = {k: q.oi for k, q in self.calls.items() if k >= self.spot}
        below = {k: q.oi for k, q in self.puts.items() if k <= self.spot}
        chg = sum(q.chg_oi for q in self.puts.values()) - sum(q.chg_oi for q in self.calls.values())
        return {"pcr": round(pcr, 2) if pcr else None, "max_pain": max_pain, "atm_iv": round(self.atm_iv(), 4),
                "call_wall": max(above, key=above.get) if above else None,
                "put_wall": max(below, key=below.get) if below else None,
                "net_put_writing": chg, "dte": round(self.dte, 1), "expiry": self.expiry.strftime("%d-%b-%Y")}


def _pick_expiry(exps, min_dte, max_dte):
    ok = [e for e in exps if min_dte <= days_to(e) <= max_dte]
    if ok:
        return ok[0]
    later = [e for e in exps if days_to(e) >= min_dte]
    return later[0] if later else (exps[-1] if exps else None)


# ---------------------------------------------------------------------- NSE
def _nse_quote(d):
    if not d:
        return None
    bid = fnum(d.get("bidprice", d.get("bidPrice", d.get("buyPrice1"))))
    ask = fnum(d.get("askPrice", d.get("askprice", d.get("sellPrice1"))))
    last = fnum(d.get("lastPrice"))
    px = (bid + ask) / 2 if bid > 0 and ask > 0 and ask >= bid else last
    if px <= 0:
        return None
    return Quote(px, fnum(d.get("impliedVolatility")) / 100, fnum(d.get("openInterest")),
                 fnum(d.get("changeinOpenInterest")), d.get("identifier", ""))


def nse_chain(nse: NSE, symbol, is_index, min_dte, max_dte) -> Chain | None:
    exps = nse.expiries(symbol)
    exp = _pick_expiry(exps, min_dte, max_dte) if exps else None
    rows, spot, legacy_exps = nse.chain_rows(symbol, is_index, exp)
    if not exp and legacy_exps:
        exp = _pick_expiry(legacy_exps, min_dte, max_dte)
        rows = [r for r in rows if r.get("expiryDate") == exp.strftime("%d-%b-%Y")]
    if not rows or not exp:
        return None
    ch = Chain(symbol, spot, exp, source="NSE", expiries=exps or legacy_exps or [])
    for r in rows:
        k = fnum(r.get("strikePrice"))
        c, p = _nse_quote(r.get("CE")), _nse_quote(r.get("PE"))
        if c:
            ch.calls[k] = c
        if p:
            ch.puts[k] = p
        if not ch.spot:
            ch.spot = fnum((r.get("CE") or r.get("PE") or {}).get("underlyingValue"))
    return ch if ch.strikes and ch.spot else None


# ---------------------------------------------------------------------- Delta
def delta_chain(delta: Delta, und, min_dte, max_dte) -> Chain | None:
    ticks = delta.tickers("call_options,put_options", und)
    parsed = []
    for t in ticks:
        p = parse_delta_option_symbol(t.get("symbol", ""))
        if p:
            parsed.append((p, t))
    if not parsed:
        return None
    exps = sorted({p[3] for p, _ in parsed})
    exp = _pick_expiry(exps, min_dte, max_dte)
    spot = 0.0
    ch = Chain(und, 0.0, exp, source="Delta", expiries=exps)
    for (kind, _, strike, e), t in parsed:
        if e != exp:
            continue
        q = t.get("quotes") or {}
        bid, ask = fnum(q.get("best_bid")), fnum(q.get("best_ask"))
        px = (bid + ask) / 2 if bid > 0 and ask > 0 else fnum(t.get("mark_price"))
        iv = fnum(q.get("mark_iv"))
        iv = iv / 100 if iv > 3 else iv
        if px <= 0:
            continue
        quote = Quote(px, iv, fnum(t.get("oi")), 0.0, t.get("symbol", ""))
        (ch.calls if kind == "C" else ch.puts)[fnum(t.get("strike_price"), strike)] = quote
        spot = spot or fnum(t.get("spot_price"))
    ch.spot = spot
    return ch if ch.strikes and ch.spot else None


# ---------------------------------------------------------------------- demo
def _nice_step(px):
    raw = px * 0.01
    for s in (0.5, 1, 2.5, 5, 10, 20, 25, 50, 100, 250, 500, 1000, 2000):
        if s >= raw:
            return s
    return 5000


def synthetic_chain(u, spot, hv, seed=0, crypto=False) -> Chain:
    rng = random.Random(f"{u.key}-{seed}")
    step = 50 if u.symbol == "NIFTY" else 100 if u.symbol in ("BANKNIFTY", "FINNIFTY") else _nice_step(spot)
    if crypto:
        dte = 7 - now_utc().weekday() % 7 + 4
    elif u.is_index:
        dte = 6
    else:
        dte = 22
    exp = (now_utc() + timedelta(days=dte)).replace(hour=10, minute=0, second=0, microsecond=0)
    iv_atm = max(0.1, hv) * rng.uniform(0.85, 1.9)
    ch = Chain(u.symbol, spot, exp, source="demo", expiries=[exp])
    T = dte / 365
    atm = round(spot / step) * step
    for i in range(-15, 16):
        k = atm + i * step
        if k <= 0:
            continue
        m = math.log(k / spot)
        iv = iv_atm * (1 + 2.5 * m * m - 0.4 * m)
        base_oi = rng.uniform(0.3, 1.0) * math.exp(-abs(i) / 6) * 1e5
        ch.calls[k] = Quote(round(bs_price(spot, k, T, iv, "C"), 2), iv, base_oi * (1.4 if i > 2 else 1),
                            rng.gauss(0, base_oi * 0.1))
        ch.puts[k] = Quote(round(bs_price(spot, k, T, iv, "P"), 2), iv, base_oi * (1.4 if i < -2 else 1) * rng.uniform(0.6, 1.6),
                           rng.gauss(0, base_oi * 0.1))
    return ch


# ====================================================================== structurer
def _fmt_k(k):
    return f"{k:,.0f}" if k >= 100 else f"{k:g}"


def _leg(kind, strike, expiry, side, price, iv, T, S, r):
    adj = price - bs_price(S, strike, T, iv, kind, r) if kind in "CP" else 0.0
    return {"kind": kind, "strike": strike, "expiry": expiry.isoformat() if expiry else None, "side": side,
            "price": price, "iv": iv, "adj": adj, "t_entry": T}


def linear_structure(u, plan, kind="SPOT"):
    d = 1 if plan["direction"] == "LONG" else -1
    e, s, t = plan["entry"], plan["stop"], plan["target"]
    risk = abs(e - s)
    mult = u.lot if kind == "FUTURE" else 1.0
    integer = kind == "FUTURE" or not u.meta.get("fractional", False)
    nm = ("BUY" if d == 1 else "SHORT") if kind == "SPOT" else ("LONG FUT" if d == 1 else "SHORT FUT")
    label = u.symbol + (f" PERP ({u.lot:g}/contract)" if u.segment == "crypto_fut" else
                        f" FUT ({u.lot:g}/lot)" if kind == "FUTURE" else "")
    return {"type": kind, "name": nm, "label": label,
            "legs": [{"kind": "S" if kind == "SPOT" else "F", "strike": None, "expiry": None, "side": d,
                      "price": e, "iv": 0, "adj": 0, "t_entry": 0}],
            "mult": mult, "entry_value": d * e, "max_loss": risk, "max_gain": abs(t - e),
            "notional": e, "rr": abs(t - e) / max(risk, 1e-12), "pop": None, "iv_hv": None,
            "expiry": None, "dte": None, "integer": integer, "ccy": u.ccy}


def option_structure(u, plan, ch: Chain, hv, ocfg, r=0.0, budget=None):
    """budget: max loss allowed for ONE lot, in the instrument currency (strikes are fitted to it)."""
    d = 1 if plan["direction"] == "LONG" else -1
    S, T = ch.spot, max(ch.dte, 0.05) / 365
    stop, target = plan["stop"], plan["target"]
    # re-anchor PRISM levels (daily close) to the live chain spot
    scale = S / plan["entry"] if plan["entry"] else 1.0
    stop, target = stop * scale, target * scale
    ks = ch.strikes
    atm = ch.atm()
    iv_atm = ch.atm_iv() or hv or 0.3
    ratio = iv_atm / hv if hv > 0 else 1.0
    cp = "C" if d == 1 else "P"
    side_book = ch.calls if cp == "C" else ch.puts
    india = u.segment == "in_fno"
    tag = {"C": "CE", "P": "PE"} if india else {"C": "C", "P": "P"}
    exp_s = ch.expiry.strftime("%d-%b")
    cap = (budget / u.lot) if budget else float("inf")          # max loss per underlying unit

    def q(book, k):
        x = book.get(k)
        return x if x and x.price > 0 else None

    def long_opt():
        otm = sorted([k for k in ks if (k >= atm if d == 1 else k <= atm) and q(side_book, k)],
                     key=lambda k: abs(k - atm))[:4]            # ATM .. 3 strikes OTM
        if not otm:
            return None
        k = next((x for x in otm if q(side_book, x).price <= cap), otm[0])
        qq = q(side_book, k)
        iv = qq.iv or iv_atm
        gain = bs_price(target, k, T * 0.25, iv, cp, r) - qq.price   # target hit with 1/4 of time left
        return {"type": "LONG_OPTION", "name": f"LONG {'CALL' if cp == 'C' else 'PUT'}",
                "label": f"{u.symbol} {_fmt_k(k)} {tag[cp]} {exp_s}",
                "legs": [_leg(cp, k, ch.expiry, +1, qq.price, iv, T, S, r)],
                "entry_value": qq.price, "max_loss": qq.price, "max_gain": gain, "notional": qq.price,
                "rr": gain / qq.price, "pop": prob_itm(S, k + d * qq.price, T, iv, cp, r),
                "breakeven": k + d * qq.price}

    def debit():
        q1 = q(side_book, atm)
        far = [k for k in ks if (k > atm if d == 1 else k < atm) and q(side_book, k)]
        if not (q1 and far):
            return None
        cands = []
        for k in far:
            dbk = q1.price - q(side_book, k).price
            if 0 < dbk < abs(k - atm):
                cands.append((k, dbk))
        if not cands:
            return None
        fit = [c for c in cands if c[1] <= cap]
        k2, db = min(fit or cands, key=lambda c: abs(c[0] - target))   # nearest the target that fits
        q2 = q(side_book, k2)
        width = abs(k2 - atm)
        nm = "BULL CALL SPREAD" if d == 1 else "BEAR PUT SPREAD"
        return {"type": "DEBIT_SPREAD", "name": nm,
                "label": f"{u.symbol} {_fmt_k(atm)}/{_fmt_k(k2)} {tag[cp]} {exp_s}",
                "legs": [_leg(cp, atm, ch.expiry, +1, q1.price, q1.iv or iv_atm, T, S, r),
                         _leg(cp, k2, ch.expiry, -1, q2.price, q2.iv or iv_atm, T, S, r)],
                "entry_value": db, "max_loss": db, "max_gain": width - db, "notional": db,
                "rr": (width - db) / db, "pop": prob_itm(S, atm + d * db, T, q1.iv or iv_atm, cp, r),
                "breakeven": atm + d * db}

    def credit():
        sk = "P" if d == 1 else "C"                       # bull -> sell puts, bear -> sell calls
        book = ch.puts if sk == "P" else ch.calls
        otm = [k for k in ks if (k < S if d == 1 else k > S) and q(book, k)]
        if not otm:
            return None
        k1 = min(otm, key=lambda k: abs(k - stop))      # short strike at the invalidation level
        width_t = max(abs(S - stop) * 0.5, 1e-9)
        wings = [k for k in ks if (k < k1 if d == 1 else k > k1) and q(book, k)]
        if not wings:
            return None
        q1 = q(book, k1)
        cands = [(k, q1.price - q(book, k).price) for k in wings]
        cands = [(k, c) for k, c in cands if 0 < c < abs(k1 - k)]
        if not cands:
            return None
        fit = [(k, c) for k, c in cands if abs(k1 - k) - c <= cap]
        k2, cr = min(fit or cands, key=lambda kc: abs(abs(k1 - kc[0]) - width_t))
        q2 = q(book, k2)
        width = abs(k1 - k2)
        iv1 = q1.iv or iv_atm
        # edge of selling rich vol: judge the odds with REALISED vol, not the (inflated) implied vol
        pop = 1 - prob_itm(S, k1, T, hv if hv > 0 else iv1, sk, r)
        ev = pop * cr - (1 - pop) * (width - cr)
        nm = "BULL PUT SPREAD" if d == 1 else "BEAR CALL SPREAD"
        tg = {"C": "CE", "P": "PE"} if india else {"C": "C", "P": "P"}
        return {"type": "CREDIT_SPREAD", "name": nm,
                "label": f"{u.symbol} {_fmt_k(k1)}/{_fmt_k(k2)} {tg[sk]} {exp_s}",
                "legs": [_leg(sk, k1, ch.expiry, -1, q1.price, iv1, T, S, r),
                         _leg(sk, k2, ch.expiry, +1, q2.price, q2.iv or iv_atm, T, S, r)],
                "entry_value": -cr, "max_loss": width - cr, "max_gain": cr, "notional": width - cr,
                "rr": cr / (width - cr), "pop": pop, "ev": ev, "breakeven": k1 - d * cr}

    if ratio <= ocfg["iv_cheap"]:
        order = [long_opt, debit, credit]
    elif ratio <= ocfg["iv_rich"]:
        order = [debit, long_opt, credit]
    else:
        order = [credit, debit, long_opt]
    for fn in order:
        st = fn()
        if st:
            st.update({"mult": u.lot, "iv_hv": round(ratio, 2), "atm_iv": round(iv_atm, 4),
                       "expiry": ch.expiry.isoformat(), "dte": round(ch.dte, 1), "integer": True,
                       "ccy": u.ccy, "und_stop": stop, "und_target": target, "und_entry": S,
                       "chosen_by": "IV/HV {:.2f} → {}".format(ratio, "cheap" if ratio <= ocfg["iv_cheap"]
                                                                else "rich" if ratio > ocfg["iv_rich"] else "mid")})
            return st
    return None


# ====================================================================== marking
def leg_value(leg, S, now=None, r=0.0):
    k = leg["kind"]
    if k in ("S", "F"):
        return S
    now = now or now_utc()
    exp = datetime.fromisoformat(leg["expiry"])
    T = max(0.0, (exp - now) / timedelta(days=365))
    v = bs_price(S, leg["strike"], T, leg["iv"], k, r)
    if leg.get("t_entry"):
        v += leg.get("adj", 0.0) * min(1.0, T / leg["t_entry"])
    return max(v, 0.0)


def structure_value(legs, S, now=None, r=0.0):
    """Per-underlying-unit signed value (same convention as entry_value)."""
    return sum(l["side"] * leg_value(l, S, now, r) for l in legs)


def first_expiry(legs):
    e = [datetime.fromisoformat(l["expiry"]) for l in legs if l.get("expiry")]
    return min(e) if e else None
