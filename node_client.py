"""Stdlib client for another node's HTTP API.

No node knows every dataset, and there is no central list either. Publishing
seeds the data in place, so every dataset has a holder from the moment it
exists, and the set of datasets in the swarm is the union of what its nodes
hold. A reader assembles that list from these calls; there is no one to ask for
it.

The node's API is split so that nothing a reader polls grows with how much that
node holds, and these are the four halves of that split: fetch_stats for what a
node is as a whole (including its cursor), fetch_holdings for which datasets it
has — incrementally, following the cursor — fetch_transfers for what is moving,
and fetch_holding for one dataset's piece bitfield. fetch_meta rounds it out with
a dataset's file -> piece map, which any holder can answer.

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


class Resync(Exception):
    """The node cannot answer from the cursor we sent — it has restarted, or the
    transitions we asked about have been trimmed away. Drop the cursor and list
    again; see fetch_holdings."""


def fetch_meta(base: str, info_hash: str, timeout: float = 10.0) -> dict:
    """A dataset's file -> piece map (plus its name, size and piece layout).

    The same on every node and fixed for the life of the dataset — it is what the
    info-hash hashes — so a reader fetches it once and keeps it. Answered only by
    a node that holds the dataset, which is also the only node that has its
    .torrent; the holdings streams say who that is.

    Only the per-file views need this. Everything a list view wants is already in
    the holdings row."""
    try:
        return json.loads(_get(base, f"/dataset/{info_hash}", timeout).decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(info_hash)
        raise


def fetch_holdings(base: str, since: str = None, timeout: float = 30.0) -> dict:
    """Which datasets a node holds: {"cursor", "more", "holdings"}.

    Rows are {info_hash, state, name, total_size, piece_length} — enough to list
    a dataset without asking anyone anything else, which is what lets the union
    of these streams be the list of datasets in the swarm.

    With no cursor this is the first page of everything it holds; with one,
    either the next page or only what has changed since — which is what makes
    following 25 nodes cost nothing while they are idle. `more` says whether to
    call again with the cursor just handed back; holdings_stream does that
    for callers that want the lot without holding it. The cursor is opaque: store it, hand it
    back, never take it apart.

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


def holdings_stream(base: str, timeout: float = 30.0):
    """Everything a node holds, a row at a time, paging to the end.

    In the order the node hands them out, which is info-hash order — the point
    of which is that several of these can be merged without holding any of them:
    a reader wanting the whole swarm takes one page per node rather than every
    node's full listing. Readers that follow the stream instead (collector.py)
    page the same way but keep the cursor they end with, so their next call is a
    delta."""
    cursor = None
    while True:
        page = fetch_holdings(base, since=cursor, timeout=timeout)
        yield from page.get("holdings") or []
        cursor = page["cursor"]
        if not page.get("more"):
            return


def fetch_matching(base: str, ref: str, timeout: float = 10.0) -> list:
    """The rows for datasets this node holds that `ref` names — an exact name or
    an info-hash prefix. How a name becomes an info-hash without reading every
    node's whole stream to find it: one small request per node, answered from
    what that node holds."""
    path = "/holdings?match=" + urllib.parse.quote(ref)
    return json.loads(_get(base, path, timeout).decode()).get("holdings") or []


def fetch_holding(base: str, info_hash: str, timeout: float = 5.0) -> dict:
    """One dataset on one node, with its piece bitfield — or None if not held.

    The only call that costs anything per dataset, which is why it is per
    dataset: the swarm-wide piece map is one of these per node, however many
    datasets there are."""
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

    Answered only by a node holding the dataset: a node keeps torrents for what
    it has and nothing else. Raises FileNotFoundError otherwise, which is how a
    node taking a dataset walks its peers until one serves it."""
    try:
        return _get(base, f"/dataset/{info_hash}.torrent", timeout)
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
    the number of datasets. A caller that wants holdings reads the cursor here
    and calls fetch_holdings when it has moved.

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
