"""Central metrics collector for the distributed swarm (push model).

Nodes POST their /stats snapshot here instead of being polled, so the collector
is the one service that accepts inbound connections — nodes only ever dial out,
which works across data centers / NAT. Nodes are independent and identified by a
per-node UUID (node_key); there is no enumeration, the collector learns nodes as
they push.

Ingest and aggregation are decoupled: each node's rows are written on arrival,
stamped with the collector clock (a single timebase across hosts whose own clocks
differ). Cross-node views — per-file replication and the live map — are computed
over the latest-seen snapshot of each *fresh* node, dropping ones gone silent.

Endpoints:
  POST /ingest   - receive one node's snapshot JSON; persist its rows
  GET  /live     - {"ts", "nodes": [...]} latest snapshot of each fresh node,
                   the live input for piece_map and any other viewer
  GET  /stats    - the collector's own health: nodes seen + last-seen ages

This collector owns the SQLite schema and per-node row writing that the old
pull-based monitor.py used to; monitor.py and its localhost node-scan are gone.
"""
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import swarm_stats

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
  node_key TEXT PRIMARY KEY, label TEXT, first_seen REAL, last_seen REAL);

CREATE TABLE IF NOT EXISTS node_session_stats(
  ts REAL, node_key TEXT, metric TEXT, value REAL);

CREATE TABLE IF NOT EXISTS torrent_status(
  ts REAL, node_key TEXT, info_hash TEXT, state TEXT, progress REAL,
  download_rate INTEGER, upload_rate INTEGER,
  download_payload_rate INTEGER, upload_payload_rate INTEGER,
  total_done INTEGER, total_wanted INTEGER,
  total_download INTEGER, total_upload INTEGER,
  all_time_download INTEGER, all_time_upload INTEGER,
  num_peers INTEGER, num_seeds INTEGER, num_connections INTEGER,
  num_pieces INTEGER, distributed_copies REAL,
  is_seeding INTEGER, is_finished INTEGER);

CREATE TABLE IF NOT EXISTS peer_info(
  ts REAL, node_key TEXT, peer_ip TEXT, client TEXT,
  down_speed INTEGER, up_speed INTEGER,
  payload_down_speed INTEGER, payload_up_speed INTEGER,
  progress REAL, total_download INTEGER, total_upload INTEGER,
  flags INTEGER, source INTEGER, rtt INTEGER);

-- Per-file replication over time: full_copies = nodes holding the whole file,
-- recon_copies = reconstructable copies (replication of the file's rarest piece).
-- Keyed by torrent (info_hash) since a swarm can host many torrents at once.
CREATE TABLE IF NOT EXISTS file_replication(
  ts REAL, info_hash TEXT, torrent_name TEXT, file_path TEXT, size INTEGER,
  num_pieces INTEGER, full_copies INTEGER, recon_copies INTEGER, holders TEXT);

CREATE INDEX IF NOT EXISTS idx_filerepl_path_ts ON file_replication(info_hash, file_path, ts);
CREATE INDEX IF NOT EXISTS idx_torrent_node_ts ON torrent_status(node_key, ts);
CREATE INDEX IF NOT EXISTS idx_session_node_ts ON node_session_stats(node_key, ts);
CREATE INDEX IF NOT EXISTS idx_peer_node_ts ON peer_info(node_key, ts);
"""

LOCK = threading.Lock()
# node_key -> (received_ts, snapshot). The freshest state of every node, in
# memory; the live input for /live and the aggregation tick.
LATEST: dict = {}
DB: sqlite3.Connection = None  # set in main(); one connection, guarded by LOCK


def record_node(db, ts, node_key, label, snap) -> None:
    """Write one node's session/torrent/peer rows, keyed by node_key, plus an
    upsert into `nodes` so reports can show a human label and last-seen age. This
    is the per-node writer the old monitor.py had, minus the live summary line."""
    db.execute(
        "INSERT INTO nodes(node_key, label, first_seen, last_seen) VALUES (?,?,?,?) "
        "ON CONFLICT(node_key) DO UPDATE SET label=excluded.label, last_seen=excluded.last_seen",
        (node_key, label, ts, ts))
    for metric, value in snap.get("session", {}).items():
        db.execute("INSERT INTO node_session_stats VALUES (?,?,?,?)",
                   (ts, node_key, metric, value))
    for t in snap.get("torrents", []):
        db.execute(
            "INSERT INTO torrent_status VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, node_key, t.get("info_hash_v2"), t.get("state"), t.get("progress"),
             t.get("download_rate"), t.get("upload_rate"),
             t.get("download_payload_rate"), t.get("upload_payload_rate"),
             t.get("total_done"), t.get("total_wanted"),
             t.get("total_download"), t.get("total_upload"),
             t.get("all_time_download"), t.get("all_time_upload"),
             t.get("num_peers"), t.get("num_seeds"), t.get("num_connections"),
             t.get("num_pieces"), t.get("distributed_copies"),
             int(bool(t.get("is_seeding"))), int(bool(t.get("is_finished")))))
    for p in snap.get("peers", []):
        db.execute(
            "INSERT INTO peer_info VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, node_key, p.get("ip"), p.get("client"),
             p.get("down_speed"), p.get("up_speed"),
             p.get("payload_down_speed"), p.get("payload_up_speed"),
             p.get("progress"), p.get("total_download"), p.get("total_upload"),
             p.get("flags"), p.get("source"), p.get("rtt")))


def fresh_snapshots(now: float = None) -> list:
    """Latest snapshot of every node still reporting (the shared input for both
    /live and the aggregation tick). Nodes silent past NODE_STALE_AFTER drop out."""
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
            label = snap.get("label", key)
            ts = time.time()  # collector clock = the single timebase across hosts
            with LOCK:
                record_node(DB, ts, key, label, snap)
                DB.commit()
                LATEST[key] = (ts, snap)
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


def aggregate_loop() -> None:
    """Periodic cross-node view: write file_replication rows and a combined
    snapshot file (so piece_map --snapshot keeps working for post-mortem). Runs
    over the latest-seen snapshot of each fresh node — the same set as /live."""
    while True:
        time.sleep(config.POLL_INTERVAL)
        ts = time.time()
        nodes = fresh_snapshots(ts)
        with LOCK:
            for meta, rows in swarm_stats.collect_by_torrent(nodes):
                avail = swarm_stats.availability(rows, meta["num_pieces"])
                for f in swarm_stats.per_file(rows, meta["files"], avail):
                    DB.execute(
                        "INSERT INTO file_replication VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts, meta["info_hash"], meta["name"], f["path"], f["size"],
                         f["num_pieces"], f["full_copies"], f["recon_copies"],
                         ",".join(map(str, f["full_holders"]))))
            DB.commit()
        if nodes:
            with open(os.path.join(config.SNAPSHOT_DIR, f"{ts:.3f}.json"), "w") as fh:
                json.dump({"ts": ts, "nodes": {s["node_key"]: s for s in nodes}}, fh)


def main() -> None:
    global DB
    os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
    DB = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    DB.executescript(SCHEMA)

    threading.Thread(target=aggregate_loop, daemon=True).start()

    srv = ThreadingHTTPServer((config.COLLECTOR_HOST, config.COLLECTOR_PORT),
                              make_handler())
    srv.daemon_threads = True
    print(f"collector on http://{config.COLLECTOR_HOST}:{config.COLLECTOR_PORT}/  "
          f"(ingest + live)  -> {config.DB_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
