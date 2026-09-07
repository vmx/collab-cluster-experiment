"""Stdlib client for another node's HTTP API.

There is no central catalog: every node keeps its own directory of .torrent
files and serves it, so "the catalog" is just what some node knows about. These
helpers are how a node pulls a peer's catalog during its sync tick, and how
control.py inspects a node.

Kept free of libtorrent so control.py works without that dependency; callers
that need to parse a .torrent bdecode the bytes themselves.
"""
import concurrent.futures
import json
import urllib.error
import urllib.request

import config


def base_url(endpoint: str) -> str:
    """"host" or "host:port" -> "http://host:port" (port defaults to the
    standard control port, see config.parse_endpoint)."""
    host, port = config.parse_endpoint(endpoint)
    return f"http://{host}:{port}"


def _get(base: str, path: str, timeout: float) -> bytes:
    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as r:
        return r.read()


def fetch_list(base: str, timeout: float = 5.0) -> list:
    """A node's catalog: [{"name", "info_hash"}] for every dataset it knows of
    (whether or not it holds the data)."""
    return json.loads(_get(base, "/catalog", timeout).decode())


def fetch_torrent_bytes(base: str, info_hash: str, timeout: float = 10.0) -> bytes:
    """The raw .torrent bytes for one dataset. Addressed by full v2 info-hash —
    never by name, which is only a label and can repeat across datasets.
    Raises FileNotFoundError if that node doesn't have it."""
    try:
        return _get(base, f"/catalog/{info_hash}.torrent", timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(info_hash)
        raise


def fetch_peers(base: str, timeout: float = 5.0) -> dict:
    """A node's view of the swarm: {"self": {...}, "peers": [...]}.

    `self` is that node's own identity, so asking a single node names the whole
    swarm — which is what lets any viewer read every node through one of them."""
    return json.loads(_get(base, "/peers", timeout).decode())


def fetch_stats(base: str, timeout: float = 2.0) -> dict:
    """A node's live snapshot: per-torrent status and piece ownership."""
    return json.loads(_get(base, "/stats", timeout).decode())


def fetch_swarm(base: str, timeout: float = 2.0) -> tuple:
    """Every node's snapshot, gathered through one node's peer table.

    Returns (snapshots, addresses). One /peers call names the whole swarm, then
    each node is asked for its own /stats — the same payload it would have to
    publish anyway. This is all any swarm-wide view needs, which is why neither
    control.py nor collector.py has to be told about nodes, or nodes about them.

    A node that doesn't answer is simply absent: liveness is "responded", not a
    staleness timer. `addresses` is every endpoint we tried, so a caller can
    fall back to one of them when `base` itself stops answering.
    """
    view = fetch_peers(base, timeout=timeout)
    # The node we asked doesn't know its own address — we do, we just dialled it.
    bases = [base] + [f"http://{p['ip']}:{p['http']}"
                      for p in view.get("peers") or []
                      if p.get("ip") and p.get("http")]
    snaps = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for base_i, snap in zip(bases, pool.map(lambda b: _stats_or_none(b, timeout),
                                                bases)):
            if snap and snap.get("node_key"):
                # Label a node by the address we reached it at — the address you
                # would type into control.py. A node cannot do this itself: it
                # never learns its own address (that is the point of the beacon).
                snap["label"] = base_i.split("//", 1)[-1]
                snaps.setdefault(snap["node_key"], snap)
    return list(snaps.values()), bases


def _stats_or_none(base: str, timeout: float):
    try:
        return fetch_stats(base, timeout)
    except Exception:
        return None
