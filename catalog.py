"""Stdlib client for another node's HTTP API.

There is no central catalog: every node keeps its own directory of .torrent
files and serves it, so "the catalog" is just what some node knows about. These
helpers are how a node pulls a peer's catalog during its sync tick, and how
control.py and the dashboard inspect a node.

The node's API is split so that nothing a reader polls grows with how much that
node holds, and these are the four halves of that split: fetch_stats for what a
node is as a whole (including its cursor), fetch_holdings for which datasets it
has — incrementally, following the cursor — fetch_transfers for what is moving,
and fetch_holding for one dataset's piece bitfield. fetch_meta rounds it out with
the static shape of a dataset, which belongs to the catalog because it is the
same on every node.

Kept free of libtorrent so control.py works without that dependency; callers
that need to parse a .torrent bdecode the bytes themselves — which is also why
fetch_meta exists, since only a node has libtorrent to read a .torrent with.
"""
import concurrent.futures
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


class Resync(Exception):
    """The node cannot answer from the cursor we sent — it has restarted, or the
    transitions we asked about have been trimmed away. Drop the cursor and list
    again; see fetch_holdings."""


def fetch_meta(base: str, info_hash: str, timeout: float = 10.0) -> dict:
    """A dataset's static shape: name, size, piece layout, file -> piece ranges.

    The same on every node and fixed for the life of the dataset (it is what the
    info-hash hashes), so a reader fetches it once and keeps it. Nodes used to
    ship it with every torrent in every snapshot, which meant one identical copy
    per node per poll."""
    try:
        return json.loads(_get(base, f"/catalog/{info_hash}", timeout).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(info_hash)
        raise


def fetch_holdings(base: str, since: str = None, timeout: float = 30.0) -> dict:
    """Which datasets a node holds: {"cursor", "more", "holdings"}.

    With no cursor this is everything it holds; with one, only what has changed
    since — which is what makes following 25 nodes cost nothing while they are
    idle. Either way the response carries the cursor to send next time. It is
    opaque: store it, hand it back, never take it apart.

    Raises Resync when the node says it cannot answer from that cursor. That is
    not an error but the mechanism working: call again with since=None and take
    the full list. The node can only tell us because the cursor it validates is
    the whole thing it issued."""
    path = "/holdings"
    if since:
        path += "?since=" + urllib.parse.quote(since)
    try:
        return json.loads(_get(base, path, timeout).decode())
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise Resync(since)
        raise


def fetch_holding(base: str, info_hash: str, timeout: float = 5.0) -> dict:
    """One dataset on one node, with its piece bitfield — or None if not held.

    The only call that costs anything per dataset, which is why it is per
    dataset: the swarm-wide piece map is one of these per node, whatever the
    catalog holds."""
    try:
        return json.loads(_get(base, f"/holdings/{info_hash}", timeout).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_transfers(base: str, timeout: float = 5.0) -> list:
    """What a node is moving right now, one row per in-flight dataset, with live
    progress and rates. Bounded by what is in flight rather than what is held,
    so it stays cheap to poll."""
    data = json.loads(_get(base, "/transfers", timeout).decode())
    return data.get("transfers") or []


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
    """What a node is as a whole: disk, how much it holds and stores, throughput,
    and its cursor. Constant size — nothing here is per dataset, which is what
    makes it the thing to poll every second. When the cursor moves, and only
    then, there is any point calling fetch_holdings."""
    return json.loads(_get(base, "/stats", timeout).decode())


def fetch_swarm(base: str, timeout: float = 2.0) -> tuple:
    """Every node's /stats, gathered through one node's peer table.

    Returns (stats, addresses). One /peers call names the whole swarm, then each
    node is asked what it is — which is why neither control.py nor collector.py
    has to be told about nodes, or nodes about them.

    Only the constant-size part: what each node *holds* is followed separately
    through its cursor, because that is the part that would otherwise grow with
    the catalog. A caller that wants holdings reads the cursor here and calls
    fetch_holdings when it has moved.

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
