"""The swarm-wide index: who holds what, and how many copies of each dataset
exist. Kept by following every node's holdings stream; queried by the
dashboard, and by anything else that needs a swarm-wide view.

Entirely a client: it reads the whole swarm through any one node's peer table
(node_client.fetch_swarm), and finds that node the way nodes find each other —
by listening to the multicast beacon. So it is told no addresses and configured
with nothing, exactly like the nodes it watches. Nor are they configured for it:
they do not report to it and cannot tell whether anyone is watching, since it
only ever listens and never beacons back. The same relationship control.py has.

Everything here is derived from what the nodes say. There is nothing else to
ask: the nodes are the only thing that knows who holds what, and their API is
already public, so there is nothing for an index to be *sent*. Anyone can run
one, and two of them agree because they read the same nodes.

A refresh is deliberately not a snapshot of everything. Each node is asked three
things, and only the first grows with nothing at all:

  /stats      what the node is as a whole, including its cursor. Constant size.
  /holdings   which datasets it has — but only when its cursor has moved, and
              then only the transitions since the last one we saw.
  /transfers  what is moving right now, bounded by what is in flight.

A list of every dataset is not among them, because no node has one to give: a
dataset exists because some node holds it, so unioning the holdings streams both
lists the datasets and counts their copies. That also makes this the only place
where such a list exists at all, and the place where the difference between
"nobody holds this any more" and "the node that holds it is down" would be
settled, since only something watching over time can tell them apart.

So a settled swarm costs one small request per node, and a busy one costs the
changes and nothing else. Those changes are folded into the aggregate below as
they arrive rather than kept per node and reassembled per request — that state
is the price of the cursor, and it is what makes this affordable on a swarm
holding far more than it moves.

The nodes are read at most once per POLL_TTL however many readers ask — and not
at all while none does. A node that doesn't answer is simply absent, but its
cursor is kept, so a node that blips does not cost a full re-list.

The queries at the bottom take the lock themselves and hand back plain data,
with holders as node keys, so no reader touches the aggregate directly.
"""
import concurrent.futures
import heapq
import random
import select
import threading
import time

import beacon
import config
import node_client

# A node named on the command line, for where multicast doesn't reach. Normally
# empty: any node will do, so the beacon picks one. Whichever way it is found,
# it is a way in and not a source of truth — every node knows the whole swarm.
SEED = ""

_HEARD_LOCK = threading.Lock()
# node_key -> {"base", "at"} — nodes heard beaconing lately. This stands in for
# the configuration the index doesn't have: it listens where the nodes announce
# themselves, and any one of them is a way in.
_HEARD: dict = {}

_POLL_LOCK = threading.Lock()
# When we last fanned out, and every node address it reached — which is what lets
# us carry on when our way in goes away.
_POLL: dict = {"at": 0.0, "bases": []}
# node_key -> {"base", "addr", "name", "stats", "cursor", "transfers", "live", "seen"}
# — what we know about each node, carried between polls. `cursor` is where we
# are in that node's stream, opaque and handed straight back. What it *holds*
# is not kept here but folded into the aggregate below.
_NODES: dict = {}

# How long a node has to stay silent before its copies stop counting. A poll it
# misses is a blip and costs nothing; longer than this and it is treated as gone,
# which means listing in full when it returns.
GONE_AFTER = 15.0

# --- the aggregate, maintained rather than rebuilt ----------------------------
# The union of what the nodes hold. It is folded together as they report changes
# instead of being reassembled on every request, because the nodes send changes
# and rebuilding from them throws that away.
#
# A dataset's identity is the same on every node that holds it — it is what the
# info-hash hashes — so it is stored once, and who holds it is a list of node
# ids rather than a row per node per dataset. The two indexes answer the two
# questions the readers actually ask.

_AGG_LOCK = threading.Lock()
_NODE_ID: dict = {}      # node_key -> small int, stable for this process
_NODE_KEY: dict = {}     # and back again
# info_hash -> [name, total_size, piece_length, complete ids, partial ids]
_DATASETS: dict = {}
# copies -> the datasets that have that many. The list view is ordered by this,
# so its first page is a walk of the first few entries, and "how many datasets
# are down to one copy" is a length rather than a scan.
_BY_COPIES: dict = {}
# node id -> the datasets it holds: the transpose of the holder lists above. It
# is what makes a node going away, or re-listing itself, cost what that node
# holds rather than a pass over the whole aggregate.
_HELD_BY: dict = {}
# Bytes held complete across the swarm, moved as copies come and go. A total
# nobody has to add up is the difference between a summary that costs nothing
# and one that costs a pass over every dataset.
_STORED = 0


def start(seed: str = "") -> None:
    """Start listening for nodes; `seed` is a way in for where multicast
    doesn't reach."""
    global SEED
    SEED = seed
    threading.Thread(target=listen_for_nodes, daemon=True).start()


def listen_for_nodes() -> None:
    """Track the nodes announcing themselves on the local segment, forever.

    The index finds its way into the swarm exactly as a node does, which is
    why it needs no address, and why it picks itself back up when the node it
    happened to be reading through goes away. It only ever listens: it sends no
    beacon, so no node learns it exists and nothing in the swarm changes because
    someone is watching."""
    try:
        sock = beacon.make_socket()
    except OSError as exc:
        # No multicast here (offline host, restricted network). Not fatal, but
        # then the only way in is the one given on the command line.
        print(f"no beacon ({exc}); name a node to read the swarm through",
              flush=True)
        return
    while True:
        # Wakes as soon as a beacon lands; the timeout is only so that on a
        # silent segment we still get around to forgetting nodes.
        select.select([sock], [], [], config.BEACON_INTERVAL)
        now = time.time()
        with _HEARD_LOCK:
            for msg, ip in beacon.drain(sock):
                if msg.get("node") and msg.get("http"):
                    _HEARD[msg["node"]] = {"at": now,
                                           "base": f"http://{ip}:{msg['http']}"}
            for key in [k for k, e in _HEARD.items()
                        if now - e["at"] > config.PEER_STALE_AFTER]:
                del _HEARD[key]


def ways_in() -> list:
    """Every address worth trying as a way into the swarm, best first.

    Any node will do, so this is a list of candidates rather than a setting: the
    one named on the command line (if any), then the nodes the last successful
    fan-out reached, then whatever the beacon has heard lately. The last is why
    the index normally needs no address at all; the middle one is what keeps
    it reading through a node whose beacons we happen to be missing."""
    with _HEARD_LOCK:
        heard = [e["base"] for e in sorted(_HEARD.values(),
                                           key=lambda e: -e["at"])]
    out = []
    for base in ([SEED] if SEED else []) + _POLL["bases"] + heard:
        if base not in out:
            out.append(base)
    return out


# --- keeping the aggregate ----------------------------------------------------
# Every change to the three structures above goes through here, under _AGG_LOCK,
# so the one invariant — that they agree with the rows that produced them — has
# a single home.

def node_id(key: str) -> int:
    """A node's place in the holder lists. Caller holds _AGG_LOCK."""
    if key not in _NODE_ID:
        _NODE_ID[key] = len(_NODE_ID)
        _NODE_KEY[_NODE_ID[key]] = key
    return _NODE_ID[key]


def _recount(info_hash: str, before: int, after: int) -> None:
    global _STORED
    if before != after:
        _BY_COPIES.get(before, set()).discard(info_hash)
        _BY_COPIES.setdefault(after, set()).add(info_hash)
        ds = _DATASETS.get(info_hash)
        if ds:
            _STORED += (after - before) * ds[1]


def _without(info_hash: str, ds: list, nid: int) -> None:
    """Take one node out of a dataset's holders, and forget the dataset if that
    was the last of them: nobody holds it, so it has left the swarm."""
    before = len(ds[3])
    ds[3] = tuple(i for i in ds[3] if i != nid)
    ds[4] = tuple(i for i in ds[4] if i != nid)
    _recount(info_hash, before, len(ds[3]))
    if not ds[3] and not ds[4]:
        _DATASETS.pop(info_hash, None)
        _BY_COPIES.get(0, set()).discard(info_hash)


def apply_rows(nid: int, rows: list) -> None:
    """Fold one node's holdings rows in. Caller holds _AGG_LOCK."""
    held = _HELD_BY.setdefault(nid, set())
    for row in rows:
        info_hash, state = row["info_hash"], row.get("state")
        ds = _DATASETS.get(info_hash)
        if ds is None:
            if state == "gone":
                continue
            ds = _DATASETS[info_hash] = [row.get("name") or "",
                                         int(row.get("total_size") or 0),
                                         int(row.get("piece_length") or 0), (), ()]
            _BY_COPIES.setdefault(0, set()).add(info_hash)
        if state == "gone":
            held.discard(info_hash)
            _without(info_hash, ds, nid)
            continue
        held.add(info_hash)
        before = len(ds[3])
        ds[3] = tuple(i for i in ds[3] if i != nid)
        ds[4] = tuple(i for i in ds[4] if i != nid)
        if state == "complete":
            ds[3] += (nid,)
        else:
            ds[4] += (nid,)
        _recount(info_hash, before, len(ds[3]))


def drop_node(nid: int) -> None:
    """Forget everything one node was reporting — it is gone, or about to say
    everything again. Caller holds _AGG_LOCK."""
    for info_hash in _HELD_BY.pop(nid, ()):
        ds = _DATASETS.get(info_hash)
        if ds is not None:
            _without(info_hash, ds, nid)


# --- following the nodes ------------------------------------------------------
# One refresh, and the two rules that keep it from growing with the swarm: ask
# every node what it is (cheap, always), and ask what it holds only when it says
# that changed (and then only for the change).

def follow(rec: dict, base: str) -> tuple:
    """What one node holds, as far as it will tell us: (cursor, rows, listed).

    Two attempts at most: with the cursor we hold, and — if the node says it
    cannot answer from that one — with none, taking the full list, which comes
    in pages. Nothing here parses the cursor. That is the node's business, which
    is exactly why it can tell us the cursor is stale instead of us having to
    work it out: a restarted node would otherwise answer "nothing has changed
    since 4417233" forever, and be believed.

    `listed` says which of the two it was, because the answers mean different
    things: a full listing is the whole truth for that node and replaces what we
    had of it, a delta only amends it.
    """
    for cursor in (rec["cursor"], None):
        listed, rows = cursor is None, []
        try:
            while True:
                page = node_client.fetch_holdings(base, since=cursor)
                rows += page.get("holdings") or []
                cursor = page["cursor"]
                if not page.get("more"):
                    break
        except node_client.Resync:
            continue
        return cursor, rows, listed
    return rec["cursor"], [], False


def refresh(st: dict, now: float) -> tuple:
    """Read one node: (key, what it holds or None, what it is moving).

    Called per node in parallel, so it touches only that node's own record and
    hands the rows back to be folded in one place rather than writing the
    aggregate from sixteen threads."""
    key, addr = st["node_key"], st.get("addr", st["node_key"])
    base = f"http://{addr}"
    rec = _NODES.setdefault(key, {"cursor": None, "transfers": []})
    rec.update({"base": base, "addr": addr, "name": st.get("name") or addr,
                "stats": st, "at": now})
    try:
        # The whole economy of this file: holdings are refetched only when the
        # node's cursor says something actually changed, and what is moving is
        # asked for only when /stats says something is. An empty list, not None
        # — None means the node did not answer and what we had of it stands,
        # while a node with nothing in flight has nothing in flight, and the
        # rows from the transfer that just finished are not to be kept.
        followed = follow(rec, base) if st.get("cursor") != rec["cursor"] else None
        moving = node_client.fetch_transfers(base) if st.get("moving") else []
        return key, followed, moving
    except Exception:
        return key, None, None


def poll(now: float = None) -> list:
    """Every live node's record, refreshed at most once per POLL_TTL.

    A node that doesn't answer drops out of this list but keeps its record and
    its cursor, so a node that blips comes back on the cursor rather than
    re-listing everything it holds. One that stays silent longer than GONE_AFTER
    is treated as gone: what it was holding stops counting."""
    now = now if now is not None else time.time()
    with _POLL_LOCK:
        if now - _POLL["at"] >= config.POLL_TTL:
            _POLL["at"] = now
            for base in ways_in():
                try:
                    stats, bases = node_client.fetch_swarm(base)
                except Exception:
                    continue
                _POLL["bases"] = bases
                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                    read = list(pool.map(lambda st: refresh(st, now), stats))
                answered = {st["node_key"] for st in stats}
                with _AGG_LOCK:
                    for key, followed, transfers in read:
                        rec = _NODES[key]
                        if transfers is not None:
                            rec["transfers"] = transfers
                        if followed:
                            cursor, rows, listed = followed
                            nid = node_id(key)
                            if listed:
                                drop_node(nid)   # the listing is the whole truth
                            apply_rows(nid, rows)
                            rec["cursor"] = cursor
                    for key, rec in _NODES.items():
                        rec["live"] = key in answered
                        if key in answered:
                            rec["seen"] = now
                        elif (now - rec.get("seen", now) > GONE_AFTER
                              and _NODE_ID.get(key) in _HELD_BY):
                            drop_node(_NODE_ID[key])
                            rec["cursor"] = None    # it must list in full again
                break
        return [rec for rec in _NODES.values() if rec.get("live")]


def name(key: str) -> str:
    """A node's display name, live or not."""
    return (_NODES.get(key) or {}).get("name") or key[:12]


# --- queries ------------------------------------------------------------------
# What readers ask. Each takes _AGG_LOCK itself and returns copies, so nothing
# outside this module holds the lock or sees a node id.

def _row(info_hash: str, ds: list) -> dict:
    return {"info_hash": info_hash, "name": ds[0], "total_size": ds[1],
            "piece_length": ds[2],
            "complete": [_NODE_KEY[i] for i in ds[3]],
            "partial": [_NODE_KEY[i] for i in ds[4]]}


def dataset(info_hash: str) -> dict:
    """One dataset and its holders, or None if no node reports it."""
    with _AGG_LOCK:
        ds = _DATASETS.get(info_hash)
        return _row(info_hash, ds) if ds else None


def _matches(copies: int, ds: list, query: str, status: str) -> bool:
    if query and query not in (ds[0] or "").lower():
        return False
    if status == "incomplete":
        return copies < 1
    if status == "replicating":
        return bool(ds[4])
    return True


def rarest(limit: int, query: str = "", status: str = "all") -> tuple:
    """(datasets rarest first, how many matched).

    Unfiltered this touches only the rarest classes — the first page of a
    healthy swarm is a handful of sets, however many datasets there are. With a
    filter it is a scan, which is what a search costs while names are unindexed,
    and it is exact: the count returned is every match, not a guess."""
    with _AGG_LOCK:
        picked, plain = [], not query and status == "all"
        for copies in sorted(k for k in _BY_COPIES if _BY_COPIES[k]):
            members = _BY_COPIES[copies]
            if plain:
                picked += [(copies, h) for h in heapq.nsmallest(limit - len(picked), members)]
                if len(picked) >= limit:
                    break
            else:
                picked += [(copies, h) for h in members
                           if _matches(copies, _DATASETS[h], query, status)]
        matched = len(_DATASETS) if plain else len(picked)
        if not plain:
            picked.sort()
        return [_row(h, _DATASETS[h]) for _, h in picked[:limit]], matched


def held_by(key: str, limit: int, query: str = "") -> tuple:
    """(a page of one node's datasets in info-hash order, how many matched)."""
    with _AGG_LOCK:
        members = _HELD_BY.get(_NODE_ID.get(key), set())
        if query:
            members = {h for h in members if h in _DATASETS
                       and query in (_DATASETS[h][0] or "").lower()}
        rows = [_row(h, _DATASETS[h]) for h in heapq.nsmallest(limit, members)
                if h in _DATASETS]
        return rows, len(members)


def totals() -> dict:
    """Swarm-wide numbers, none of which walk the aggregate."""
    with _AGG_LOCK:
        return {"total": len(_DATASETS),
                "at_risk": len(_BY_COPIES.get(0, ())) + len(_BY_COPIES.get(1, ())),
                "rarest": min((k for k, v in _BY_COPIES.items() if v), default=0),
                "stored": _STORED}


def rescue(key: str, limit: int) -> dict:
    """What a node with spare space could take, and what it could let go.

    `candidates`: a random sample of the rarest datasets the node doesn't hold,
    with at least one complete copy to fetch from. Random, so that nodes asking
    at the same time mostly get different ones. `evictable`: the node's own
    holdings with the most copies. Both count downloading copies too, so a
    rescue under way elsewhere shows."""
    with _AGG_LOCK:
        held = _HELD_BY.get(_NODE_ID.get(key), set())
        picked = []
        for copies in sorted(k for k in _BY_COPIES if k > 0 and _BY_COPIES[k]):
            members = _BY_COPIES[copies]
            # Enough that the ones held here can't crowd out the rest.
            draw = random.sample(tuple(members), min(len(members), limit + len(held)))
            picked += [h for h in draw if h not in held][:limit - len(picked)]
            if len(picked) >= limit:
                break

        def holders(h):
            return len(_DATASETS[h][3]) + len(_DATASETS[h][4])

        picked.sort(key=holders)
        evictable = heapq.nlargest(limit, (h for h in held if h in _DATASETS),
                                   key=holders)
        return {"candidates": [_row(h, _DATASETS[h]) for h in picked],
                "evictable": [_row(h, _DATASETS[h]) for h in evictable]}
