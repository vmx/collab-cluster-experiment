"""Shared configuration for the BitTorrent v2 prototype network.

Single source of truth for ports, paths, counts and timing. Services can be
co-located on one host for a deterministic dev run or spread across a real
network; the BitTorrent peer layer advertises routable addresses either way.
"""
import os
import socket

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
NODES_DIR = os.path.join(BASE_DIR, "nodes")

# Catalog of created torrents. make_torrent.py writes one file per torrent here:
#   data/torrents/<name>.torrent
# The .torrent is the only source of truth — nodes/control resolve a torrent by
# <name> and read its name + info-hash by parsing it; there is no sidecar.
TORRENTS_DIR = os.path.join(DATA_DIR, "torrents")
# Where the built-in sample datasets are generated when no content is given.
SAMPLE_DIR = os.path.join(DATA_DIR, "sample")

# --- Network -----------------------------------------------------------------
# HOST is the loopback address for reaching co-located services (tracker,
# collector, a node's control server) in a single-host dev run.
HOST = "127.0.0.1"

# Peers, unlike those control endpoints, may live on other hosts, so the
# BitTorrent layer binds and advertises a real routable address instead of
# loopback. BIND_HOST is what a node listens on (all interfaces, so remote peers
# can connect); ADVERTISE_IP is the address it announces to the tracker and
# reports to the collector so peers dial it on a routable address.
BIND_HOST = "0.0.0.0"


def primary_ip() -> str:
    """This host's primary outbound IPv4 — the address other hosts reach it on.

    Opens a throwaway UDP socket toward a public address (a UDP connect sends no
    packet) and reads back the local endpoint the OS picked as the source, which
    is the routable interface even on a multi-homed host. Falls back to loopback
    when the host is offline, e.g. a self-contained single-machine dev run."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# Auto-detected per host; override with SWARM_ADVERTISE_IP on a multi-homed or
# NAT host that must advertise a specific address to the rest of the swarm.
ADVERTISE_IP = os.environ.get("SWARM_ADVERTISE_IP") or primary_ip()

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
# Address nodes/viewers reach the collector at (its bind is COLLECTOR_HOST above).
# Loopback for a single-host run; set SWARM_COLLECTOR to the routable address
# (e.g. the host's bridge IP) when nodes push from other hosts or containers.
COLLECTOR_REACH_HOST = os.environ.get("SWARM_COLLECTOR") or HOST
COLLECTOR_BASE = f"http://{COLLECTOR_REACH_HOST}:{COLLECTOR_PORT}"   # where nodes/viewers reach it
COLLECTOR_URL = f"{COLLECTOR_BASE}/api/ingest"       # node push target (--collector default)

# Web UI /summary tuning. The collector buckets each torrent's pieces into at most
# this many columns before sending (the dashboard only draws that many), so the
# payload stays small no matter how many pieces a torrent has. The built summary
# is cached for SUMMARY_TTL (defined with the timing constants below) so a crowd
# of viewers shares one recompute per tick instead of one per request.
WEBUI_MAX_COLS = 120

# --- Tracker-based discovery (our own tracker) -------------------------------
# Discovery is tracker-driven: torrents are built with this announce URL baked in
# and marked private (which disables PEX/DHT/LSD), so peers find each other purely
# by announcing to the tracker. The tracker is our own tiny stdlib script
# (bittorrent_tracker.py) — it accepts any info-hash (no whitelist) and also
# serves the torrent catalog, so a node needs only this URL to obtain both peers
# and the .torrent itself.
TRACKER_PORT = 6969      # de-facto standard BitTorrent tracker port
TRACKER_BIND = BIND_HOST # bind on all interfaces so remote nodes/containers reach it
# Address nodes/control reach the tracker at, and the announce URL baked into
# torrents at build time. On a distributed run set SWARM_TRACKER to the routable
# address before building torrents; defaults to loopback for a single-host run.
# (make_torrent.py's --tracker overrides the baked announce URL per build.)
TRACKER_HOST = os.environ.get("SWARM_TRACKER") or HOST
TRACKER_BASE = f"http://{TRACKER_HOST}:{TRACKER_PORT}"
TRACKER_URL = f"{TRACKER_BASE}/announce"

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

# Cache the built /summary for one push interval: new node data only lands that
# often, so recomputing more often than this just burns CPU on identical input.
SUMMARY_TTL = PUSH_INTERVAL

# How often the collector re-polls the tracker's /stats to refresh swarm
# membership for the reconciliation view. Membership changes at announce cadence,
# so tracking that interval keeps the collector's picture within one announce.
TRACKER_POLL_INTERVAL = ANNOUNCE_INTERVAL


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
