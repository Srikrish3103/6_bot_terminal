"""Local web dashboard: python run.py --web  ->  http://localhost:8765

Same background engine as the live terminal (cycles + quote ticks), exposed as
JSON at /api/state and rendered by web/dashboard.html. Runs only on your machine.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .data import CCY_SIGN
from .instruments import SEG_COLOR, SEG_LABEL
from .live import AGENTS, Engine, State

log = logging.getLogger("web")


def state_json(st: State, eng: Engine) -> dict:
    now = time.time()
    with st.lock:
        unreal = sum(p["upnl_base"] for p in st.positions)
        equity = st.start + st.realized + unreal
        scan = []
        for r in st.scan[:30]:
            q = st.quotes.get(r["key"], {})
            px = q.get("px", r["price"])
            scan.append({**r, "live": px, "prev": q.get("prev", px), "vs_scan": (px / q.get("ref", px) - 1) if q.get("ref") else 0,
                         "tick_age": now - q.get("t", 0)})
        positions = []
        for p in st.positions:
            positions.append({k: p[k] for k in ("id", "key", "segment", "symbol", "direction", "structure", "name", "label",
                                                "units", "und_entry", "und_stop", "und_target", "last", "upnl_base",
                                                "max_loss_base", "opened_at", "ccy")})
        run = st.run or {}
        closed = []
        try:
            closed = eng.desk.book.closed_positions(15) if hasattr(eng, "desk") else []
        except Exception:  # noqa
            pass
        return {
            "now": datetime.now().strftime("%d %b %Y %H:%M:%S"),
            "demo": st.demo, "base": st.base, "cur": CCY_SIGN.get(st.base, st.base),
            "busy": st.busy, "cycles": st.cycles,
            "last_cycle": st.last_cycle.strftime("%H:%M:%S") if st.last_cycle else None,
            "next_cycle_in": max(0, st.next_cycle - now), "next_tick_in": max(0, st.next_tick - now),
            "equity": equity, "start": st.start, "realized": st.realized, "unrealized": unreal,
            "agents": [{"name": n, "color": c, "role": r, **st.agents[n]} for n, c, r in AGENTS],
            "log": [{"t": t, "agent": a, "state": s, "msg": m} for t, a, s, m in list(st.log)[-60:]][::-1],
            "funnel": run.get("funnel"), "results": run.get("results", []), "scan": scan,
            "positions": positions, "closed": closed, "scanned": run.get("scanned", 0),
            "segments": {k: {"label": SEG_LABEL[k], "color": SEG_COLOR[k]} for k in SEG_LABEL},
        }


def make_handler(st, eng, page: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):          # keep the console quiet
            pass

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                self._send(200, json.dumps(state_json(st, eng), default=str).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/run":
                if not st.busy:
                    st.next_cycle = 0
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b"not found", "text/plain")
    return H


def run_web(cfg, root: Path, demo=False, execute=True, cycle_min=15, tick_sec=30, pace=0.15,
            port=8765, open_browser=True):
    st = State()
    eng = Engine(cfg, root, st, demo, execute, cycle_min, tick_sec, pace)
    eng.start()
    page = root / "web" / "dashboard.html"
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st, eng, page))
    url = f"http://localhost:{port}"
    print(f"SENTINEL-6 dashboard → {url}   ({'DEMO data' if demo else 'live data'} · paper only · Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop_flag.set()
        srv.server_close()
