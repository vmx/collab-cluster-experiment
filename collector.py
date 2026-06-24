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
  GET  /stats    - the collector's own health: nodes seen + last-seen ages
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

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

        def do_GET(self):
            if self.path == "/live":
                body = json.dumps({"ts": time.time(),
                                   "nodes": fresh_snapshots()}).encode()
                self._send(body, "application/json")
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
          f"(ingest + live, in-memory)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
