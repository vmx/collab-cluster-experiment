"""Show the current peer-connection topology of the swarm: which nodes are
connected to which, per torrent, and which side dialed. Polls every node's live
/stats.

    python topology.py
    watch -n 1 python topology.py

Reading the graph:
- Nodes are identified by their listen port (127.0.0.1:6881+id); in this
  localhost setup libtorrent reports a peer's advertised listen endpoint, so both
  ends of a connection are identifiable.
- Direction comes from the `incoming` source bit: an inbound connection is
  flagged `incoming`, so we read each connection from the *dialer* (non-incoming)
  side. That yields exactly one directed edge per connection — "n1 -> n0" means
  node 1 dialed node 0.
- The `src:` bits are libtorrent's view of where a peer is known from. With
  tracker-only discovery this is `tracker` (the dialing side learned the peer
  from a tracker announce).

Localhost only: identifying nodes by port assumes the 6881+id scheme.
"""
import json
import urllib.request

import config

PORT_TO_ID = {config.bt_port(i): i for i in range(config.NUM_NODES)}


def fetch_stats(node_id: int):
    url = f"http://{config.HOST}:{config.stats_port(node_id)}/stats"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def peer_node_id(ip: str):
    """Map a peer endpoint "host:port" to a node id via its listen port."""
    host, _, port = ip.rpartition(":")
    if host != config.HOST:
        return None
    try:
        return PORT_TO_ID.get(int(port))
    except ValueError:
        return None


def main() -> None:
    snaps = {i: fetch_stats(i) for i in range(config.NUM_NODES)}
    up = {i: s for i, s in snaps.items() if s}
    if not up:
        print("no nodes responding (are they running?)")
        return

    by_torrent: dict = {}   # name -> list of (dialer_id, dialee_id, src_bits)
    holds: dict = {}        # name -> set of node ids holding it
    for nid, snap in up.items():
        for t in snap.get("torrents", []):
            holds.setdefault(t.get("name"), set()).add(nid)
            by_torrent.setdefault(t.get("name"), [])
        for p in snap.get("peers", []):
            flags = p.get("source_flags") or []
            if "incoming" in flags:
                continue           # read this connection from the dialer's side
            dst = peer_node_id(p.get("ip", ""))
            if dst is None or dst == nid:
                continue           # foreign peer, or a self-dial — ignore
            src_bits = [f for f in flags if f != "incoming"]
            by_torrent.setdefault(p.get("torrent"), []).append((nid, dst, src_bits))

    print(f"=== swarm topology ({len(up)}/{config.NUM_NODES} nodes responding) ===")
    # Count a node's connections over *unique* undirected pairs: a brief
    # both-dialed-each-other race produces reciprocal edges but is one connection.
    links: set = set()
    for name in sorted(by_torrent):
        members = sorted(holds.get(name, set()))
        edges = sorted(by_torrent[name])
        print(f"\ntorrent '{name}'  (nodes: "
              f"{', '.join(f'n{m}' for m in members) or '-'})")
        if not edges:
            print("  (no connections yet)")
        for src, dst, src_bits in edges:
            bits = ",".join(src_bits) or "-"
            print(f"  n{src} -> n{dst}   src:{bits}")
            links.add((name, frozenset((src, dst))))

    if links:
        deg: dict = {}
        for _name, pair in links:
            for n in pair:
                deg[n] = deg.get(n, 0) + 1
        print("\nconnections per node: "
              + "  ".join(f"n{n}:{deg.get(n, 0)}" for n in sorted(up)))


if __name__ == "__main__":
    main()
