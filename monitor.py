"""Monitoring service: poll every node's /stats, persist all of it to SQLite
time-series tables plus per-poll JSON snapshots.

There is no tracker to poll — discovery is trackerless (introducer + PEX), so
the swarm's makeup is reconstructed purely from each node's peer list, and the
summary shows how many peers each node found via PEX.

Runs as its own process. Prints a one-line per-node summary each poll so the
swarm's progress is visible live.
"""
import json
import os
import sqlite3
import time
import urllib.request

import config
import swarm_stats

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_session_stats(
  ts REAL, node_id INTEGER, metric TEXT, value REAL);

CREATE TABLE IF NOT EXISTS torrent_status(
  ts REAL, node_id INTEGER, info_hash TEXT, state TEXT, progress REAL,
  download_rate INTEGER, upload_rate INTEGER,
  download_payload_rate INTEGER, upload_payload_rate INTEGER,
  total_done INTEGER, total_wanted INTEGER,
  total_download INTEGER, total_upload INTEGER,
  all_time_download INTEGER, all_time_upload INTEGER,
  num_peers INTEGER, num_seeds INTEGER, num_connections INTEGER,
  num_pieces INTEGER, distributed_copies REAL,
  is_seeding INTEGER, is_finished INTEGER);

CREATE TABLE IF NOT EXISTS peer_info(
  ts REAL, node_id INTEGER, peer_ip TEXT, client TEXT,
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
CREATE INDEX IF NOT EXISTS idx_torrent_node_ts ON torrent_status(node_id, ts);
CREATE INDEX IF NOT EXISTS idx_session_node_ts ON node_session_stats(node_id, ts);
CREATE INDEX IF NOT EXISTS idx_peer_node_ts ON peer_info(node_id, ts);
"""


def fetch(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def record_node(db, ts, node_id, snap, summary):
    for metric, value in snap.get("session", {}).items():
        db.execute("INSERT INTO node_session_stats VALUES (?,?,?,?)",
                   (ts, node_id, metric, value))
    # Count peers discovered via PEX, per torrent, for the live summary (the
    # trackerless discovery signal). The raw source bits are stored per peer below.
    pex_by_torrent = {}
    for p in snap.get("peers", []):
        if "pex" in (p.get("source_flags") or []):
            name = p.get("torrent")
            pex_by_torrent[name] = pex_by_torrent.get(name, 0) + 1
    for t in snap.get("torrents", []):
        db.execute(
            "INSERT INTO torrent_status VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, node_id, t.get("info_hash_v2"), t.get("state"), t.get("progress"),
             t.get("download_rate"), t.get("upload_rate"),
             t.get("download_payload_rate"), t.get("upload_payload_rate"),
             t.get("total_done"), t.get("total_wanted"),
             t.get("total_download"), t.get("total_upload"),
             t.get("all_time_download"), t.get("all_time_upload"),
             t.get("num_peers"), t.get("num_seeds"), t.get("num_connections"),
             t.get("num_pieces"), t.get("distributed_copies"),
             int(bool(t.get("is_seeding"))), int(bool(t.get("is_finished")))))
        pex = pex_by_torrent.get(t.get("name"), 0)
        summary.append(
            f"n{node_id}/{t.get('name')}:{(t.get('progress') or 0) * 100:3.0f}% "
            f"▲{(t.get('upload_rate') or 0) // 1024}K "
            f"▼{(t.get('download_rate') or 0) // 1024}K "
            f"p{t.get('num_peers') or 0}" + (f"·pex{pex}" if pex else ""))
    for p in snap.get("peers", []):
        db.execute(
            "INSERT INTO peer_info VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, node_id, p.get("ip"), p.get("client"),
             p.get("down_speed"), p.get("up_speed"),
             p.get("payload_down_speed"), p.get("payload_up_speed"),
             p.get("progress"), p.get("total_download"), p.get("total_upload"),
             p.get("flags"), p.get("source"), p.get("rtt")))


def main() -> None:
    os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
    db = sqlite3.connect(config.DB_PATH)
    db.executescript(SCHEMA)

    node_urls = {i: f"http://{config.HOST}:{config.stats_port(i)}/stats"
                 for i in range(config.NUM_NODES)}
    print(f"monitor: polling {config.NUM_NODES} nodes every "
          f"{config.POLL_INTERVAL}s -> {config.DB_PATH}", flush=True)

    while True:
        ts = time.time()
        combined = {"ts": ts, "nodes": {}}
        summary = []

        for node_id, url in node_urls.items():
            snap = fetch(url)
            if not snap:
                summary.append(f"n{node_id}:--")
                continue
            combined["nodes"][node_id] = snap
            record_node(db, ts, node_id, snap, summary)

        # Per-file replication needs all nodes' bitfields combined for this poll,
        # grouped per torrent (a node may hold several at once).
        for meta, rows in swarm_stats.collect_by_torrent(list(combined["nodes"].values())):
            avail = swarm_stats.availability(rows, meta["num_pieces"])
            for f in swarm_stats.per_file(rows, meta["files"], avail):
                db.execute("INSERT INTO file_replication VALUES (?,?,?,?,?,?,?,?,?)",
                           (ts, meta["info_hash"], meta["name"], f["path"], f["size"],
                            f["num_pieces"], f["full_copies"], f["recon_copies"],
                            ",".join(map(str, f["full_holders"]))))

        db.commit()
        with open(os.path.join(config.SNAPSHOT_DIR, f"{ts:.3f}.json"), "w") as f:
            json.dump(combined, f)
        print("  ".join(summary), flush=True)
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
