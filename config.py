"""Shared configuration for the collab-cluster network.

Single source of truth for ports, paths and timing. There is no central service:
every host runs the same `node.py`, and nodes find each other with a UDP
multicast beacon on the local network. Services can be co-located on one host for
a deterministic dev run or spread across a real network.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
NODES_DIR = os.path.join(BASE_DIR, "nodes")

# Where the built-in sample datasets are generated (make_torrent.py). Nothing
# else lives under data/ any more — each node keeps its own catalog of .torrent
# files under nodes/<id>/catalog/, because there is no central catalog.
SAMPLE_DIR = os.path.join(DATA_DIR, "sample")

# --- Network -----------------------------------------------------------------
# Nodes may live on other hosts, so they bind all interfaces rather than loopback.
BIND_HOST = "0.0.0.0"


BT_PORT_BASE = 6881      # node i listens for BitTorrent on BT_PORT_BASE + i
STATS_PORT_BASE = 8001   # node i serves its HTTP API on STATS_PORT_BASE + i

# --- Peer discovery (the beacon) ---------------------------------------------
# This replaces the tracker entirely. Every node periodically multicasts a tiny
# datagram — who it is, its ports, and a digest of its catalog — to the local
# network, and listens for everyone else's. That single mechanism answers both
# "which nodes exist" and "has anyone published something new", and the peer's
# address comes free as the datagram's source. Nodes then wire peers straight
# into libtorrent with torrent_handle.connect_peer(), so no announce, DHT, PEX
# or LSD is involved anywhere.
#
# The group is a local-scope (administratively scoped) multicast address and the
# beacon goes out with TTL 1, so it never leaves the local segment. See
# beacon.py for the datagram itself; the optional dashboard joins the same group
# to find a node to read through, but only listens.
BEACON_GROUP = os.environ.get("SWARM_BEACON_GROUP") or "239.255.42.1"
BEACON_PORT = int(os.environ.get("SWARM_BEACON_PORT") or 6772)

# --- Torrent (BitTorrent v2 only) --------------------------------------------
PIECE_SIZE = 256 * 1024          # 256 KiB; power of two (v2 requires >= 16 KiB)

# Per-node upload rate cap (bytes/s, 0 = unlimited). Over localhost a transfer is
# otherwise instantaneous; capping it spreads the transfer over time so the live
# rate/progress is actually observable as it happens. 1 MiB/s => ~30s+.
UPLOAD_RATE_LIMIT = 1 * 1024 * 1024

# --- Timing (seconds) --------------------------------------------------------
# How often a node runs its sync tick: beacon out, drain beacons in, pull any
# changed peer catalog, take what it wants, and re-mesh its torrents. This is the
# system's heartbeat — everything converges within a small multiple of it.
BEACON_INTERVAL = 2.0
# Forget a peer we haven't heard a beacon from in this long. Must comfortably
# exceed BEACON_INTERVAL so a single dropped datagram doesn't evict a live node.
PEER_STALE_AFTER = 3 * BEACON_INTERVAL

NODE_LOOP_INTERVAL = 1.0   # how often a node refreshes what it is moving
                           # (only the transfers in flight - see node.session_loop)

# --- Dashboard (optional) ----------------------------------------------------
# Purely observability, and purely a client: it reads the swarm through any one
# node's /peers and /stats. Nodes are not configured for it and never report to
# it, so nothing here is a node setting.
COLLECTOR_HOST = "0.0.0.0"               # bind address (accept browsers)
COLLECTOR_PORT = 8100

# How long a fan-out over the nodes is reused. This is the whole rate limit: the
# nodes are read at most this often no matter how many browsers are watching,
# and not at all while none is.
POLL_TTL = 1.0
# The collector buckets each torrent's pieces into at most this many columns
# before sending (the dashboard only draws that many), so the payload stays small
# no matter how many pieces a torrent has.
WEBUI_MAX_COLS = 120


def bt_port(node_id: int) -> int:
    return BT_PORT_BASE + node_id


def stats_port(node_id: int) -> int:
    return STATS_PORT_BASE + node_id


def parse_endpoint(endpoint: str, default_port: int = STATS_PORT_BASE) -> tuple:
    """Parse a control endpoint "host" or "host:port" into (host, port).

    Nodes are addressed by where they listen, not by an id: on separate hosts
    (or containers) each node has its own IP and can share one control port, so
    the port is optional and defaults to the standard control port. Only when
    several nodes share one IP (a single-host dev run) do you spell out the port.
    IPv4/hostname only — good enough for the private network control runs on."""
    host, sep, port = endpoint.rpartition(":")
    if not sep:
        return endpoint, default_port
    return host, int(port)
