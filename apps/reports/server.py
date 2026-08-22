"""Public read-only web view of backtest runs.

A separate process from the MCP server on purpose. `docs/MCP_AUTH.md` is blunt
that the MCP server authorises nobody, so it must never be reachable from the
internet — exposing it would hand anyone `backtests.run` and `send_report`.
This process has no tools in it at all: the worst a stranger can do is read a
chart. That isolation is the whole design, so do not add a write path here.

Serves:
    GET /                      index of runs
    GET /r/<run_id>/           the run's chart
    GET /r/<run_id>/<file>     one artifact
    GET /health                liveness

Bind and public URL come from the environment, because they differ per host:
loopback on a workstation, 0.0.0.0 on the server.
"""

from __future__ import annotations

import html
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "mcp_server"))
import runs  # noqa: E402

BIND = os.environ.get("REPORTS_BIND", "127.0.0.1")
PORT = int(os.environ.get("REPORTS_PORT", "8083"))

# Only what a report is made of. An allow-list rather than a deny-list: this is
# internet-facing, and a new file type appearing in a run directory should not
# silently become downloadable.
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def resolve(run_id: str, rel: str | None = None) -> Path | None:
    """Locate a run directory, or one file in it, refusing anything outside.

    Paths arrive from URLs, so everything is resolved and confirmed to sit
    under the root that owns the run before it is read — otherwise ".." walks
    out and this process serves the filesystem to the internet.
    """
    base = runs.run_root_for(run_id)
    if base is None:
        return None
    root, target = base.resolve(), (base / run_id).resolve()
    if not target.is_dir() or root not in target.parents:
        return None
    if rel is None:
        return target
    f = (target / rel).resolve()
    if not f.is_file() or target not in f.parents:
        return None
    if f.suffix.lower() not in TYPES:
        return None
    return f


_CSS = """
:root{--bg:#fcfcfb;--fg:#1a1a18;--dim:#6b6b66;--line:#e4e4df;--accent:#2f6f4f;
      --chip:#eef2ef;--warn:#8a5a2b;--warnbg:#fdf3e7}
:root:not([data-theme=light]) @media (prefers-color-scheme:dark){}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --bg:#16161a;--fg:#e8e8e4;--dim:#9a9a94;--line:#2c2c32;--accent:#7fbf9a;
  --chip:#22262a;--warn:#d6a86a;--warnbg:#2a2118}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1.25rem;background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:64rem;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 .25rem}
.sub{color:var(--dim);margin:0 0 1.75rem;font-size:.9rem}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th{text-align:left;font-weight:600;color:var(--dim);font-size:.78rem;
  text-transform:uppercase;letter-spacing:.04em;padding:0 .6rem .5rem;
  border-bottom:1px solid var(--line)}
td{padding:.6rem;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--chip)}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
.chip{display:inline-block;padding:.08rem .4rem;border-radius:.25rem;
  background:var(--chip);color:var(--dim);font-size:.75rem}
.adhoc{background:var(--warnbg);color:var(--warn)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.note{margin:1.5rem 0 0;padding:.75rem .9rem;background:var(--warnbg);
  color:var(--warn);border-radius:.35rem;font-size:.85rem}
.scroll{overflow-x:auto}
"""


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><div class=wrap>{body}</div></body></html>").encode()


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def index_html() -> bytes:
    rows = []
    for p in sorted(runs.all_patterns(),
                    key=lambda r: r.get("backtest_run_id", ""), reverse=True):
        rid = p.get("backtest_run_id", "")
        adhoc = rid.endswith("_adhoc") or "_ADHOC" in (p.get("pattern_id") or "")
        # Reach rate and win rate are both percentages in the nineties and mean
        # different things; the kind column is what stops them being read as
        # one number. See docs/BACKTEST_EXECUTION.md.
        kind = p.get("kind") or "—"
        headline = _pct(p.get("win_rate")) if kind == "strategy" else "—"
        rows.append(
            f"<tr><td><a href='/r/{html.escape(rid)}/'>"
            f"{html.escape(p.get('pattern_id') or rid)}</a>"
            f"{' <span class=\"chip adhoc\">ad-hoc</span>' if adhoc else ''}"
            f"<div class='mono' style='color:var(--dim);font-size:.78rem'>"
            f"{html.escape(rid)}</div></td>"
            f"<td>{html.escape(str(p.get('instrument') or '—'))} "
            f"<span class=chip>{html.escape(str(p.get('timeframe') or ''))}</span></td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td class=num>{headline}</td>"
            f"<td class=num>{p.get('sample_size') or '—'}</td>"
            f"<td class='mono' style='font-size:.78rem'>"
            f"{html.escape(str(p.get('tested_from') or '—'))} → "
            f"{html.escape(str(p.get('tested_to') or '—'))}</td></tr>")

    body = (
        "<h1>Backtest reports</h1>"
        f"<p class=sub>{len(rows)} run"
        f"{'s' if len(rows) != 1 else ''}, newest first.</p>"
        "<div class=scroll><table><thead><tr><th>Pattern</th><th>Instrument</th>"
        "<th>Kind</th><th class=num>Win rate</th><th class=num>Trades</th>"
        "<th>Period</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
        "<p class=note><strong>Win rate is blank for zone studies on purpose.</strong> "
        "They have no entries or P&amp;L, only reach rates — how often price hit a "
        "level. A reach rate is not a win rate.</p>")
    return _page("Backtest reports", body)


def run_html(run_id: str, d: Path) -> bytes:
    files = sorted(f for f in d.iterdir() if f.suffix.lower() in TYPES)
    summary = {}
    sj = d / "summary.json"
    if sj.is_file():
        try:
            summary = json.loads(sj.read_text())
        except (ValueError, OSError):
            summary = {}
    links = "".join(
        f"<tr><td><a href='/r/{html.escape(run_id)}/{html.escape(f.name)}'>"
        f"{html.escape(f.name)}</a></td>"
        f"<td class=num>{f.stat().st_size:,} B</td></tr>" for f in files)
    body = (
        f"<h1>{html.escape(run_id)}</h1>"
        "<p class=sub><a href='/'>← all reports</a></p>"
        "<div class=scroll><table><thead><tr><th>File</th>"
        "<th class=num>Size</th></tr></thead><tbody>"
        f"{links}</tbody></table></div>"
        + (f"<h2 style='font-size:1rem;margin:1.75rem 0 .5rem'>summary.json</h2>"
           f"<div class=scroll><pre class=mono>"
           f"{html.escape(json.dumps(summary, indent=2))}</pre></div>"
           if summary else ""))
    return _page(run_id, body)


class Handler(BaseHTTPRequestHandler):
    server_version = "trading-reports"
    sys_version = ""                      # do not advertise the Python version

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The charts are self-contained; nothing should be fetched from
        # elsewhere, and nothing here should end up framed by another site.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, code: int, msg: str) -> None:
        self._send(code, msg.encode(), "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)

        if path == "/health":
            return self._send(200, b'{"ok": true}', "application/json")
        if path in ("/", "/index.html"):
            return self._send(200, index_html(), "text/html; charset=utf-8")
        if not path.startswith("/r/"):
            return self._text(404, "not found")

        rest = path[3:]
        run_id, _, rel = rest.partition("/")
        if not run_id:
            return self._text(404, "not found")

        d = resolve(run_id)
        if d is None:
            return self._text(404, "no such run")

        # Bare run URL goes straight to the chart when there is one: the point
        # of a link in a chat is that tapping it shows the picture, not a file
        # listing. Runs without a chart fall back to the listing.
        if rel in ("", "index.html"):
            if (d / "chart.html").is_file():
                self.send_response(302)
                self.send_header("Location", f"/r/{run_id}/chart.html")
                self.end_headers()
                return
            return self._send(200, run_html(run_id, d), "text/html; charset=utf-8")

        f = resolve(run_id, rel)
        if f is None:
            return self._text(404, "not found")
        try:
            blob = f.read_bytes()
        except OSError:
            return self._text(500, "unreadable")
        self._send(200, blob, TYPES[f.suffix.lower()])

    def log_message(self, fmt, *args):
        sys.stderr.write("[reports] %s\n" % (fmt % args))


if __name__ == "__main__":
    n = len(runs.all_patterns())
    print(f"[reports] serving {n} run(s) on http://{BIND}:{PORT}", flush=True)
    if BIND not in ("127.0.0.1", "localhost"):
        print("[reports] bound publicly — read-only, no tools reachable here",
              flush=True)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
