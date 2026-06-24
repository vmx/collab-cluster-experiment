"""Shared configuration for the BitTorrent v2 prototype network.

Single source of truth for ports, paths, counts and timing. Everything runs on
localhost so the experiment is deterministic and easy to restart.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
NODES_DIR = os.path.join(BASE_DIR, "nodes")

# Catalog of created torrents. make_torrent.py writes one pair per torrent here:
#   data/torrents/<name>.torrent  + data/torrents/<name>.json (sidecar meta).
# The sidecar records the torrent path and the seed's save_path (the parent dir
# of the shared content) so nodes can serve arbitrary local files/directories
# without hard-coding their location. Nodes/control resolve a torrent by <name>.
TORRENTS_DIR = os.path.join(DATA_DIR, "torrents")
# Where the built-in sample datasets are generated when no content is given.
SAMPLE_DIR = os.path.join(DATA_DIR, "sample")

# --- Network -----------------------------------------------------------------
HOST = "127.0.0.1"

BT_PORT_BASE = 6881      # node i listens for BitTorrent on BT_PORT_BASE + i
STATS_PORT_BASE = 8001   # node i serves its /stats JSON on STATS_PORT_BASE + i

# --- Metrics collector (push model) ------------------------------------------
# Nodes POST their /stats snapshot to a central collector rather than being
# polled. The collector is the one service that accepts inbound connections;
# nodes only ever dial out (NAT/firewall-friendly), so this scales to nodes
# spread across separate data centers. Identity is a per-node UUID (node_key);
# there is no node enumeration, the collector learns nodes as they push.
COLLECTOR_HOST = "0.0.0.0"               # bind address (accept remote nodes)
COLLECTOR_PORT = 8100
COLLECTOR_BASE = f"http://{HOST}:{COLLECTOR_PORT}"   # where nodes/viewers reach it
COLLECTOR_URL = f"{COLLECTOR_BASE}/ingest"           # node push target (--collector default)

# --- Tracker-based discovery (our own tracker) -------------------------------
# Discovery is tracker-driven: torrents are built with this announce URL baked in
# and marked private (which disables PEX/DHT/LSD), so peers find each other purely
# by announcing to the tracker. The tracker is our own tiny stdlib script
# (bittorrent_tracker.py) — it accepts any info-hash (no whitelist) and also
# serves the torrent catalog, so a node needs only this URL to obtain both peers
# and the .torrent itself.
TRACKER_PORT = 8000
TRACKER_URL = f"http://{HOST}:{TRACKER_PORT}/announce"
TRACKER_BASE = f"http://{HOST}:{TRACKER_PORT}"

# --- Torrent (BitTorrent v2 only) --------------------------------------------
PIECE_SIZE = 256 * 1024          # 256 KiB; power of two (v2 requires >= 16 KiB)

# Per-node upload rate cap (bytes/s, 0 = unlimited). Over localhost a transfer is
# otherwise instantaneous; capping it spreads the transfer over time so the live
# rate/progress is actually observable as it happens. 1 MiB/s => ~30s+.
UPLOAD_RATE_LIMIT = 1 * 1024 * 1024

# --- Timing (seconds) --------------------------------------------------------
# Tracker announce interval. libtorrent enforces a 300s floor on re-announce by
# default (min_announce_interval), so without lowering it a node started later
# would wait ~5 min before the tracker pairs it with the others. Keep it short so
# the localhost swarm meshes quickly regardless of start order.
ANNOUNCE_INTERVAL = 5
NODE_LOOP_INTERVAL = 1.0   # how often a node refreshes its stats snapshot
PUSH_INTERVAL = 1.0        # how often a node POSTs its snapshot to the collector
# Drop a node from the live view after this much silence (it stopped
# pushing). Mirrors the tracker's reap window: comfortably more than PUSH_INTERVAL.
NODE_STALE_AFTER = 3 * PUSH_INTERVAL


def bt_port(node_id: int) -> int:
    return BT_PORT_BASE + node_id


def stats_port(node_id: int) -> int:
    return STATS_PORT_BASE + node_id
