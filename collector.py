"""Central metrics collector for the distributed swarm (push model).

Nodes POST their /stats snapshot here instead of being polled, so the collector
is the one service that accepts inbound connections — nodes only ever dial out,
which works across data centers / NAT. Nodes are independent and identified by a
per-node UUID (node_key); there is no enumeration, the collector learns nodes as
they push.

State is in memory only — the collector keeps just the latest snapshot per node
and serves a live view; nothing is persisted. A node silent past NODE_STALE_AFTER
drops out until it reports again; on restart the collector rebuilds within one
push interval.

Endpoints:
  POST /ingest   - receive one node's snapshot JSON; keep it as that node's latest
  GET  /live     - {"ts", "nodes": [...]} latest snapshot of each fresh node,
                   the live input for piece_map and any other viewer
  GET  /summary  - {"ts", "torrents": [...]} the same swarm-wide aggregation
                   piece_map.py renders (per torrent: meta, per-node ownership,
                   availability, copies summary, per-file replication), shaped as
                   render-ready JSON for the web UI. Built via swarm_stats so it
                   always agrees with piece_map.
  GET  /stats    - the collector's own health: nodes seen + last-seen ages
  GET  /         - the web UI (static files served from ./webui/)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import swarm_stats

# The web UI is plain static files (no build step); the collector is already the
# one inbound service, so it serves them alongside the data endpoints.
WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
# path -> (filename under WEBUI_DIR, content-type). A small whitelist keeps the
# static surface explicit and sidesteps any path-traversal concern.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/tutuca.js": ("tutuca.js", "text/javascript; charset=utf-8"),
}

LOCK = threading.Lock()
# node_key -> (received_ts, snapshot). The freshest state of every node, in
# memory; the live input for /live.
LATEST: dict = {}


def fresh_snapshots(now: float = None) -> list:
    """Latest snapshot of every node still reporting. Nodes silent past
    NODE_STALE_AFTER drop out."""
    now = now if now is not None else time.time()
    with LOCK:
        return [snap for (seen, snap) in LATEST.values()
                if now - seen < config.NODE_STALE_AFTER]


def piece_size(i: int, piece_length: int, total_size: int, num_pieces: int) -> int:
    """Bytes in piece i (the last piece is usually short). Mirrors piece_map."""
    if i < num_pieces - 1:
        return piece_length
    return total_size - piece_length * (num_pieces - 1)


def build_summary() -> dict:
    """The swarm-wide view piece_map.py renders, as render-ready JSON.

    Aggregation comes straight from swarm_stats (the same code piece_map uses),
    so the web UI and the terminal view never drift; only presentation differs.
    Per-node ownership bitfields are included so the UI can draw the piece map;
    holder ids are resolved to display labels here.
    """
    torrents = []
    for meta, rows in swarm_stats.collect_by_torrent(fresh_snapshots()):
        num_pieces = meta["num_pieces"]
        piece_length = meta["piece_length"]
        total_size = meta["total_size"]
        avail = swarm_stats.availability(rows, num_pieces)
        labels = {r["id"]: r["label"] for r in rows}  # node_key -> display label
        min_avail = min(avail) if avail else 0
        total_have = sum(avail)
        full_holders = [r["label"] for r in rows if all(r["bits"])]

        out_rows = []
        for r in rows:
            stored = sum(piece_size(i, piece_length, total_size, num_pieces)
                         for i, b in enumerate(r["bits"]) if b)
            out_rows.append({
                "label": r["label"], "role": r["role"],
                "have": sum(r["bits"]), "stored": stored,
                "bits": [1 if b else 0 for b in r["bits"]],
            })

        files = []
        for f in swarm_stats.per_file(rows, meta["files"], avail):
            files.append({
                "path": f["path"], "size": f["size"],
                "full_copies": f["full_copies"],
                "full_holders": [labels.get(i, i) for i in f["full_holders"]],
                "recon_copies": f["recon_copies"],
                "partial": [{"label": labels.get(i, i), "pct": pct}
                            for i, pct in f["partial"]],
            })

        torrents.append({
            "info_hash": meta["info_hash"], "name": meta["name"],
            "num_pieces": num_pieces, "piece_length": piece_length,
            "total_size": total_size, "nodes_seen": len(rows),
            "rows": out_rows, "avail": avail, "files": files,
            "summary": {
                "full_copies": len(full_holders), "full_holders": full_holders,
                "min_avail": min_avail,
                "redundancy": (total_have / num_pieces) if num_pieces else 0,
                "fully_available": min_avail >= 1,
                "total_stored": total_have * piece_length,
            },
        })
    return {"ts": time.time(), "torrents": torrents}


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, body: bytes, ctype: str = "text/plain", code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/ingest":
                return self._send(b"", code=404)
            try:
                length = int(self.headers.get("Content-Length", 0))
                snap = json.loads(self.rfile.read(length) or b"{}")
                key = snap["node_key"]
            except Exception as exc:
                return self._send(json.dumps({"error": str(exc)}).encode(),
                                  "application/json", 400)
            with LOCK:
                LATEST[key] = (time.time(), snap)
            self._send(b"", code=204)

        def _send_static(self, filename: str, ctype: str) -> None:
            try:
                with open(os.path.join(WEBUI_DIR, filename), "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send(b"web UI not found (is ./webui/ present?)",
                                  code=404)
            self._send(body, ctype)

        def do_GET(self):
            if self.path in STATIC_FILES:
                filename, ctype = STATIC_FILES[self.path]
                self._send_static(filename, ctype)
            elif self.path == "/live":
                body = json.dumps({"ts": time.time(),
                                   "nodes": fresh_snapshots()}).encode()
                self._send(body, "application/json")
            elif self.path == "/summary":
                self._send(json.dumps(build_summary()).encode(), "application/json")
            elif self.path == "/stats":
                now = time.time()
                with LOCK:
                    nodes = [{"node_key": k, "label": s.get("label", k),
                              "age": round(now - seen, 2),
                              "fresh": now - seen < config.NODE_STALE_AFTER}
                             for k, (seen, s) in LATEST.items()]
                self._send(json.dumps({"nodes": nodes}).encode(), "application/json")
            else:
                self._send(b"", code=404)

    return Handler


def main() -> None:
    srv = ThreadingHTTPServer((config.COLLECTOR_HOST, config.COLLECTOR_PORT),
                              make_handler())
    srv.daemon_threads = True
    print(f"collector on http://{config.COLLECTOR_HOST}:{config.COLLECTOR_PORT}/  "
          f"(web UI + ingest + live/summary, in-memory)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
