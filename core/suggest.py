"""Turn desk results into (a) a stage-by-stage funnel and (b) plain-English trade instructions."""
from __future__ import annotations

from datetime import datetime

from .data import CCY_SIGN
from .instruments import SEGMENTS

STAGES = [
    ("universe", "Universe", "Every instrument the desk watches"),
    ("data", "Has data", "Price history downloaded"),
    ("scored", "Scored ≥ min", "KESTREL score above the floor"),
    ("shortlist", "Shortlisted", "Top picks per segment"),
    ("setup", "Chart setup", "PRISM confirmed a SETUP"),
    ("agree", "News & crowd agree", "HERALD / PULSE not against the trade"),
    ("approved", "Risk approved", "AEGIS sized it and passed all checks"),
    ("trade", "TRADE", "ARBITER consensus — suggested trades"),
]


def funnel(universe, data, scan_rows, results, min_score, conflict_thr):
    segs = {s: {k: 0 for k, _, _ in STAGES} for s in SEGMENTS}
    for u in universe:
        segs[u.segment]["universe"] += 1
        if u.key in data:
            segs[u.segment]["data"] += 1
    for r in scan_rows:
        if r["score"] >= min_score:
            segs[r["segment"]]["scored"] += 1
    for res in results:
        s, rep = segs[res["segment"]], res["reports"]
        s["shortlist"] += 1
        if rep["prism"]["verdict"] != "SETUP":
            continue
        s["setup"] += 1
        al = rep["arbiter"]["details"].get("aligned", {})
        if any(al.get(k, 0) < conflict_thr and rep[k]["confidence"] != "LOW" for k in ("herald", "pulse")):
            continue
        s["agree"] += 1
        if rep["aegis"]["verdict"] != "APPROVED":
            continue
        s["approved"] += 1
        if rep["arbiter"]["verdict"] == "TRADE":
            s["trade"] += 1
    total = {k: sum(segs[s][k] for s in SEGMENTS) for k, _, _ in STAGES}
    return {"stages": [{"key": k, "label": l, "help": h, "count": total[k]} for k, l, h in STAGES],
            "by_segment": {s: segs[s] for s in SEGMENTS if segs[s]["universe"]}}


def _px(x, ccy):
    sign = CCY_SIGN.get(ccy, "")
    if x is None:
        return "-"
    return f"{sign}{x:,.2f}" if abs(x) >= 1 else f"{sign}{x:,.4f}"


def _units(n, integer):
    return f"{n:,.0f}" if integer or float(n).is_integer() else f"{n:,.4f}"


def action(res, base, rates):
    """Plain-English instruction for one desk result (any decision)."""
    st = res.get("structure")
    rep = res["reports"]
    ag = rep["aegis"]["details"]
    sym, seg, ccy = res["symbol"], res["segment"], (st or {}).get("ccy", "INR")
    units = ag.get("units") or 0
    venue = {"us_spot": "US stock (INDmoney)", "in_spot": "NSE cash", "crypto_spot": "crypto spot",
             "in_fno": "NSE F&O", "crypto_fut": "Delta India perpetual", "crypto_opt": "Delta India options"}[seg]
    if not st:
        return {"headline": f"No tradable structure for {sym}", "legs": [], "exit": "", "risk": None,
                "venue": venue, "units": 0}
    integer = st.get("integer", True)
    sized = units > 0
    units = units or 1                       # rejected picks: show what ONE lot / unit would look like
    legs = []
    if st["type"] == "SPOT":
        verb = "BUY" if res["direction"] == "LONG" else "SELL"
        legs.append(f"{verb} {_units(units, integer)} {'shares' if seg != 'crypto_spot' else sym} of {sym} at ~{_px(st['legs'][0]['price'], ccy)}")
        headline = f"{verb} {sym}"
    elif st["type"] == "FUTURE":
        verb = "BUY (long)" if res["direction"] == "LONG" else "SELL (short)"
        per = "contract" if seg == "crypto_fut" else "lot"
        legs.append(f"{verb} {_units(units, True)} {per}{'s' if units != 1 else ''} {st['label']} at ~{_px(st['legs'][0]['price'], ccy)}")
        headline = f"{'LONG' if res['direction'] == 'LONG' else 'SHORT'} {sym} futures"
    else:
        exp = datetime.fromisoformat(st["expiry"]).strftime("%d %b") if st.get("expiry") else ""
        tag = {"C": "CE", "P": "PE"} if seg == "in_fno" else {"C": "Call", "P": "Put"}
        lot_word = f"lot{'s' if units != 1 else ''} ({st['mult']:g} per lot)" if seg == "in_fno" else \
                   f"contract{'s' if units != 1 else ''} ({st['mult']:g} {sym} each)"
        for l in st["legs"]:
            legs.append(f"{'BUY' if l['side'] > 0 else 'SELL'} {_units(units, True)} {lot_word} {sym} {l['strike']:,.0f} "
                        f"{tag[l['kind']]} {exp} @ ~{_px(l['price'], ccy)}")
        net = st["entry_value"]
        if st["type"] == "CREDIT_SPREAD":
            legs.append(f"Net credit received ≈ {_px(-net * st['mult'] * units, ccy)}")
        else:
            legs.append(f"Net premium paid ≈ {_px(net * st['mult'] * units, ccy)}")
        headline = f"{st['name']} on {sym}"
    risk_base = ag.get("loss_at_stop_base")
    dirw = "falls below" if res["direction"] == "LONG" else "rises above"
    tw = "reaches" if res["direction"] == "LONG" else "drops to"
    exit_txt = (f"Exit if {sym} {dirw} {_px(st['und_stop'], ccy if st['type'] != 'SPOT' else ccy)} (stop) "
                f"or {tw} {_px(st['und_target'], ccy)} (target)")
    if st.get("expiry"):
        exit_txt += f"; otherwise close before expiry {datetime.fromisoformat(st['expiry']).strftime('%d %b')}"
    if not sized:
        legs.insert(0, "Not sized — shown for 1 lot/unit only (risk check failed)")
    return {"headline": headline, "legs": legs, "exit": exit_txt, "venue": venue, "units": units if sized else 0,
            "risk": risk_base, "risk_txt": f"{CCY_SIGN.get(base, '')}{risk_base:,.0f}" if risk_base else "-",
            "reward_x": round(st["rr"], 2), "structure": st["name"], "contract": st["label"],
            "why_structure": st.get("chosen_by")}
