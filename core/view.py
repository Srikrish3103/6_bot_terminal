"""Console (rich) + HTML report rendering."""
from __future__ import annotations

import html
import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

COL = {"KESTREL": "#4fb3ff", "PRISM": "#b36bff", "HERALD": "#ff6a3d",
       "PULSE": "#3de0c4", "AEGIS": "#ff9a1f", "ARBITER": "#ffd23d"}
DEC = {"TRADE": "bold green", "WAIT": "bold yellow", "REJECT": "bold red", "HOLD": "bold cyan"}


def fp(x):
    return f"{x:,.2f}" if abs(x) >= 10 else f"{x:,.4f}"


def console_report(run: dict, con: Console | None = None):
    con = con or Console()
    tag = " [DEMO DATA]" if run["demo"] else ""
    con.rule(f"[bold]SENTINEL-6 · {run['ts']} · {run['scanned']} assets scanned in {run['secs']}s{tag}")

    t = Table(title="KESTREL · ranked watchlist", title_style=COL["KESTREL"], header_style="bold")
    for c in ("#", "Symbol", "Class", "Price", "1D", "20D", "Mom z", "Vol x", "Dir", "Score"):
        t.add_column(c, justify="right" if c not in ("Symbol", "Class", "Dir") else "left")
    for i, r in enumerate(run["scan"][:10], 1):
        t.add_row(str(i), r["symbol"], r["class"], fp(r["price"]), f"{r['chg_1d']:+.1%}", f"{r['ret_20d']:+.1%}",
                  f"{r['momentum_z']:+.2f}", f"{r['vol_ratio']:.2f}",
                  f"[green]{r['direction']}" if r["direction"] == "LONG" else f"[red]{r['direction']}", str(r["score"]))
    con.print(t)

    d = Table(title="Desk reports → ARBITER", title_style=COL["ARBITER"], header_style="bold")
    for c in ("Symbol", "Dir", "KESTREL", "PRISM", "HERALD", "PULSE", "AEGIS", "ARBITER", "Score", "Why"):
        d.add_column(c)
    for res in run["results"]:
        r = res["reports"]
        a = r["arbiter"]
        d.add_row(res["symbol"], res["direction"],
                  r["kestrel"]["verdict"], f"{r['prism']['verdict']} ({r['prism']['score']:+.2f})",
                  f"{r['herald']['verdict']} ({r['herald']['score']:+.2f})",
                  f"{r['pulse']['verdict']} ({r['pulse']['score']:+.2f})",
                  ("[green]" if r["aegis"]["verdict"] == "APPROVED" else "[red]") + r["aegis"]["verdict"],
                  f"[{DEC[a['verdict']]}]{a['verdict']}", f"{a['details']['composite']:+.2f}", a["details"]["reason"])
    con.print(d)

    b = run["book"]
    pt = Table(title=f"PAPER BOOK · equity ${b['equity']:,.0f}  (realized {b['realized']:+,.0f} · open {b['unrealized']:+,.0f})",
               header_style="bold")
    for c in ("Symbol", "Side", "Qty", "Entry", "Last", "Stop", "Target", "uPnL $"):
        pt.add_column(c)
    for p in b["positions"]:
        pt.add_row(p["symbol"], p["side"], f"{p['qty']:,.4f}", fp(p["entry"]), fp(p["last"]), fp(p["stop"]),
                   fp(p["target"]), f"{p['upnl_usd']:+,.0f}")
    con.print(pt if b["positions"] else Panel("No open paper positions", title="PAPER BOOK"))


# ------------------------------------------------------------------ HTML
CSS = """
:root{--bg:#0b0f1e;--panel:#121833;--line:#26305c;--ink:#e8ecff;--mute:#8d96c4}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 'VT323',monospace}
h1,h2{font-family:'Press Start 2P',monospace;letter-spacing:1px}h1{font-size:18px;margin:0}
h2{font-size:11px;margin:0 0 10px}.wrap{max-width:1200px;margin:auto;padding:16px}
.bar{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}.chip{border:2px solid;padding:4px 10px;font-family:'Press Start 2P';font-size:10px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}
.card{background:var(--panel);border:2px solid var(--line);padding:12px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-size:16px}td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:right}
td:first-child,th:first-child{text-align:left}.up{color:#4dff8a}.dn{color:#ff5b5b}.mute{color:var(--mute)}
.dec{font-family:'Press Start 2P';font-size:14px;padding:6px 12px;border:3px solid;display:inline-block}
.TRADE{color:#4dff8a;border-color:#4dff8a}.WAIT{color:#ffd23d;border-color:#ffd23d}.REJECT{color:#ff5b5b;border-color:#ff5b5b}.HOLD{color:#3de0c4;border-color:#3de0c4}
.k{color:var(--mute)}footer{color:var(--mute);text-align:center;margin:20px 0;font-size:14px}
"""


def _e(x):
    return html.escape(str(x))


def _agent(name, rep, body):
    c = COL[name]
    return (f"<div class='card' style='border-color:{c}'><h2 style='color:{c}'>{name} · {_e(rep['role'])}</h2>"
            f"<div><b style='color:{c}'>{_e(rep['verdict'])}</b> <span class='mute'>score {rep['score']:+.2f} · "
            f"confidence {_e(rep['confidence'])}</span></div>{body}"
            + "".join(f"<div class='mute'>⚠ {_e(n)}</div>" for n in rep["notes"]) + "</div>")


def html_report(run: dict, path: Path):
    b = run["book"]
    chips = "".join(f"<span class='chip' style='color:{c};border-color:{c}'>{n}</span>" for n, c in COL.items())
    scan = "".join(
        f"<tr><td>{i}. {_e(r['symbol'])}</td><td>{r['class']}</td><td>{fp(r['price'])}</td>"
        f"<td class='{'up' if r['chg_1d'] >= 0 else 'dn'}'>{r['chg_1d']:+.1%}</td>"
        f"<td class='{'up' if r['direction'] == 'LONG' else 'dn'}'>{r['direction']}</td><td><b>{r['score']}</b></td></tr>"
        for i, r in enumerate(run["scan"][:12], 1))
    blocks = []
    for res in run["results"]:
        r = res["reports"]
        p, h, pu, ag, ar = r["prism"]["details"], r["herald"]["details"], r["pulse"]["details"], r["aegis"]["details"], r["arbiter"]
        prism_b = (f"<table><tr><td class=k>Trend</td><td>{p['trend']}</td></tr><tr><td class=k>Volume</td><td>{p['volume']}</td></tr>"
                   f"<tr><td class=k>RSI</td><td>{p['rsi']}</td></tr><tr><td class=k>Support / Resist.</td><td>{fp(p['support'])} / {fp(p['resistance'])}</td></tr>"
                   f"<tr><td class=k>Entry · Stop · Target</td><td>{fp(p['entry'])} · {fp(p['stop'])} · {fp(p['target'])}</td></tr></table>")
        cat = h.get("catalyst")
        herald_b = (f"<div class=mute>{h.get('headlines', 0)} headlines</div>"
                    + (f"<div><b>HIGH-IMPACT:</b> {_e(cat['type'])} — source check {cat['source_check']} · {cat['time_sensitivity']}</div>" if cat else "")
                    + "<table>" + "".join(f"<tr><td>{_e(x['title'][:90])}<div class=mute>{_e(x['source'])} · {x['age_h']}h</div></td>"
                                          f"<td class='{'up' if x['score'] > 0 else 'dn'}'>{x['score']:+.2f}</td></tr>" for x in h.get("top", [])[:4]) + "</table>")
        pulse_b = (f"<table><tr><td class=k>Posts</td><td>{pu.get('posts', 0)}</td></tr><tr><td class=k>Crowd</td><td>{pu.get('crowd', '-')}</td></tr>"
                   f"<tr><td class=k>Mention velocity</td><td>x{pu.get('velocity', 0)}</td></tr><tr><td class=k>Contrarian view</td><td>{pu.get('contrarian', '-')}</td></tr></table>")
        aegis_b = "<table>" + "".join(f"<tr><td>{_e(k)}<div class=mute>{_e(v['detail'])}</div></td><td class='{'up' if v['pass'] else 'dn'}'>{'PASS' if v['pass'] else 'FAIL'}</td></tr>"
                                      for k, v in ag["checks"].items()) + "</table>"
        blocks.append(
            f"<div class='card'><h1>{_e(res['symbol'])} <span class='mute'>· {res['direction']}</span></h1>"
            f"<div style='margin:10px 0'><span class='dec {ar['verdict']}'>{ar['verdict']}</span> "
            f"<span class=mute>composite {ar['details']['composite']:+.2f} — {_e(ar['details']['reason'])}</span></div>"
            f"<div class='grid'>{_agent('PRISM', r['prism'], prism_b)}{_agent('HERALD', r['herald'], herald_b)}"
            f"{_agent('PULSE', r['pulse'], pulse_b)}{_agent('AEGIS', r['aegis'], aegis_b)}</div></div>")
    pos = "".join(f"<tr><td>{_e(p['symbol'])}</td><td>{p['side']}</td><td>{fp(p['entry'])}</td><td>{fp(p['last'])}</td>"
                  f"<td>{fp(p['stop'])}</td><td>{fp(p['target'])}</td><td class='{'up' if p['upnl_usd'] >= 0 else 'dn'}'>{p['upnl_usd']:+,.0f}</td></tr>"
                  for p in b["positions"]) or "<tr><td class=mute colspan=7>No open positions</td></tr>"
    doc = f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>
<title>SENTINEL-6 desk</title><link href='https://fonts.googleapis.com/css2?family=Press+Start+2P&family=VT323&display=swap' rel=stylesheet>
<style>{CSS}</style></head><body><div class=wrap>
<h1>SENTINEL-6 <span class=mute>· {run['ts']}{' · DEMO DATA' if run['demo'] else ''}</span></h1><div class=bar>{chips}</div>
<div class='card' style='border-color:{COL['KESTREL']}'><h2 style='color:{COL['KESTREL']}'>KESTREL · {run['scanned']} assets scanned</h2>
<table><tr><th>Symbol</th><th>Class</th><th>Price</th><th>1D</th><th>Dir</th><th>Score</th></tr>{scan}</table></div>
{''.join(blocks)}
<div class=card><h2>PAPER BOOK · equity ${b['equity']:,.0f} · realized {b['realized']:+,.0f} · open {b['unrealized']:+,.0f}</h2>
<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Last</th><th>Stop</th><th>Target</th><th>uPnL $</th></tr>{pos}</table></div>
<footer>Self-generated research · PAPER TRADING ONLY · not financial advice</footer></div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(run, default=str, indent=1), encoding="utf-8")
