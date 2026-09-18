"""Minimal server-rendered travel dashboard (stdlib only).

Route ``/travel`` (and ``/``) shows active price watches, confirmed
trips, and monthly SerpAPI usage. WhatsApp remains the primary
interface; this is a read-only overview suitable for Maritime.

Run: python -m src.cli dashboard [--port 8090]
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.services.search_budget import SearchBudgetManager
from src.storage.repository import Repository

_STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem auto;
     max-width:900px;padding:0 16px;color:#1f2937;background:#fff}
h1{font-size:1.4rem} h2{font-size:1.05rem;margin-top:2rem;color:#374151}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #e5e7eb}
th{color:#6b7280;font-weight:600}
.badge{background:#eef2ff;color:#3730a3;border-radius:6px;padding:.1rem .5rem}
.muted{color:#6b7280}.down{color:#047857}.up{color:#b91c1c}
.bar{background:#e5e7eb;border-radius:6px;height:10px;overflow:hidden;max-width:320px}
.bar>div{background:#1d4ed8;height:100%}
"""


def _fmt_money(v) -> str:
    return f"${v:,.0f}" if v is not None else "—"


def _fmt_dt(v) -> str:
    return v.strftime("%b %d %H:%M") if v else "—"


def render_travel_page(repo: Repository, budget: SearchBudgetManager) -> str:
    usage = budget.get_usage()
    pct = min(int(usage.used / usage.limit * 100), 100) if usage.limit else 0

    # Watches and trips across all users (personal deployment).
    watches = []
    trips = []
    seen_users = set()
    for row in repo._execute("SELECT DISTINCT user_id FROM price_watches").fetchall():
        seen_users.add(row[0])
    for row in repo._execute("SELECT DISTINCT user_id FROM booked_trips").fetchall():
        seen_users.add(row[0])
    for user in sorted(seen_users):
        watches.extend(repo.list_watches(user))
        trips.extend(repo.list_booked_trips(user))

    def esc(v) -> str:
        return html.escape(str(v)) if v is not None else "—"

    watch_rows = []
    for w in watches:
        change = ""
        if w.initial_price and w.latest_price:
            delta = w.latest_price - w.initial_price
            cls = "down" if delta <= 0 else "up"
            change = f"<span class='{cls}'>{'-' if delta <= 0 else '+'}${abs(delta):,.0f}</span>"
        dates = w.request.outbound_date.start.isoformat() if w.request.outbound_date else "?"
        if w.request.return_date:
            dates += f" → {w.request.return_date.start.isoformat()}"
        status = "active" if w.active else ("paused" if w.paused else "stopped")
        watch_rows.append(
            f"<tr><td>{esc(w.origin)}→{esc(w.destination)}</td><td>{esc(dates)}</td>"
            f"<td>{_fmt_money(w.latest_price)}</td><td>{change or '—'}</td>"
            f"<td>{_fmt_money(w.lowest_price)}</td><td>{_fmt_money(w.target_price)}</td>"
            f"<td>{_fmt_dt(w.next_check_at)}</td><td>{status}</td></tr>"
        )

    trip_rows = []
    for t in trips:
        trip_rows.append(
            f"<tr><td>{esc(t.airline)}</td><td>{esc(t.flight_number)}</td>"
            f"<td>{esc(t.origin)}→{esc(t.destination)}</td>"
            f"<td>{_fmt_dt(t.departure)}</td><td>{esc(t.confirmation_code)}</td>"
            f"<td>{esc(t.status.value)}</td></tr>"
        )

    by_cat = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>"
        for k, v in sorted(usage.by_category.items())
    ) or "<tr><td colspan=2 class=muted>no searches yet this month</td></tr>"

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Travel Agent</title><style>{_STYLE}</style></head><body>
<h1>✈️ Travel dashboard <span class="badge">{usage.month}</span></h1>

<h2>SerpAPI usage</h2>
<p>{usage.used} / {usage.limit} searches used
 · {usage.remaining} remaining
 · reserve {usage.reserve} (background floor: {usage.remaining_for_background} left)
 · {usage.cache_hits} cache hits (free)</p>
<div class="bar"><div style="width:{pct}%"></div></div>
<table><tr><th>Category</th><th>Calls</th></tr>{by_cat}</table>

<h2>Price watches ({len(watches)})</h2>
<table><tr><th>Route</th><th>Dates</th><th>Current</th><th>Change</th>
<th>Lowest</th><th>Target</th><th>Next check</th><th>Status</th></tr>
{''.join(watch_rows) or '<tr><td colspan=8 class=muted>none</td></tr>'}</table>

<h2>Confirmed trips ({len(trips)})</h2>
<table><tr><th>Airline</th><th>Flight</th><th>Route</th><th>Departure</th>
<th>Conf.</th><th>Status</th></tr>
{''.join(trip_rows) or '<tr><td colspan=6 class=muted>none</td></tr>'}</table>

<p class="muted">Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC ·
primary interface: WhatsApp</p>
</body></html>"""


def serve(repo: Repository, budget: SearchBudgetManager, port: int = 8090) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("", "/travel"):
                body = render_travel_page(repo, budget).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # quiet
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"travel dashboard on http://0.0.0.0:{port}/travel")
    server.serve_forever()
