"""Console (rich) + HTML report rendering."""
from __future__ import annotations

import html
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .data import CCY_SIGN
from .instruments import SEG_COLOR, SEG_LABEL

COL = {"KESTREL": "#4fb3ff", "PRISM": "#b36bff", "HERALD": "#ff6a3d",
       "PULSE": "#3de0c4", "AEGIS": "#ff9a1f", "ARBITER": "#ffd23d"}
DEC = {"TRADE": "bold green", "WAIT": "bold yellow", "REJECT": "bold red", "HOLD": "bold cyan"}


def fp(x):
    if x is None:
        return "-"
    return f"{x:,.2f}" if abs(x) >= 10 else f"{x:,.4f}"


def fu(units, integer=True):
    return f"{units:,.0f}" if integer or float(units).is_integer() else f"{units:,.4f}"


def seg_tag(seg):
    return f"[{SEG_COLOR[seg]}]{SEG_LABEL[seg]}[/]"


def struct_text(res):
    st = res.get("structure")
    if not st:
        return f"[red]none[/] [dim]{res.get('structure_note') or ''}"
    extra = ""
    if st.get("iv_hv") is not None:
        extra = f" [dim]IV/HV {st['iv_hv']:.2f}{' · POP %.0f%%' % (100 * st['pop']) if st['type'] == 'CREDIT_SPREAD' else ''}"
    return f"{st['name']} [bold]{st['label']}[/] [dim]{st['rr']:.1f}R[/]{extra}"


def console_report(run: dict, con: Console | None = None):
    con = con or Console()
    cur = CCY_SIGN.get(run["base"], run["base"])
    tag = " [DEMO DATA]" if run["demo"] else ""
    con.rule(f"[bold]SENTINEL-6 · {run['ts']} · {run['scanned']} assets scanned in {run['secs']}s{tag}")

    t = Table(title="KESTREL · ranked watchlist (top 15)", title_style=COL["KESTREL"], header_style="bold")
    for c in ("#", "Seg", "Symbol", "Price", "1D", "20D", "Mom z", "Vol x", "Dir", "Score"):
        t.add_column(c, justify="right" if c not in ("Seg", "Symbol", "Dir") else "left")
    for i, r in enumerate(run["scan"][:15], 1):
        t.add_row(str(i), seg_tag(r["segment"]), r["symbol"], fp(r["price"]), f"{r['chg_1d']:+.1%}", f"{r['ret_20d']:+.1%}",
                  f"{r['momentum_z']:+.2f}", f"{r['vol_ratio']:.2f}",
                  f"[green]{r['direction']}" if r["direction"] == "LONG" else f"[red]{r['direction']}", str(r["score"]))
    con.print(t)

    d = Table(title="Desk reports → ARBITER", title_style=COL["ARBITER"], header_style="bold")
    for c in ("Seg", "Symbol", "Dir", "PRISM", "HERALD", "PULSE", "Structure", "AEGIS", "ARBITER", "Why"):
        d.add_column(c, overflow="fold")
    for res in run["results"]:
        r = res["reports"]
        a = r["arbiter"]
        ag = r["aegis"]
        units = ag["details"].get("units") or 0
        d.add_row(seg_tag(res["segment"]), res["symbol"], res["direction"],
                  f"{r['prism']['verdict']} {r['prism']['score']:+.2f}",
                  f"{r['herald']['verdict']} {r['herald']['score']:+.2f}",
                  f"{r['pulse']['verdict']} {r['pulse']['score']:+.2f}",
                  struct_text(res),
                  ("[green]" if ag["verdict"] == "APPROVED" else "[red]") + ag["verdict"]
                  + (f" ×{fu(units, (res['structure'] or {}).get('integer', True))}" if units else ""),
                  f"[{DEC[a['verdict']]}]{a['verdict']}[/] {a['details']['composite']:+.2f}", a["details"]["reason"])
    con.print(d)

    b = run["book"]
    pt = Table(title=f"PAPER BOOK · equity {cur}{b['equity']:,.0f}  (realized {cur}{b['realized']:+,.0f} · "
                     f"open {cur}{b['unrealized']:+,.0f})", header_style="bold")
    for c in ("Seg", "Position", "Units", "Und entry", "Und last", "Stop", "Target", f"Max loss {cur}", f"uPnL {cur}"):
        pt.add_column(c)
    for p in b["positions"]:
        pt.add_row(seg_tag(p["segment"]), f"{p['name']} {p['label']}", fu(p["units"], False), fp(p["und_entry"]),
                   fp(p["last"]), fp(p["und_stop"]), fp(p["und_target"]), f"{p['max_loss_base']:,.0f}",
                   f"[{'green' if p['upnl_base'] >= 0 else 'red'}]{p['upnl_base']:+,.0f}")
    con.print(pt if b["positions"] else Panel("No open paper positions", title="PAPER BOOK"))


# ------------------------------------------------------------------ HTML
CSS = """
:root{--bg:#0b0f1e;--panel:#121833;--line:#26305c;--ink:#e8ecff;--mute:#8d96c4}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 'VT323',monospace}
h1,h2{font-family:'Press Start 2P',monospace;letter-spacing:1px}h1{font-size:16px;margin:0}
h2{font-size:11px;margin:0 0 10px}.wrap{max-width:1240px;margin:auto;padding:16px}
.bar{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}.chip{border:2px solid;padding:4px 10px;font-family:'Press Start 2P';font-size:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}
.card{background:var(--panel);border:2px solid var(--line);padding:12px;margin:10px 0;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:16px}td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:right}
td:first-child,th:first-child{text-align:left}.up{color:#4dff8a}.dn{color:#ff5b5b}.mute{color:var(--mute)}
.dec{font-family:'Press Start 2P';font-size:13px;padding:6px 12px;border:3px solid;display:inline-block}
.TRADE{color:#4dff8a;border-color:#4dff8a}.WAIT{color:#ffd23d;border-color:#ffd23d}.REJECT{color:#ff5b5b;border-color:#ff5b5b}.HOLD{color:#3de0c4;border-color:#3de0c4}
.seg{font-family:'Press Start 2P';font-size:9px;padding:2px 6px;border:1px solid}
.k{color:var(--mute)}footer{color:var(--mute);text-align:center;margin:20px 0;font-size:14px}
"""


def _e(x):
    return html.escape(str(x))


def _seg(seg):
    c = SEG_COLOR[seg]
    return f"<span class=seg style='color:{c};border-color:{c}'>{SEG_LABEL[seg]}</span>"


def _agent(name, rep, body):
    c = COL[name]
    return (f"<div class='card' style='border-color:{c}'><h2 style='color:{c}'>{name} · {_e(rep['role'])}</h2>"
            f"<div><b style='color:{c}'>{_e(rep['verdict'])}</b> <span class='mute'>score {rep['score']:+.2f} · "
            f"confidence {_e(rep['confidence'])}</span></div>{body}"
            + "".join(f"<div class='mute'>⚠ {_e(n)}</div>" for n in rep["notes"]) + "</div>")


def _rows(pairs):
    return "<table>" + "".join(f"<tr><td class=k>{_e(k)}</td><td>{v}</td></tr>" for k, v in pairs) + "</table>"


def html_report(run: dict, path: Path):
    b = run["book"]
    cur = CCY_SIGN.get(run["base"], run["base"])
    chips = "".join(f"<span class='chip' style='color:{c};border-color:{c}'>{n}</span>" for n, c in COL.items())
    scan = "".join(
        f"<tr><td>{i}. {_e(r['symbol'])}</td><td>{_seg(r['segment'])}</td><td>{fp(r['price'])}</td>"
        f"<td class='{'up' if r['chg_1d'] >= 0 else 'dn'}'>{r['chg_1d']:+.1%}</td>"
        f"<td class='{'up' if r['direction'] == 'LONG' else 'dn'}'>{r['direction']}</td><td><b>{r['score']}</b></td></tr>"
        for i, r in enumerate(run["scan"][:20], 1))
    blocks = []
    for res in run["results"]:
        r = res["reports"]
        p, h, pu, ag, ar = r["prism"]["details"], r["herald"]["details"], r["pulse"]["details"], r["aegis"]["details"], r["arbiter"]
        st = res.get("structure")
        prism_b = _rows([("Trend", p["trend"]), ("Volume", p["volume"]), ("RSI", p["rsi"]),
                         ("Support / Resist.", f"{fp(p['support'])} / {fp(p['resistance'])}"),
                         ("Entry · Stop · Target", f"{fp(p['entry'])} · {fp(p['stop'])} · {fp(p['target'])}")])
        cat = h.get("catalyst")
        herald_b = (f"<div class=mute>{h.get('headlines', 0)} headlines</div>"
                    + (f"<div><b>HIGH-IMPACT:</b> {_e(cat['type'])} — source check {cat['source_check']} · {cat['time_sensitivity']}</div>" if cat else "")
                    + "<table>" + "".join(f"<tr><td>{_e(x['title'][:90])}<div class=mute>{_e(x['source'])} · {x['age_h']}h</div></td>"
                                          f"<td class='{'up' if x['score'] > 0 else 'dn'}'>{x['score']:+.2f}</td></tr>" for x in h.get("top", [])[:4]) + "</table>")
        pos = pu.get("positioning") or {}
        pos_pairs = []
        if pos.get("pcr") is not None:
            pos_pairs += [("PCR (OI)", pos["pcr"]), ("Max pain", fp(pos.get("max_pain"))),
                          ("Call wall / Put wall", f"{fp(pos.get('call_wall'))} / {fp(pos.get('put_wall'))}"),
                          ("ATM IV", f"{100 * pos.get('atm_iv', 0):.1f}%")]
        if pos.get("funding") is not None:
            pos_pairs.append(("Funding /8h", f"{100 * pos['funding']:+.4f}%"))
        pulse_b = _rows([("Posts", pu.get("posts", 0)), ("Crowd", pu.get("crowd", "-")),
                         ("Mention velocity", f"x{pu.get('velocity', 0)}")] + pos_pairs)
        if st:
            legs = "<br>".join(f"{'BUY' if l['side'] > 0 else 'SELL'} {l['kind']}{' ' + fp(l['strike']) if l['strike'] else ''} @ {fp(l['price'])}"
                               + (f" · IV {100 * l['iv']:.1f}%" if l['iv'] else "") for l in st["legs"])
            struct_b = _rows([("Structure", f"<b>{_e(st['name'])}</b>"), ("Contract", _e(st["label"])), ("Legs", legs),
                              ("Max loss / unit", fp(st["max_loss"])), ("Max gain / unit", fp(st["max_gain"])),
                              ("R:R", f"{st['rr']:.2f}")]
                             + ([("Why", _e(st.get("chosen_by", ""))), ("DTE", st.get("dte")),
                                 ("POP", f"{100 * (st.get('pop') or 0):.0f}%")] if st.get("iv_hv") is not None else [])
                             + [("Units", f"{ag.get('units', 0):,.4g}"), (f"Risk {cur}", f"{ag.get('loss_at_stop_base', 0):,.0f}")])
        else:
            struct_b = f"<div class=dn>No structure: {_e(res.get('structure_note'))}</div>"
        aegis_b = "<table>" + "".join(f"<tr><td>{_e(k)}<div class=mute>{_e(v['detail'])}</div></td><td class='{'up' if v['pass'] else 'dn'}'>{'PASS' if v['pass'] else 'FAIL'}</td></tr>"
                                      for k, v in ag["checks"].items()) + "</table>"
        blocks.append(
            f"<div class='card'><h1>{_seg(res['segment'])} {_e(res['symbol'])} <span class='mute'>· {res['direction']}</span></h1>"
            f"<div style='margin:10px 0'><span class='dec {ar['verdict']}'>{ar['verdict']}</span> "
            f"<span class=mute>composite {ar['details']['composite']:+.2f} — {_e(ar['details']['reason'])}</span></div>"
            f"<div class='grid'>{_agent('PRISM', r['prism'], prism_b)}{_agent('HERALD', r['herald'], herald_b)}"
            f"{_agent('PULSE', r['pulse'], pulse_b)}"
            f"<div class=card style='border-color:#ffd23d'><h2 style='color:#ffd23d'>TRADE STRUCTURE</h2>{struct_b}</div>"
            f"{_agent('AEGIS', r['aegis'], aegis_b)}</div></div>")
    pos = "".join(f"<tr><td>{_seg(p['segment'])} {_e(p['name'])} {_e(p['label'])}</td><td>{fu(p['units'], False)}</td>"
                  f"<td>{fp(p['und_entry'])}</td><td>{fp(p['last'])}</td><td>{fp(p['und_stop'])}</td><td>{fp(p['und_target'])}</td>"
                  f"<td class='{'up' if p['upnl_base'] >= 0 else 'dn'}'>{p['upnl_base']:+,.0f}</td></tr>"
                  for p in b["positions"]) or "<tr><td class=mute colspan=7>No open positions</td></tr>"
    doc = f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>SENTINEL-6 desk</title><link href='https://fonts.googleapis.com/css2?family=Press+Start+2P&family=VT323&display=swap' rel=stylesheet>
<style>{CSS}</style></head><body><div class=wrap>
<h1>SENTINEL-6 <span class=mute>· {run['ts']}{' · DEMO DATA' if run['demo'] else ''}</span></h1><div class=bar>{chips}</div>
<div class='card' style='border-color:{COL['KESTREL']}'><h2 style='color:{COL['KESTREL']}'>KESTREL · {run['scanned']} assets scanned</h2>
<table><tr><th>Symbol</th><th>Seg</th><th>Price</th><th>1D</th><th>Dir</th><th>Score</th></tr>{scan}</table></div>
{''.join(blocks)}
<div class=card><h2>PAPER BOOK · equity {cur}{b['equity']:,.0f} · realized {cur}{b['realized']:+,.0f} · open {cur}{b['unrealized']:+,.0f}</h2>
<table><tr><th>Position</th><th>Units</th><th>Und entry</th><th>Und last</th><th>Stop</th><th>Target</th><th>uPnL {cur}</th></tr>{pos}</table></div>
<footer>Self-generated research · PAPER TRADING ONLY · not financial advice</footer></div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(run, default=str, indent=1), encoding="utf-8")
