"""Shared configuration for the BitTorrent v2 prototype network.

Single source of truth for ports, paths, counts and timing. Everything runs on
localhost so the experiment is deterministic and easy to restart.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
NODES_DIR = os.path.join(BASE_DIR, "nodes")
STATS_DIR = os.path.join(BASE_DIR, "stats")
SNAPSHOT_DIR = os.path.join(STATS_DIR, "snapshots")
DB_PATH = os.path.join(STATS_DIR, "monitor.db")

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
NUM_NODES = 5  # how many node stats ports the monitor/control scan; nodes are
               # started by hand and are roleless until told what to do.

BT_PORT_BASE = 6881      # node i listens for BitTorrent on BT_PORT_BASE + i
STATS_PORT_BASE = 8001   # node i serves its /stats JSON on STATS_PORT_BASE + i

# --- Trackerless discovery (introducer bootstrap + PEX) ----------------------
# There is no central tracker. A newly joining torrent dials these "introducer"
# peers to enter the swarm; once connected, PEX (peer exchange) gossips the rest
# of the peers between them. Nothing here is a special node — every peer is a
# valid introducer — so list a few for resilience: any one that's reachable is
# enough to bootstrap, and a down introducer is simply swapped for the next.
# Once a node has joined even once, fast-resume + PEX mean it no longer needs an
# introducer; they only matter for a cold, from-zero join.
#
# IMPORTANT (multi-torrent): PEX is per-torrent, so each torrent needs at least
# one introducer that is itself a member of THAT torrent's swarm. A single
# introducer that doesn't hold a given torrent cannot bootstrap it. The simplest
# setup is to treat one node as a rendezvous and add every torrent to it (the
# role the tracker used to play). Order doesn't matter: a torrent with no peers
# re-dials its introducers periodically (node.py BOOTSTRAP_EVERY), so it meshes
# as soon as an introducer joins that swarm.
#
# Each entry is either a node id (localhost: the address is derived from its
# bt_port) or an explicit "host:port" string (cross-machine / VPN, where an id
# can't map to an address — e.g. "10.0.0.5:6881").
INTRODUCERS = [0]

# --- Torrent (BitTorrent v2 only) --------------------------------------------
PIECE_SIZE = 256 * 1024          # 256 KiB; power of two (v2 requires >= 16 KiB)

# Per-node upload rate cap (bytes/s, 0 = unlimited). Over localhost a transfer is
# otherwise instantaneous; capping it spreads the transfer over time so the
# monitor captures a meaningful rate/progress time-series. 1 MiB/s => ~30s+.
UPLOAD_RATE_LIMIT = 1 * 1024 * 1024

# --- Timing (seconds) --------------------------------------------------------
POLL_INTERVAL = 1.0        # how often the monitor polls every node
NODE_LOOP_INTERVAL = 1.0   # how often a node refreshes its stats snapshot


def bt_port(node_id: int) -> int:
    return BT_PORT_BASE + node_id


def stats_port(node_id: int) -> int:
    return STATS_PORT_BASE + node_id


def introducer_addrs(self_id: int = None) -> list:
    """Resolve INTRODUCERS to (host, port) tuples, skipping this node itself.

    Int entries are localhost node ids (address derived from bt_port); string
    entries are explicit "host:port" for cross-machine/VPN setups.
    """
    addrs = []
    for entry in INTRODUCERS:
        if isinstance(entry, int):
            if entry == self_id:
                continue
            addrs.append((HOST, bt_port(entry)))
        else:
            host, _, port = entry.rpartition(":")
            addrs.append((host, int(port)))
    return addrs
