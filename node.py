"""A collab-cluster node: the whole system, in one program.

Run one per host. Nodes find each other with a UDP multicast beacon and move the
bytes with BitTorrent v2. There is no tracker, no central catalog and no
coordinator.

  GET  /stats                    what this node is, as a whole: disk, counts,
                                 throughput, and the cursor below. Constant
                                 size — it says nothing per dataset.
  GET  /holdings[?since=<cursor>]     which datasets this node holds and whether
                                 each is complete, with each one's name, size and
                                 piece length. With a cursor, only what has
                                 changed since; the response carries the next
                                 one. This is what copy counting reads, and,
                                 unioned across nodes, it is the catalog.
  GET  /holdings/<info_hash>     one dataset here, with its piece bitfield — the
                                 drill-down, one dataset at a time.
  GET  /transfers                what is moving right now: progress and rates,
                                 for in-flight transfers only.
  GET  /catalog/<info_hash>      one dataset's file -> piece-range map, for the
                                 per-file views. Held datasets only.
  GET  /catalog/<info_hash>.torrent   the raw .torrent. Held datasets only —
                                 whoever holds the data has the torrent.
  GET  /peers                    {"self": {...}, "peers": [...]} — this node's
                                 view of the swarm.
  POST /publish  {"path": ...}   hash a local file/dir into a dataset and seed it
                                 in place. This is the only way data enters the
                                 swarm.
  POST /add      {"info_hash": ...}            take a dataset from whoever has it
  POST /remove   {"info_hash"|"name": ...}     drop one

Two background threads — the libtorrent session loop (tracks what is moving)
and the sync loop (the engine, below) — while the main thread serves the HTTP
API above.

The split across those endpoints is deliberate and is the one thing here that
decides whether a swarm-wide view stays affordable. A node holds far more
datasets than it moves, and holdings change only when one is taken, finishes or
is dropped — so they are read as a stream of transitions (a cursor), while the
per-second numbers are confined to the transfers in flight and the piece
bitfields to a single dataset at a time. Nothing a reader polls scales with how
much this node holds.

There is no catalog here, and that is the second half of the same idea.
Publishing seeds the data in place, so a dataset has a holder from the instant it
exists and the union of every node's holdings is exactly the set of datasets in
the swarm. A node knows only what it holds — it keeps a .torrent for that and
nothing about any other dataset — so nothing it stores scales with the catalog
either. Two things follow: "which datasets exist" is a question for whoever is
watching the whole swarm and not for any one node, and a dataset lives exactly as
long as someone holds it.

What a node holds is only ever what it was given: /publish puts local data in,
/add takes a copy of somebody else's. Nothing arrives unasked.
"""
import argparse
import glob
import json
import os
import re
import shutil
import signal
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import libtorrent as lt

import beacon
import catalog
import config
import make_torrent

# Persist torrents natively via libtorrent fast-resume. save_info_dict embeds the
# torrent's metadata in the resume file, so a restarted node can re-add a torrent
# from the .resume file alone; flush_disk_cache makes the on-disk data match what
# we record. We rewrite a torrent's .resume whenever its state changes, and
# delete it on /remove, so restarting a node restores exactly the torrents it was
# holding (with download progress intact).
SAVE_FLAGS = lt.torrent_handle.save_info_dict | lt.torrent_handle.flush_disk_cache
# How often (session loops) to checkpoint resume data for torrents that changed.
RESUME_EVERY = 5

# How many holding transitions to keep so a reader can be told what changed
# rather than re-sent everything. A reader whose cursor is older than the oldest
# retained transition is told to re-list instead; the bound is what stops the log
# growing without limit on a long-lived node.
CHANGE_LOG_LIMIT = 10000


class NodeState:
    def __init__(self, node_id: int, node_key: str, ses: "lt.session"):
        self.node_id = node_id
        # Stable swarm-wide identity (a persisted UUID). The integer node_id is a
        # local convenience (ports, data dirs); node_key is how peers tell each
        # other apart, and what the collector keys every metric by, so a restart
        # keeps the same series.
        self.node_key = node_key
        self.ses = ses
        self.lock = threading.Lock()
        # info_hash(v2 str) -> {name, save_path, ti, files, handle, state,
        # total_size, piece_length} — the data we hold, and the whole of what
        # this node knows about any dataset. There is no second dict of datasets
        # it merely knows of: a node holds what it was given or told to take and
        # knows nothing at all about the rest, so "which datasets exist" is a
        # question for whoever can see every node. `state` is "downloading" or
        # "complete", set where the transition happens (add, finish, remove)
        # rather than rediscovered by polling every torrent, and read by mesh(),
        # which only offers peers to torrents still missing data.
        self.torrents: dict = {}
        # node_key -> {ip, bt, http, last_seen} — other nodes we can see.
        # Beacons are the only way in: a node we cannot hear, we do not know.
        # Addresses are all we want from them: a peer is somewhere to fetch a
        # .torrent from and somewhere to point a torrent that still needs bytes.
        self.peers: dict = {}
        # --- how a reader follows what we hold -------------------------------
        # `torrents` changes only on a transition, so it is published as a stream
        # of them: a reader keeps a cursor and asks what changed since, instead
        # of refetching a list that is mostly the same every time.
        #
        # `epoch` is minted fresh every process start and `seq` counts
        # transitions within it. Together they are the cursor — see cursor_of()
        # for why the reader is not allowed to take them apart.
        self.epoch = uuid.uuid4().hex[:8]
        self.seq = 0
        self.changes: list = []      # (seq, info_hash, state), oldest first
        self.trimmed_before = 0      # cursors at or below this can't be answered
        # Both maintained as transitions happen rather than counted on demand,
        # so /stats answers without walking everything this node holds.
        self.n_complete = 0
        self.stored_complete = 0
        # The transfers still moving, with their live progress and rates. The
        # only place per-dataset per-second numbers exist, and bounded by what is
        # in flight rather than by how much this node holds.
        self.transfers: list = []
        # Session-wide throughput, from libtorrent's own counters. Summing the
        # torrents would mean asking every one of them every second, which is
        # precisely what must not scale with the number held.
        self.rates = {"download_rate": 0, "upload_rate": 0, "num_peers": 0}
        self.stop = threading.Event()


# --- on-disk layout ----------------------------------------------------------
# Everything a node owns lives under nodes/<id>/. Datasets are identified by
# their v2 info-hash, but nothing on disk is a bare hash: each gets a readable
# slug, "<name>_<hash8>", so two datasets that happen to share a name can coexist
# without either shadowing the other.

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def slug(name: str, info_hash: str) -> str:
    """A readable, collision-free on-disk label, e.g. "media_3f9ac1d2"."""
    safe = _UNSAFE.sub("_", name)[:40].strip("._-") or "dataset"
    return f"{safe}_{info_hash[:8]}"


def node_dir(node_id: int) -> str:
    return os.path.join(config.NODES_DIR, str(node_id))


def catalog_dir(node_id: int) -> str:
    """This node's .torrent files — its answer to "what datasets exist"."""
    return os.path.join(node_dir(node_id), "catalog")


def data_dir(node_id: int) -> str:
    """Where downloaded datasets land (published ones stay where they are)."""
    return os.path.join(node_dir(node_id), "data")


def resume_dir(node_id: int) -> str:
    return os.path.join(node_dir(node_id), ".resume")


def node_key_path(node_id: int) -> str:
    return os.path.join(node_dir(node_id), "node_key")


def load_or_create_node_key(node_id: int) -> str:
    """The node's stable UUID identity, generated once and persisted (like
    fast-resume). Integer ids collide across hosts; this doesn't."""
    path = node_key_path(node_id)
    if os.path.exists(path):
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    key = uuid.uuid4().hex
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(key)
    return key


def node_disk(node_id: int) -> dict:
    """Free/total bytes of the filesystem holding this node's data directory —
    the disk that fills up as the node stores more datasets."""
    try:
        usage = shutil.disk_usage(node_dir(node_id))
        return {"free": usage.free, "total": usage.total}
    except OSError:
        return {"free": 0, "total": 0}


# --- the torrent files this node has -----------------------------------------
# One .torrent per dataset held, and no others. A node used to keep a copy of
# every torrent in the swarm so that "the catalog" was a thing it could serve;
# it isn't any more (see the holdings section below), so this directory now
# tracks ns.torrents exactly: written when a dataset is taken, deleted when it
# is dropped.

def store_torrent(ns: NodeState, name: str, info_hash: str, blob: bytes) -> str:
    """Write a dataset's .torrent alongside the data this node holds.

    Kept because a v2 torrent cannot be regenerated from libtorrent's resume data
    — the piece layers live outside the info dict — and because a holder is who
    peers ask for it. 14 KB against ~100 MiB of data, so it costs nothing next to
    what holding the dataset already costs.

    Written via a temp file + rename so a reader never sees a partial torrent."""
    directory = catalog_dir(ns.node_id)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{slug(name, info_hash)}.torrent")
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    return path


def drop_torrent(ns: NodeState, name: str, info_hash: str) -> None:
    """Forget a dataset's .torrent when the data goes. The directory holds what
    this node holds, so a dropped dataset leaves nothing behind to serve."""
    try:
        os.remove(os.path.join(catalog_dir(ns.node_id),
                               f"{slug(name, info_hash)}.torrent"))
    except FileNotFoundError:
        pass


def _ambiguous(ref: str, kind: str, matches: list) -> ValueError:
    """Refuse to guess, and hand back something that can be pasted straight back
    in — the shortened hashes below are valid references in their own right."""
    return ValueError(f"{ref!r} is an ambiguous {kind} - {len(matches)} datasets "
                      f"match; use one of these info-hashes: "
                      f"{', '.join(h[:16] for h in sorted(matches))}")


def resolve(ns: NodeState, info_hash: str = None, name: str = None) -> str:
    """Find one dataset *this node holds*, by info-hash or by name.

    Scoped to what it holds because that is all it knows: there is no local
    catalog of the swarm any more. Resolving a name across the swarm is the
    caller's job (control.py fans out over the holdings streams), and /add
    therefore takes an info-hash; this is what /remove uses, which can only ever
    act on something already held.

    An info-hash may be given in full or shortened to any unique leading portion.
    Names are labels, not identifiers — two nodes can publish different content
    under the same name — so a name matching several datasets is an error.
    """
    with ns.lock:
        held = {ih: e["name"] for ih, e in ns.torrents.items()}
    if info_hash:
        ref = info_hash.strip().lower()
        matches = [ih for ih in held if ih.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _ambiguous(ref, "info-hash", matches)
        # Nothing matched. It may have been a *name* that merely looks like a
        # hash (a directory called "deadbeef00"), so fall through rather than
        # failing on a technicality.
        name = name or info_hash
    matches = [ih for ih, held_name in held.items() if held_name == name]
    if not matches:
        raise FileNotFoundError(name)
    if len(matches) > 1:
        raise _ambiguous(name, "name", matches)
    return matches[0]


# --- holdings ----------------------------------------------------------------
# Which datasets this node holds, published as something a reader can follow
# incrementally rather than refetch.
#
# This is also the catalog. There is no separate list of "datasets that exist":
# publishing seeds the data in place, so every dataset has a holder from the
# moment it exists, and the union of every node's holdings is therefore exactly
# the set of datasets in the swarm. A node learns what exists by following its
# peers' streams and forgetting the rows it doesn't act on — it keeps no copy of
# the whole. The corollary is that a dataset lives as long as someone holds it:
# when the last holder drops it, it leaves the swarm's view.
#
# The distinction that matters: holding a dataset is durable state that changes
# only when one is taken, finishes, or is dropped, while progress and rates
# change every second. Keeping them in one payload — as a single /stats snapshot
# would — means a reader either refetches everything it already knows every
# second, or misses transitions. So transitions go in a log with a cursor, and
# the per-second numbers live on /transfers, whose size is bounded by what is
# moving rather than by what is stored.
#
# A row carries the dataset's immutable identity as well as the state: name,
# size and piece length. Not a contradiction of the rule above — that rule is
# about things which *churn*, and these never change — but what makes the union
# of these streams usable as a catalog without a per-dataset lookup for every
# dataset in it. Everything else about a dataset (the file -> piece map, which is
# the large part) stays behind /catalog/<info_hash>, for the views that need it.


class StaleCursor(Exception):
    """A cursor this node can no longer answer from — minted in an earlier epoch,
    or older than the retained part of the change log."""


def holding_row(info_hash: str, entry: dict, state: str = None) -> dict:
    """One row of the holdings stream: what this node has of a dataset, and the
    little about the dataset itself that every list view needs."""
    return {"info_hash": info_hash, "state": state or entry["state"],
            "name": entry["name"], "total_size": entry["total_size"],
            "piece_length": entry["piece_length"]}


def note_holding(ns: NodeState, row: dict, previous: str = None) -> None:
    """Record a holding transition. The caller holds ns.lock.

    Every change to `torrents` goes through here, which is what lets a reader ask
    "what changed since" and be answered without a scan. The running totals are
    kept here for the same reason: the row and `previous` are enough to move
    them, so /stats never has to walk everything held to report them.

    A tombstone ("gone") carries the same fields as any other row, so a reader
    that never saw the dataset arrive still knows what left.
    """
    ns.seq += 1
    ns.changes.append((ns.seq, row))
    became = (row["state"] == "complete") - (previous == "complete")
    ns.n_complete += became
    ns.stored_complete += became * row["total_size"]
    excess = len(ns.changes) - CHANGE_LOG_LIMIT
    if excess > 0:
        # What we drop we can no longer answer for: a cursor from before this
        # point gets told to start over rather than handed an incomplete delta.
        ns.trimmed_before = ns.changes[excess - 1][0]
        del ns.changes[:excess]


def cursor_of(ns: NodeState) -> str:
    """This node's current position in its own change log. Caller holds ns.lock.

    Deliberately one opaque string. It is "<epoch>:<seq>" today, and readable on
    purpose, but nothing outside this file may take it apart: a reader stores it
    and hands it back untouched. That is what lets *this node* decide whether a
    cursor is still good. Split across two fields the node cannot tell — it would
    see only a number, with no idea which epoch minted it — and a reader that
    forgets to compare epochs is handed an empty delta after every restart and
    believes, silently and permanently, that nothing has changed.
    """
    return f"{ns.epoch}:{ns.seq}"


def cursor_since(ns: NodeState, token: str) -> int:
    """The seq a cursor refers to, or StaleCursor. Caller holds ns.lock."""
    epoch, _, seq = token.partition(":")
    if epoch != ns.epoch or not seq.isdigit():
        raise StaleCursor(token)
    since = int(seq)
    if since < ns.trimmed_before or since > ns.seq:
        raise StaleCursor(token)
    return since


def holdings(ns: NodeState, since: str = None) -> dict:
    """What this node holds: everything, or only what changed since a cursor.

    A row is the dataset's identity (info_hash, name, total_size, piece_length)
    and what this node has of it (state). Progress and rates are left out on
    purpose — they move every second, so including them would put every in-flight
    transfer into the stream continuously and defeat the cursor entirely. They
    are on /transfers instead. The identity fields are exempt from that reasoning
    because they never change, and they are what lets a reader union these
    streams into a catalog without a lookup per dataset.

    `state` is "downloading" or "complete", and in a delta also "gone": the
    tombstone that tells a reader a dataset was dropped rather than merely
    absent from this response. A dataset whose last holder reports "gone" has
    left the swarm — this stream is the catalog, so there is nothing else for it
    to still be in.

    `more` is always false for now — a full listing is not paged yet — but it is
    in the response so a reader's loop is already written to follow one, and
    paging can be added behind the same cursor without the reader changing.
    """
    with ns.lock:
        if since is None:
            rows = [holding_row(ih, e) for ih, e in ns.torrents.items()]
        else:
            after = cursor_since(ns, since)
            rows = [row for seq, row in ns.changes if seq > after]
        # Read under the same lock as the rows, so the cursor we hand back can
        # never claim to cover a transition the reader was not given.
        return {"cursor": cursor_of(ns), "more": False, "holdings": rows}


def node_stats(ns: NodeState) -> dict:
    """What this node is, as a whole. Constant size: nothing here is per dataset.

    Cheap enough to poll every second forever, which is the point — a reader
    watches this, and only fetches holdings when the cursor has moved.
    """
    with ns.lock:
        held, complete = len(ns.torrents), ns.n_complete
        cursor, rates = cursor_of(ns), dict(ns.rates)
        moving = list(ns.transfers)
    return {"node_key": ns.node_key, "ts": time.time(),
            "bt_port": config.bt_port(ns.node_id),
            "http_port": config.stats_port(ns.node_id),
            "disk": node_disk(ns.node_id),
            "held": held, "complete": complete,
            "downloading": held - complete, "moving": len(moving),
            # Bytes on disk: the completed datasets, plus how far the in-flight
            # ones have got. Both sides are already to hand, so this stays
            # O(what is moving) rather than O(what is held).
            "stored": ns.stored_complete + sum(t["bytes_done"] for t in moving),
            "cursor": cursor, **rates}


def _is_pad(fs, i: int) -> bool:
    if hasattr(fs, "pad_file_at"):
        try:
            return fs.pad_file_at(i)
        except Exception:
            pass
    return "/.pad/" in fs.file_path(i).replace(os.sep, "/")


def file_list(ti) -> list:
    """Static file -> piece-range mapping (real files only; pad files skipped).

    v2 aligns each file to a piece boundary, so a real file owns the contiguous
    range [first_piece, last_piece] exclusively. Consumers use this with a node's
    piece bitfield to tell which files (and how many copies) a node holds.
    """
    fs = ti.files()
    out = []
    for i in range(fs.num_files()):
        if _is_pad(fs, i):
            continue
        size = fs.file_size(i)
        if size > 0:
            first = ti.map_file(i, 0, 1).piece
            last = ti.map_file(i, size - 1, 1).piece
        else:
            first, last = ti.map_file(i, 0, 0).piece, -1  # empty file: no pieces
        out.append({"path": fs.file_path(i).replace(os.sep, "/"), "size": size,
                    "first_piece": first, "last_piece": last})
    return out


def dataset_meta(info_hash: str, entry: dict) -> dict:
    """A dataset's static shape: name, size, piece layout, file -> piece ranges.

    Identical on every node and fixed for the dataset's lifetime — it is what the
    info-hash hashes — so a reader fetches it once and keeps it, rather than
    receiving one copy per node per poll as it used to.

    The file -> piece map is the large part and only the per-file views need it,
    which is why the holdings row carries just name/size/piece_length and this is
    a separate lookup.
    """
    ti = entry["ti"]
    return {"info_hash": info_hash, "name": ti.name(),
            "total_size": ti.total_size(), "piece_length": ti.piece_length(),
            "num_pieces": ti.num_pieces(), "files": entry["files"]}


def transfer_row(info_hash: str, entry: dict, st) -> dict:
    """One in-flight transfer, as /transfers reports it.

    The only place per-dataset live numbers appear. Everything here changes every
    second, which is why it is kept out of the holdings stream — and why this
    list is bounded by what is moving, not by what is held.
    """
    return {"info_hash": info_hash, "name": entry["name"],
            "total_size": entry["ti"].total_size(),
            "progress": st.progress, "bytes_done": st.total_done,
            "download_rate": st.download_rate, "upload_rate": st.upload_rate,
            "num_peers": st.num_peers}


def holding_detail(ns: NodeState, info_hash: str) -> dict:
    """One dataset on this node, with its piece bitfield.

    The bitfield is the one genuinely large per-dataset thing a node knows, so it
    is served one dataset at a time and never as part of a listing. That split is
    what keeps the swarm-wide piece map affordable: it costs one request per node
    for the dataset being looked at, whatever the catalog holds.

    Left as a plain JSON bool array rather than packed. The endpoint is already
    bounded by a single dataset, so packing would trade away legibility — the
    thing every other wire format here keeps — for nothing that matters.
    """
    with ns.lock:
        entry = ns.torrents.get(info_hash)
        state = entry["state"] if entry else None
    if not entry:
        raise FileNotFoundError(info_hash)
    st = entry["handle"].status()
    return {"info_hash": info_hash, "name": entry["name"], "state": state,
            "pieces": [bool(b) for b in st.pieces],
            "progress": st.progress, "bytes_done": st.total_done,
            "num_peers": st.num_peers}


# --- libtorrent session ------------------------------------------------------

def make_session(node_id: int) -> "lt.session":
    settings = {
        # Bind all interfaces so peers on other hosts can reach us (not just
        # loopback).
        "listen_interfaces": f"{config.BIND_HOST}:{config.bt_port(node_id)}",
        # Every discovery mechanism is off. Peers come from exactly one place:
        # the sync loop injecting beacon-discovered nodes with connect_peer().
        # The torrents are private too, so libtorrent wouldn't use these anyway;
        # keeping them off explicitly means nothing can pull in outside peers.
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        # Distinct hosts have distinct IPs, but many nodes on one dev host share
        # an IP; without this libtorrent allows only ONE peer connection per IP
        # per torrent, so a single-host swarm couldn't mesh.
        "allow_multiple_connections_per_ip": True,
        # Only the two categories the session loop actually reads: fast-resume
        # checkpoints, and the session counters it turns into this node's
        # throughput. all_categories would additionally switch on the per-peer,
        # per-piece and per-block log streams, which libtorrent generates at high
        # volume all through a transfer and which we pop only to discard.
        "alert_mask": (lt.alert.category_t.storage_notification
                       | lt.alert.category_t.stats_notification),
        # libtorrent's queue is written for a client seeding a few torrents it
        # chose: it keeps `active_seeds` unpaused and rotates the rest out, and
        # a paused torrent refuses peers. A node has to answer for everything it
        # holds, so seeding is never queued (-1 is "no limit"). At the default a
        # node serves only the handful it has not paused.
        "active_seeds": -1,
        "active_limit": -1,
        # Downloading *is* queued, for the same reason copies are complete
        # holders: half a dataset is worth nothing. Run at once they advance in
        # lockstep and finish together, so a large batch moves bytes for a long
        # stretch without producing one copy. A few dozen at a time is no slower
        # overall.
        "active_downloads": 32,
        # How long a queued torrent waits paused to be looked at again. At the
        # default (30s), a node told to take one dataset can sit still for most
        # of that.
        "auto_manage_interval": 2,
        # Publishing seeds in place, so a dataset is complete once libtorrent
        # has checked the files already sitting there. Serialised, a batch of
        # publishes is confirmed one at a time, and until then each one counts
        # as a dataset with no copies.
        "active_checking": 64,
        # Choking rations upload slots against strangers who give nothing back.
        # There are none here. Rationed, a seed rotates who it answers and a
        # choked leecher sits on its requests until request_timeout drops the
        # connection a minute later; a few such stalls add that minute to a
        # whole batch.
        "unchoke_slots_limit": -1,
        # Pace the transfer so progress is observable as it happens (see config).
        # By default libtorrent exempts loopback/LAN peers from rate limits, so
        # we must turn that off for the cap to apply within a single-host swarm.
        "upload_rate_limit": config.UPLOAD_RATE_LIMIT,
        "ignore_limits_on_local_network": False,
    }
    return lt.session(settings)


def add_torrent(ns: NodeState, blob: bytes, serve_path: str = None) -> dict:
    """Start holding a dataset, given its .torrent bytes.

    Takes the bytes rather than an info-hash because a node no longer keeps
    torrents for datasets it doesn't hold: there is nowhere local to look one up.
    They come from make_torrent.build (publish) or from a peer that holds the
    dataset (take, below), and either way the identity is read out of the bytes
    themselves rather than believed from whoever supplied them.

    `serve_path` says where the data comes from: given, it is a copy already on
    this host (what /publish was handed) and we seed it in place; omitted, we
    download a fresh copy into nodes/<id>/data/<slug>/. Returns a status dict.
    """
    ti = lt.torrent_info(lt.bdecode(blob))
    info_hash = str(ti.info_hashes().v2)
    tname = ti.name()
    with ns.lock:
        held = ns.torrents.get(info_hash)
    if held:
        return {"info_hash": info_hash, "name": held["name"],
                "added": False, "note": "already present"}

    if serve_path:
        content = os.path.abspath(serve_path)
        if not os.path.exists(content):
            raise ValueError(f"serve path does not exist: {content}")
        # Derived from the torrent, not assumed — see make_torrent.serve_save_path.
        save_path = make_torrent.serve_save_path(ti, content)
    else:
        # Each dataset gets its own directory, keyed by slug rather than name, so
        # two datasets sharing a name don't download over each other.
        save_path = os.path.join(data_dir(ns.node_id), slug(tname, info_hash))
        os.makedirs(save_path, exist_ok=True)

    # On disk before it is in the stream: a peer that reacts to our transition
    # asks us for this file, so it must already be there to serve.
    store_torrent(ns, tname, info_hash, blob)

    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = save_path
    handle = ns.ses.add_torrent(atp)

    entry = {"name": tname, "save_path": save_path, "ti": ti,
             "files": file_list(ti), "handle": handle, "state": "downloading",
             "total_size": ti.total_size(), "piece_length": ti.piece_length()}
    with ns.lock:
        ns.torrents[info_hash] = entry
        # Even a dataset we already have every byte of starts here: the session
        # loop promotes it once libtorrent has checked the files. One path in,
        # so nothing can be held without a transition being published.
        note_holding(ns, holding_row(info_hash, entry))
    # Persist immediately so a restart before any download still restores it
    # (the session loop writes the actual .resume file from the alert).
    handle.save_resume_data(SAVE_FLAGS)
    print(f"node {ns.node_id}: +'{tname}' [{info_hash[:8]}] "
          f"({len(entry['files'])} files) -> {save_path}", flush=True)
    return {"info_hash": info_hash, "name": tname, "added": True}


def fetch_torrent(ns: NodeState, info_hash: str) -> bytes:
    """A dataset's .torrent, from a peer that holds it.

    This is what replaces having a copy of every torrent in the swarm. Whoever
    holds the data necessarily has the torrent, so asking peers in turn finds it
    — and only the node that has decided to take a dataset pays for it, once,
    instead of every node paying for every dataset.

    The bytes are checked against the info-hash asked for, so a peer cannot
    answer with something else.
    """
    with ns.lock:
        bases = [f"http://{p['ip']}:{p['http']}" for p in ns.peers.values()
                 if p.get("ip") and p.get("http")]
    for base in bases:
        try:
            blob = catalog.fetch_torrent_bytes(base, info_hash)
        except Exception:
            continue
        got = str(lt.torrent_info(lt.bdecode(blob)).info_hashes().v2)
        if got == info_hash:
            return blob
        print(f"node {ns.node_id}: {base} offered {info_hash[:8]} but sent "
              f"{got[:8]}; ignoring", flush=True)
    raise FileNotFoundError(info_hash)


def take(ns: NodeState, info_hash: str) -> dict:
    """Take a dataset: find its .torrent among the peers, then hold it."""
    return add_torrent(ns, fetch_torrent(ns, info_hash))


def remove_torrent(ns: NodeState, info_hash: str) -> dict:
    """Stop holding a dataset, and stop being one of the places it exists.

    There is no catalog to stay in: the holdings streams *are* the catalog, so
    dropping a dataset removes this node from the set of places it exists, and
    dropping the last copy retracts it from the swarm. The downloaded files stay
    on disk — re-publishing that path reproduces the same dataset, since the
    identity is the content."""
    with ns.lock:
        entry = ns.torrents.pop(info_hash, None)
        if entry:
            # The tombstone: without it a reader following the stream cannot tell
            # a dropped dataset from one that simply wasn't mentioned.
            note_holding(ns, holding_row(info_hash, entry, "gone"), entry["state"])
    if not entry:
        return {"removed": False, "note": "not held"}
    ns.ses.remove_torrent(entry["handle"])
    # Drop its resume file so a restart doesn't bring the torrent back, and its
    # .torrent so this node stops answering for a dataset it no longer has.
    try:
        os.remove(os.path.join(resume_dir(ns.node_id),
                               f"{slug(entry['name'], info_hash)}.resume"))
    except FileNotFoundError:
        pass
    drop_torrent(ns, entry["name"], info_hash)
    print(f"node {ns.node_id}: -{entry['name']} [{info_hash[:8]}]", flush=True)
    return {"removed": True, "info_hash": info_hash, "name": entry["name"]}


def publish(ns: NodeState, path: str) -> dict:
    """Put local data into the swarm. The only way in.

    Hash the path into a v2 torrent and seed it in place — nothing is copied.
    Publishing *is* starting to hold it, which is what makes the holdings streams
    the catalog: a dataset exists from the moment someone has it, and there is no
    separate list for it to be added to. The transition goes into this node's
    stream, where anyone watching the swarm sees it; putting a copy on another
    node is a separate instruction to that node.
    """
    name, info_hash, blob = make_torrent.build(path)   # raises ValueError
    with ns.lock:
        known = info_hash in ns.torrents
    res = add_torrent(ns, blob, serve_path=path)
    return {"name": name, "info_hash": info_hash, "published": not known,
            "serving": res.get("added", False), "note": res.get("note")}


def _write_resume(ns: NodeState, alert) -> None:
    """Checkpoint one torrent's fast-resume data.

    The name comes from the alert's own params, not from its handle: a handle
    whose torrent has already been removed reports an all-zero info-hash, so the
    checkpoint would be filed under a name that does not match its contents,
    that /remove never cleans up, and that silently brings the dataset back on
    the next restart. For the same reason a checkpoint still in flight when the
    torrent is dropped is discarded rather than written."""
    info_hash = str(alert.params.info_hashes.v2)
    with ns.lock:
        held = info_hash in ns.torrents
    if not held:
        return
    os.makedirs(resume_dir(ns.node_id), exist_ok=True)
    path = os.path.join(resume_dir(ns.node_id),
                        f"{slug(alert.torrent_name, info_hash)}.resume")
    with open(path, "wb") as f:
        f.write(lt.write_resume_data_buf(alert.params))


def load_resumes(ns: NodeState) -> int:
    """Re-add every torrent saved as a .resume file (native fast-resume). The
    resume file is self-contained (save_info_dict), so no catalog lookup needed."""
    count = 0
    for path in sorted(glob.glob(os.path.join(resume_dir(ns.node_id), "*.resume"))):
        try:
            with open(path, "rb") as f:
                atp = lt.read_resume_data(f.read())
        except Exception as exc:
            print(f"node {ns.node_id}: skip {os.path.basename(path)}: {exc}", flush=True)
            continue
        ti = atp.ti
        if ti is None:
            print(f"node {ns.node_id}: skip {os.path.basename(path)}: no metadata",
                  flush=True)
            continue
        info_hash = str(ti.info_hashes().v2)
        handle = ns.ses.add_torrent(atp)
        entry = {"name": ti.name(), "save_path": atp.save_path, "ti": ti,
                 "files": file_list(ti), "handle": handle, "state": "downloading",
                 "total_size": ti.total_size(), "piece_length": ti.piece_length()}
        with ns.lock:
            ns.torrents[info_hash] = entry
            note_holding(ns, holding_row(info_hash, entry))
        count += 1
        print(f"node {ns.node_id}: resumed '{ti.name()}' [{info_hash[:8]}]", flush=True)
    return count


def flush_resume(ns: NodeState) -> None:
    """Synchronously checkpoint all torrents (used on shutdown)."""
    with ns.lock:
        entries = list(ns.torrents.values())
    pending = 0
    for e in entries:
        e["handle"].save_resume_data(SAVE_FLAGS)
        pending += 1
    deadline = time.time() + 5
    while pending > 0 and time.time() < deadline:
        for a in ns.ses.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                _write_resume(ns, a)
                pending -= 1
            elif isinstance(a, lt.save_resume_data_failed_alert):
                pending -= 1
        time.sleep(0.05)


def _note_rates(ns: NodeState, alert, prev, now: float):
    """Session-wide throughput, from libtorrent's cumulative byte counters.

    Per node rather than per torrent, for two reasons: summing the torrents would
    mean asking every one of them every second — the thing that must not scale
    with how much is held — and a node that is only seeding has no torrent it is
    polling at all, so it would report nothing.

    `now` is the loop's clock, not this function's: the counters are cumulative,
    so a rate is only as good as the interval it is divided by. The floor under
    that interval is the same guard from the other side — a reply that arrives a
    loop late would otherwise turn a whole loop's bytes into an impossible spike.
    """
    recv = alert.values.get("net.recv_payload_bytes", 0)
    sent = alert.values.get("net.sent_payload_bytes", 0)
    peers = alert.values.get("peer.num_peers_connected", 0)
    if prev:
        dt = max(config.NODE_LOOP_INTERVAL / 2, now - prev[0])
        with ns.lock:
            ns.rates = {"download_rate": int(max(0, recv - prev[1]) / dt),
                        "upload_rate": int(max(0, sent - prev[2]) / dt),
                        "num_peers": peers}
    return (now, recv, sent)


def session_loop(ns: NodeState) -> None:
    """Live state: what is moving right now, and what this node is doing overall.

    Deliberately never touches the torrents the node merely *holds*. Whether a
    dataset is held and whether it is complete are recorded where they change
    (note_holding), so this loop only has to notice the one transition libtorrent
    doesn't announce to us by itself — a download finishing — and it can look for
    that in the in-flight set alone. Asking every torrent for its status once a
    second is what a node cannot afford when it holds far more than it moves, so
    it doesn't: status() is called here on the transfers in flight, and elsewhere
    only for the one dataset someone is looking at.
    """
    loops = 0
    prev = None                       # (ts, recv, sent), for the rate deltas
    while not ns.stop.is_set():
        ns.stop.wait(config.NODE_LOOP_INTERVAL)  # sleep, but wake promptly on stop
        loops += 1
        now = time.time()
        ns.ses.post_session_stats()
        counters = None
        for a in ns.ses.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                _write_resume(ns, a)
            elif isinstance(a, lt.session_stats_alert):
                # Only the newest: a batch can hold a loop's reply and this
                # one's, and reading both against the same clock would divide a
                # loop's worth of bytes by no time at all.
                counters = a
            # save_resume_data_failed_alert: nothing to persist yet; ignore.
        if counters is not None:
            prev = _note_rates(ns, counters, prev, now)

        with ns.lock:
            moving = [(ih, e) for ih, e in ns.torrents.items()
                      if e["state"] == "downloading"]

        transfers, finished = [], []
        for info_hash, entry in moving:
            st = entry["handle"].status()
            if st.is_seeding or st.progress >= 1.0:
                finished.append((info_hash, entry))
            else:
                transfers.append(transfer_row(info_hash, entry, st))

        with ns.lock:
            for info_hash, entry in finished:
                # Guard against a /remove that landed between the two locks:
                # publishing a transition for a dataset we no longer hold would
                # resurrect it in every reader.
                if ns.torrents.get(info_hash) is entry:
                    entry["state"] = "complete"
                    note_holding(ns, holding_row(info_hash, entry), "downloading")
            ns.transfers = transfers

        # A complete torrent never changes again, so this is the last checkpoint
        # it needs — which is also why the periodic one below can ignore them.
        for _, entry in finished:
            entry["handle"].save_resume_data(SAVE_FLAGS)
        if loops % RESUME_EVERY == 0:
            for _, entry in moving:
                if entry["handle"].need_save_resume_data():
                    entry["handle"].save_resume_data(SAVE_FLAGS)

    flush_resume(ns)


# --- the engine --------------------------------------------------------------
# One tick, every BEACON_INTERVAL:
#   1. beacon   say who we are
#   2. peers    drain everyone else's beacons
#   3. mesh     hand every known peer to every torrent still missing data
# That's the whole distributed system. Everything below is those three steps.
#
# Nothing here decides what to store. A node holds what it published and what it
# was told to take, and nothing arrives on it unasked — storing is the one thing
# a node cannot undo cheaply, so it is always an instruction (/add) rather than
# something a node talks itself into.


def self_beacon(ns: NodeState) -> dict:
    """Who we are, as the swarm sees us. Deliberately says nothing about our
    address: a receiver reads that off the datagram's source, so no node ever has
    to work out (or be told) its own routable IP.

    Identity and ports, and nothing else: what a peer does with us is to ask us
    for a .torrent or to send us bytes, and both need only an address."""
    return {"v": beacon.VERSION, "node": ns.node_key,
            "bt": config.bt_port(ns.node_id),
            "http": config.stats_port(ns.node_id)}


def note_peer(ns: NodeState, key: str, ip: str, bt: int, http: int,
              last_seen: float) -> None:
    """Record (or refresh) a peer. Never records ourselves."""
    if not key or key == ns.node_key:
        return
    with ns.lock:
        peer = ns.peers.setdefault(key, {})
        # Never let an older sighting overwrite a fresher one.
        if last_seen >= peer.get("last_seen", 0):
            peer.update({"ip": ip, "bt": bt, "http": http,
                         "last_seen": last_seen})


def drain_beacons(ns: NodeState, sock) -> None:
    """Record everyone who announced themselves since the last tick."""
    now = time.time()
    for msg, ip in beacon.drain(sock):
        note_peer(ns, msg.get("node"), ip, msg.get("bt"), msg.get("http"), now)


def expire_peers(ns: NodeState) -> None:
    cutoff = time.time() - config.PEER_STALE_AFTER
    with ns.lock:
        for key in [k for k, p in ns.peers.items()
                    if p.get("last_seen", 0) < cutoff]:
            del ns.peers[key]


def mesh(ns: NodeState) -> None:
    """Step 3: what the tracker used to do. Hand every known peer to every
    torrent that still needs data, and let libtorrent take it from there.

    A complete torrent is skipped because it needs nobody: in BitTorrent the
    side that wants the bytes opens the connection, and libtorrent closes a
    seed-to-seed connection as soon as the handshake shows neither end has
    anything to offer. So a settled swarm holds no peer connections at all and
    does nothing here — the tick costs what is moving, not what is stored. A
    node that restarts and still wants data re-offers on its own next tick,
    which is what heals the swarm; one that restarts complete has nothing to
    heal."""
    with ns.lock:
        handles = [e["handle"] for e in ns.torrents.values()
                   if e["state"] != "complete"]
        addrs = [(p["ip"], p["bt"]) for p in ns.peers.values()
                 if p.get("ip") and p.get("bt")]
    for handle in handles:
        for addr in addrs:
            try:
                handle.connect_peer(addr)
            except Exception:
                pass  # torrent not ready, or peer already known


def sync_loop(ns: NodeState, sock) -> None:
    """The engine. One tick of the three steps, forever."""
    while not ns.stop.is_set():
        try:
            beacon.send(sock, self_beacon(ns))       # 1. say who we are
            drain_beacons(ns, sock)                  # 2. hear who else is here
            expire_peers(ns)
            mesh(ns)                                 # 3. wire peers into torrents
        except Exception:
            traceback.print_exc()
        ns.stop.wait(config.BEACON_INTERVAL)


# --- HTTP --------------------------------------------------------------------

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def make_handler(ns: NodeState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle_one_request(self):
            # Peers and CLI clients come and go mid-request — a timed-out
            # /catalog fetch, an interrupted `control.py status`. socketserver
            # would print a traceback for each one, which in a service log looks
            # like the node is failing when it is only being read from.
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _send(self, body: bytes, content_type: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, code=200):
            self._send(json.dumps(obj).encode(), "application/json", code)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw or b"{}")

        def do_GET(self):
            split = urlsplit(self.path)
            path = split.path
            if path == "/stats":
                self._send_json(node_stats(ns))
            elif path == "/holdings":
                self._holdings(split.query)
            elif path.startswith("/holdings/"):
                self._holding(path[len("/holdings/"):])
            elif path == "/transfers":
                with ns.lock:
                    rows = list(ns.transfers)
                self._send_json({"ts": time.time(), "transfers": rows})
            elif path == "/peers":
                self._send_json(self._peers_view())
            elif path.startswith("/catalog/"):
                self._catalog_file(path[len("/catalog/"):])
            else:
                self._send_json({"error": "not found"}, 404)

        def _peers_view(self) -> dict:
            now = time.time()
            with ns.lock:
                peers = [{"node": key, "ip": p.get("ip"), "bt": p.get("bt"),
                          "http": p.get("http"),
                          "age": round(now - p.get("last_seen", now))}
                         for key, p in ns.peers.items()]
                held = len(ns.torrents)
            me = self_beacon(ns)
            me.update({"held": held})
            return {"self": me, "peers": sorted(peers, key=lambda p: p["node"])}

        def _holdings(self, query: str):
            since = parse_qs(query).get("since", [None])[0]
            try:
                self._send_json(holdings(ns, since))
            except StaleCursor:
                # Not a failure: the reader's cursor predates a restart, or the
                # transitions it asked about have been trimmed away. Saying so is
                # the whole reason the cursor is opaque — answering with a delta
                # that silently omits everything before it is the one thing that
                # must not happen.
                with ns.lock:
                    current = cursor_of(ns)
                self._send_json({"resync": True, "cursor": current}, 409)

        def _holding(self, name: str):
            info_hash = name.lower()
            if not _HEX64.match(info_hash):
                return self._send_json({"error": "not a v2 info-hash"}, 400)
            try:
                self._send_json(holding_detail(ns, info_hash))
            except FileNotFoundError:
                self._send_json({"error": "not held"}, 404)

        def _catalog_file(self, name: str):
            # Addressed by full v2 info-hash. The readable slug is only how the
            # file is *stored*; no protocol depends on it. With the .torrent
            # suffix this is the torrent itself; without it, the same dataset's
            # shape as JSON, for readers that have no libtorrent to parse it
            # with — which is every reader here except a node.
            raw = name.endswith(".torrent")
            info_hash = (name[:-len(".torrent")] if raw else name).lower()
            if not _HEX64.match(info_hash):
                return self._send_json({"error": "not a v2 info-hash"}, 400)
            with ns.lock:
                entry = ns.torrents.get(info_hash)
            # Answered only for what this node holds, which is also the only
            # thing it has the torrent for. A reader asks a holder; the holdings
            # streams say who that is.
            if not entry:
                return self._send_json({"error": "not held here"}, 404)
            if not raw:
                return self._send_json(dataset_meta(info_hash, entry))
            try:
                with open(os.path.join(catalog_dir(ns.node_id),
                                       f"{slug(entry['name'], info_hash)}.torrent"),
                          "rb") as f:
                    body = f.read()
            except OSError:
                return self._send_json({"error": "not readable"}, 404)
            self._send(body, "application/x-bittorrent")

        def do_POST(self):
            body = {}
            try:
                body = self._read_json()
                if self.path == "/publish":
                    self._send_json(publish(ns, body["path"]))
                elif self.path == "/add":
                    # By info-hash only: a node has no catalog to look a name up
                    # in. control.py resolves names across the swarm's holdings
                    # streams and sends the hash it found.
                    self._send_json(take(ns, body["info_hash"].lower()))
                elif self.path == "/remove":
                    info_hash = resolve(ns, body.get("info_hash"), body.get("name"))
                    self._send_json(remove_torrent(ns, info_hash))
                else:
                    self._send_json({"error": "not found"}, 404)
            except FileNotFoundError as exc:
                self._send_json({"error": f"unknown dataset: {exc} "
                                          f"(see: python control.py list)"}, 404)
            except KeyError as exc:
                self._send_json({"error": f"missing field: {exc}"}, 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)

    return Handler


def run(target, *args):
    try:
        target(*args)
    except Exception:
        traceback.print_exc()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="A collab-cluster node: peer discovery, storage and "
                    "transfer in one self-sufficient process.")
    # A node-local slot number: picks this node's data dir (nodes/<id>/) and, so
    # several nodes can share one host in a dev run, offsets its ports. It is not
    # how the node is identified in the swarm (that's the node_key UUID). One
    # node per host is the common case, so it defaults to 0.
    ap.add_argument("--id", type=int, default=0)
    args = ap.parse_args()

    # Treat SIGTERM like Ctrl-C (raise KeyboardInterrupt) so the node shuts down
    # gracefully — checkpointing fast-resume — when stopped by a process manager
    # or `kill`, not just by an interactive Ctrl-C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    node_key = load_or_create_node_key(args.id)
    ns = NodeState(args.id, node_key, make_session(args.id))
    os.makedirs(catalog_dir(args.id), exist_ok=True)
    resumed = load_resumes(ns)       # what it was holding before a restart

    threads = [threading.Thread(target=run, args=(session_loop, ns), daemon=True)]
    sock = beacon.make_socket()
    threads.append(threading.Thread(target=run, args=(sync_loop, ns, sock), daemon=True))
    for t in threads:
        t.start()

    # Bind the HTTP API on all interfaces (not loopback) so peers and control can
    # reach it at this node's routable address — e.g. from the host into a
    # container, or across separate servers.
    srv = ThreadingHTTPServer((config.BIND_HOST, config.stats_port(args.id)),
                              make_handler(ns))
    # Handler threads must be daemonic: with HTTP/1.1 keep-alive they otherwise sit
    # blocked reading the next request on a persistent connection, and as non-daemon
    # threads they'd keep the process alive after Ctrl-C, hanging shutdown.
    srv.daemon_threads = True
    state = f"{resumed} held"
    print(f"node {args.id} up [{node_key[:8]}] - bt:{config.bt_port(args.id)} "
          f"http:{config.stats_port(args.id)}  "
          f"beacon:{config.BEACON_GROUP}:{config.BEACON_PORT}  "
          f"({state})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        ns.stop.set()              # let the session loop checkpoint and exit
        threads[0].join(timeout=8)
        sock.close()


if __name__ == "__main__":
    main()
