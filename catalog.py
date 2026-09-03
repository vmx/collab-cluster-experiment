"""Stdlib client for another node's HTTP API.

There is no central catalog: every node keeps its own directory of .torrent
files and serves it, so "the catalog" is just what some node knows about. These
helpers are how a node pulls a peer's catalog during its sync tick, and how
control.py inspects a node.

Kept free of libtorrent so control.py works without that dependency; callers
that need to parse a .torrent bdecode the bytes themselves.
"""
import json
import urllib.error
import urllib.parse
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


def fetch_peers(base: str, me: dict = None, timeout: float = 5.0) -> dict:
    """A node's view of the swarm: {"self": {...}, "peers": [...]}.

    `self` is that node's own identity, so dialling a single known address is
    enough to learn it *and* everyone it can see — which is how --peer
    bootstrapping works where multicast doesn't reach.

    Pass `me` (a node's own beacon dict) to introduce yourself in the same
    breath. Gossip would otherwise be one-directional: a bootstrapped node would
    learn the whole swarm while remaining invisible to it, so nothing it
    published would ever be noticed. The callee reads our address off the
    connection, exactly as a beacon's is read off its datagram."""
    path = "/peers"
    if me:
        path += "?" + urllib.parse.urlencode(me)
    return json.loads(_get(base, path, timeout).decode())


def fetch_stats(base: str, timeout: float = 2.0) -> dict:
    """A node's live snapshot (the same payload it pushes to the collector)."""
    return json.loads(_get(base, "/stats", timeout).decode())
