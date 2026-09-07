"""A collab-cluster node: the whole system, in one program.

Run one per host. Nodes find each other with a UDP multicast beacon, learn what
datasets exist by pulling each other's catalogs, and move the bytes with
BitTorrent v2. There is no tracker, no central catalog and no coordinator.

  GET  /stats                    JSON snapshot: per-torrent status and the
                                 piece bitfield behind every swarm-wide view.
                                 Already public, so there is nothing for a node
                                 to report anywhere.
  GET  /catalog                  [{"name","info_hash"}] — every dataset this node
                                 knows of, held or not.
  GET  /catalog/<info_hash>.torrent   the raw .torrent for one dataset.
  GET  /peers                    {"self": {...}, "peers": [...]} — this node's
                                 view of the swarm.
  POST /publish  {"path": ...}   hash a local file/dir into a dataset, put it in
                                 this node's catalog, and seed it in place. This
                                 is the only way data enters the swarm, and it
                                 works the same in every replication mode.
  POST /add      {"info_hash"|"name": ...}     take a known dataset (manual mode)
  POST /remove   {"info_hash"|"name": ...}     drop one

Two background threads — the libtorrent session loop (refreshes the stats
snapshot) and the sync loop (the engine, below) — while the main thread serves
the HTTP API above.
"""
import argparse
import glob
import hashlib
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
from urllib.parse import urlsplit

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


class NodeState:
    def __init__(self, node_id: int, node_key: str, ses: "lt.session", want):
        self.node_id = node_id
        # Stable swarm-wide identity (a persisted UUID). The integer node_id is a
        # local convenience (ports, data dirs); node_key is how peers tell each
        # other apart, and what the collector keys every metric by, so a restart
        # keeps the same series.
        self.node_key = node_key
        self.ses = ses
        # want(meta) -> bool: the one policy knob. See make_want().
        self.want = want
        self.lock = threading.Lock()
        # info_hash(v2 str) -> {name, save_path, ti, files, handle, complete}
        # — the data we hold. `complete` is refreshed by the session loop and
        # read by mesh(), which only offers peers to torrents still missing data.
        self.torrents: dict = {}
        # info_hash(v2 str) -> {name, path} — datasets we know exist. A superset
        # of `torrents`: in manual mode a node knows of far more than it holds.
        self.catalog: dict = {}
        self.digest = ""            # fingerprint of `catalog`, sent in the beacon
        # node_key -> {ip, bt, http, cat, last_seen} — other nodes we can see.
        # Beacons are the only way in: a node we cannot hear, we do not know.
        self.peers: dict = {}
        self.snapshot: dict = {"node_key": node_key, "torrents": []}
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


# --- catalog -----------------------------------------------------------------

def compute_digest(cat: dict) -> str:
    """A short fingerprint of a catalog's contents.

    Rides along in every beacon so a peer can tell at a glance whether our
    catalog changed since it last looked — which is what keeps the sync tick from
    re-fetching a list that hasn't moved. Order-independent (hashes are sorted),
    so two nodes that know the same datasets always agree."""
    h = hashlib.sha256()
    for ih in sorted(cat):
        h.update(ih.encode())
    return h.hexdigest()[:12]


def load_catalog(ns: NodeState) -> None:
    """Index this node's catalog directory once at startup.

    Afterwards the index is maintained in memory: we're the only writer of our
    own catalog dir, so there's no need to re-parse every .torrent each tick."""
    os.makedirs(catalog_dir(ns.node_id), exist_ok=True)
    cat = {m["info_hash"]: {"name": m["name"], "path": m["path"]}
           for m in make_torrent.list_catalog(catalog_dir(ns.node_id))}
    with ns.lock:
        ns.catalog = cat
        ns.digest = compute_digest(cat)


def store_torrent(ns: NodeState, name: str, info_hash: str, blob: bytes) -> str:
    """Write a .torrent into this node's catalog and index it.

    Written via a temp file + rename so a reader never sees a partial torrent."""
    directory = catalog_dir(ns.node_id)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{slug(name, info_hash)}.torrent")
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path)
    with ns.lock:
        ns.catalog[info_hash] = {"name": name, "path": path}
        ns.digest = compute_digest(ns.catalog)
    return path


def _ambiguous(ref: str, kind: str, matches: list) -> ValueError:
    """Refuse to guess, and hand back something that can be pasted straight back
    in — the shortened hashes below are valid references in their own right."""
    return ValueError(f"{ref!r} is an ambiguous {kind} - {len(matches)} datasets "
                      f"match; use one of these info-hashes: "
                      f"{', '.join(h[:16] for h in sorted(matches))}")


def resolve(ns: NodeState, info_hash: str = None, name: str = None) -> str:
    """Find one dataset by info-hash or by name, refusing to guess.

    An info-hash may be given in full or shortened to any unique leading portion,
    so the 16-character forms printed by `control.py list` and by the ambiguity
    error can be used as-is. Names are labels, not identifiers — two nodes can
    publish different content under the same name — so a name matching several
    datasets is an error.
    """
    with ns.lock:
        cat = dict(ns.catalog)
    if info_hash:
        ref = info_hash.strip().lower()
        matches = [ih for ih in cat if ih.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _ambiguous(ref, "info-hash", matches)
        # Nothing matched. It may have been a *name* that merely looks like a
        # hash (a directory called "deadbeef00"), so fall through rather than
        # failing on a technicality.
        name = name or info_hash
    matches = [ih for ih, m in cat.items() if m["name"] == name]
    if not matches:
        raise FileNotFoundError(name)
    if len(matches) > 1:
        raise _ambiguous(name, "name", matches)
    return matches[0]


# --- snapshot helpers ---------------------------------------------------------
# What /stats carries, and nothing more: every field below is read by something
# (the dashboard, control.py, the swarm map). A node reports its own view once
# and every viewer derives the rest, so anything no viewer reads is not measured
# here at all.

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


def torrent_dict(st, ti, files_meta) -> dict:
    info_hash_v2 = ""
    try:
        info_hash_v2 = str(st.info_hashes.v2)
    except Exception:
        pass
    # Whether this node holds the whole dataset. Decided here, once, so no
    # viewer has to re-derive it from is_seeding/progress and risk disagreeing
    # with the next viewer about what "complete" means.
    complete = bool(st.is_seeding) or st.progress >= 1.0
    return {
        "info_hash_v2": info_hash_v2,
        "name": ti.name(),
        # Per-piece ownership bitfield: which pieces (=which data) THIS node holds.
        # This is the authoritative source for the swarm-wide piece map.
        "pieces": [bool(b) for b in st.pieces],
        "piece_length": ti.piece_length(),
        "total_size": ti.total_size(),
        # Static file -> piece-range map so consumers can do per-file analysis.
        "files": files_meta,
        "progress": st.progress,
        "complete": complete,
        # libtorrent's download_rate is a decaying average that keeps reporting
        # for seconds after a torrent finishes. A node holding the whole dataset
        # is not downloading, so say so here rather than leave every viewer to
        # subtract the ghost itself (and one of them forget to).
        "download_rate": 0 if complete else st.download_rate,
        "upload_rate": st.upload_rate,
        "total_done": st.total_done,
        "num_peers": st.num_peers,
    }


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
        # Only the category the session loop actually reads: fast-resume
        # checkpoints. all_categories would additionally switch on the per-peer,
        # per-piece and per-block log streams, which libtorrent generates at high
        # volume all through a transfer and which we pop only to discard.
        "alert_mask": lt.alert.category_t.storage_notification,
        # Pace the transfer so progress is observable as it happens (see config).
        # By default libtorrent exempts loopback/LAN peers from rate limits, so
        # we must turn that off for the cap to apply within a single-host swarm.
        "upload_rate_limit": config.UPLOAD_RATE_LIMIT,
        "ignore_limits_on_local_network": False,
    }
    return lt.session(settings)


def add_torrent(ns: NodeState, info_hash: str, serve_path: str = None) -> dict:
    """Start holding a dataset this node knows about.

    `serve_path` says where the data comes from: given, it is a copy already on
    this host (what /publish was handed) and we seed it in place; omitted, we
    download a fresh copy into nodes/<id>/data/<slug>/. Returns a status dict.
    """
    with ns.lock:
        meta = ns.catalog.get(info_hash)
        held = ns.torrents.get(info_hash)
    if held:
        return {"info_hash": info_hash, "name": held["name"],
                "added": False, "note": "already present"}
    if not meta:
        raise FileNotFoundError(info_hash)

    ti = lt.torrent_info(meta["path"])
    tname = ti.name()

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

    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = save_path
    handle = ns.ses.add_torrent(atp)

    entry = {"name": tname, "save_path": save_path, "ti": ti,
             "files": file_list(ti), "handle": handle, "complete": False}
    with ns.lock:
        ns.torrents[info_hash] = entry
    # Persist immediately so a restart before any download still restores it
    # (the session loop writes the actual .resume file from the alert).
    handle.save_resume_data(SAVE_FLAGS)
    print(f"node {ns.node_id}: +'{tname}' [{info_hash[:8]}] "
          f"({len(entry['files'])} files) -> {save_path}", flush=True)
    return {"info_hash": info_hash, "name": tname, "added": True}


def remove_torrent(ns: NodeState, info_hash: str) -> dict:
    """Stop holding a dataset. It stays in the catalog — the node still knows it
    exists, it just doesn't keep a copy (and in "all" mode would take it again;
    use manual mode if you want removals to stick)."""
    with ns.lock:
        entry = ns.torrents.pop(info_hash, None)
    if not entry:
        return {"removed": False, "note": "not held"}
    ns.ses.remove_torrent(entry["handle"])
    # Drop its resume file so a restart doesn't bring the torrent back.
    try:
        os.remove(os.path.join(resume_dir(ns.node_id),
                               f"{slug(entry['name'], info_hash)}.resume"))
    except FileNotFoundError:
        pass
    print(f"node {ns.node_id}: -{entry['name']} [{info_hash[:8]}]", flush=True)
    return {"removed": True, "info_hash": info_hash, "name": entry["name"]}


def publish(ns: NodeState, path: str) -> dict:
    """Put local data into the swarm. The only way in, identical in every mode.

    Hash the path into a v2 torrent, drop it in this node's catalog, and seed it
    in place — nothing is copied. That bumps our catalog digest, so the next
    beacon tells every peer there's something new; what they do about it is their
    own want() decision.
    """
    name, info_hash, blob = make_torrent.build(path)   # raises ValueError
    with ns.lock:
        known = info_hash in ns.catalog
    if not known:
        store_torrent(ns, name, info_hash, blob)
    res = add_torrent(ns, info_hash, serve_path=path)
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
        with ns.lock:
            ns.torrents[info_hash] = {"name": ti.name(), "save_path": atp.save_path,
                                      "ti": ti, "files": file_list(ti),
                                      "handle": handle, "complete": False}
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


def session_loop(ns: NodeState) -> None:
    loops = 0
    while not ns.stop.is_set():
        ns.stop.wait(config.NODE_LOOP_INTERVAL)  # sleep, but wake promptly on stop
        loops += 1
        for a in ns.ses.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                _write_resume(ns, a)
            # save_resume_data_failed_alert: nothing to persist yet; ignore.

        with ns.lock:
            entries = list(ns.torrents.values())

        torrents = []
        for e in entries:
            t = torrent_dict(e["handle"].status(), e["ti"], e["files"])
            e["complete"] = t["complete"]      # mesh() reads this
            torrents.append(t)

        snap = {
            "node_key": ns.node_key,    # stable swarm-wide identity
            "ts": time.time(),
            "bt_port": config.bt_port(ns.node_id),
            "disk": node_disk(ns.node_id),
            "torrents": torrents,
        }
        with ns.lock:
            ns.snapshot = snap

        # Checkpoint fast-resume for any torrent whose state changed.
        if loops % RESUME_EVERY == 0:
            for e in entries:
                if e["handle"].need_save_resume_data():
                    e["handle"].save_resume_data(SAVE_FLAGS)

    flush_resume(ns)


# --- the engine --------------------------------------------------------------
# One tick, every BEACON_INTERVAL:
#   1. beacon   say who we are and what our catalog looks like
#   2. peers    drain everyone else's beacons
#   3. catalog  pull from any peer whose digest changed
#   4. want()   take datasets we don't hold but should
#   5. mesh     hand every known peer to every torrent still missing data
# That's the whole distributed system. Everything below is those five steps.

def make_want(policy: str):
    """The single policy knob: given a dataset we know of but don't hold, do we
    want a copy?

    "manual" wants nothing on its own, so a node only holds what someone asked it
    for via /add. It is the default because storing data is the one thing a node
    cannot undo cheaply — everything else it does (discovery, tracking the
    catalog, serving what it has) costs nothing and happens regardless.

    "all" mirrors everything, so the swarm converges on one complete copy per
    node with no operator input at all.
    """
    if policy == "manual":
        return lambda meta: False
    if policy == "all":
        return lambda meta: True
    raise ValueError(f"unknown replication policy: {policy!r}")


def self_beacon(ns: NodeState) -> dict:
    """Who we are, as the swarm sees us. Deliberately says nothing about our
    address: a receiver reads that off the datagram's source, so no node ever has
    to work out (or be told) its own routable IP."""
    with ns.lock:
        digest = ns.digest
    return {"v": beacon.VERSION, "node": ns.node_key,
            "bt": config.bt_port(ns.node_id),
            "http": config.stats_port(ns.node_id), "cat": digest}


def note_peer(ns: NodeState, key: str, ip: str, bt: int, http: int,
              cat: str, last_seen: float) -> None:
    """Record (or refresh) a peer. Never records ourselves."""
    if not key or key == ns.node_key:
        return
    with ns.lock:
        peer = ns.peers.setdefault(key, {"pulled": None})
        # Never let an older sighting overwrite a fresher one.
        if last_seen >= peer.get("last_seen", 0):
            peer.update({"ip": ip, "bt": bt, "http": http, "cat": cat,
                         "last_seen": last_seen})


def drain_beacons(ns: NodeState, sock) -> None:
    """Record everyone who announced themselves since the last tick."""
    now = time.time()
    for msg, ip in beacon.drain(sock):
        note_peer(ns, msg.get("node"), ip, msg.get("bt"), msg.get("http"),
                  msg.get("cat"), now)


def expire_peers(ns: NodeState) -> None:
    cutoff = time.time() - config.PEER_STALE_AFTER
    with ns.lock:
        for key in [k for k, p in ns.peers.items()
                    if p.get("last_seen", 0) < cutoff]:
            del ns.peers[key]


def pull_catalog(ns: NodeState, key: str, peer: dict) -> None:
    """Learn about datasets a peer knows and we don't, and fetch their .torrents.

    Gated on the peer's beacon digest, so a settled swarm does no HTTP at all."""
    digest = peer.get("cat")
    if digest and digest == peer.get("pulled"):
        return
    base = f"http://{peer['ip']}:{peer['http']}"
    entries = catalog.fetch_list(base)
    for meta in entries:
        info_hash = meta.get("info_hash")
        with ns.lock:
            if not info_hash or info_hash in ns.catalog:
                continue
        blob = catalog.fetch_torrent_bytes(base, info_hash)
        # Trust nothing about the filename or the peer's claimed name: parse the
        # torrent and take its identity from the bytes themselves.
        ti = lt.torrent_info(lt.bdecode(blob))
        got = str(ti.info_hashes().v2)
        if got != info_hash:
            print(f"node {ns.node_id}: {key[:8]} offered {info_hash[:8]} but sent "
                  f"{got[:8]}; ignoring", flush=True)
            continue
        store_torrent(ns, ti.name(), got, blob)
        print(f"node {ns.node_id}: learned '{ti.name()}' [{got[:8]}] "
              f"from {key[:8]}", flush=True)
    with ns.lock:
        if key in ns.peers:
            ns.peers[key]["pulled"] = digest


def take_wanted(ns: NodeState) -> None:
    """Step 4: the policy. Everything the node knows of but doesn't hold gets
    offered to want(); whatever it accepts starts downloading."""
    with ns.lock:
        pending = [(ih, dict(meta)) for ih, meta in ns.catalog.items()
                   if ih not in ns.torrents]
    for info_hash, meta in pending:
        if not ns.want(meta):
            continue
        try:
            add_torrent(ns, info_hash)
        except Exception as exc:
            print(f"node {ns.node_id}: can't take {meta['name']!r} "
                  f"[{info_hash[:8]}]: {exc}", flush=True)


def mesh(ns: NodeState) -> None:
    """Step 5: what the tracker used to do. Hand every known peer to every
    torrent that still needs data, and let libtorrent take it from there.

    A complete torrent is skipped because it needs nobody: in BitTorrent the
    side that wants the bytes opens the connection, and libtorrent closes a
    seed-to-seed connection as soon as the handshake shows neither end has
    anything to offer. So a settled swarm holds no peer connections at all and
    does nothing here — the tick costs what is moving, not what is stored. A
    node that restarts and still wants data re-offers on its own next tick,
    which is what heals the swarm; a node that restarts holding everything has
    nothing to heal."""
    with ns.lock:
        handles = [e["handle"] for e in ns.torrents.values() if not e["complete"]]
        addrs = [(p["ip"], p["bt"]) for p in ns.peers.values()
                 if p.get("ip") and p.get("bt")]
    for handle in handles:
        for addr in addrs:
            try:
                handle.connect_peer(addr)
            except Exception:
                pass  # torrent not ready, or peer already known


def pull_catalogs(ns: NodeState) -> None:
    """Step 3: learn what everyone else knows exists."""
    with ns.lock:
        peers = [(k, dict(p)) for k, p in ns.peers.items()]
    for key, peer in peers:
        if not peer.get("ip") or not peer.get("http"):
            continue
        try:
            pull_catalog(ns, key, peer)
        except Exception as exc:
            print(f"node {ns.node_id}: catalog pull from {key[:8]} failed: {exc}",
                  flush=True)


def sync_loop(ns: NodeState, sock) -> None:
    """The engine. One tick of the five steps, forever."""
    while not ns.stop.is_set():
        try:
            beacon.send(sock, self_beacon(ns))       # 1. say who we are
            drain_beacons(ns, sock)                  # 2. hear who else is here
            expire_peers(ns)
            pull_catalogs(ns)                        # 3. learn what exists
            take_wanted(ns)                          # 4. decide what to hold
            mesh(ns)                                 # 5. wire peers into torrents
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
            path = urlsplit(self.path).path
            if path == "/stats":
                with ns.lock:
                    snap = ns.snapshot
                self._send_json(snap)
            elif path == "/catalog":
                with ns.lock:
                    cat = [{"name": m["name"], "info_hash": ih}
                           for ih, m in ns.catalog.items()]
                self._send_json(sorted(cat, key=lambda m: (m["name"], m["info_hash"])))
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
                          "http": p.get("http"), "cat": p.get("cat"),
                          "age": round(now - p.get("last_seen", now))}
                         for key, p in ns.peers.items()]
                held = len(ns.torrents)
                known = len(ns.catalog)
            me = self_beacon(ns)
            me.update({"held": held, "known": known})
            return {"self": me, "peers": sorted(peers, key=lambda p: p["node"])}

        def _catalog_file(self, name: str):
            # Addressed by full v2 info-hash. The readable slug is only how the
            # file is *stored*; no protocol depends on it.
            if not name.endswith(".torrent"):
                return self._send_json({"error": "not found"}, 404)
            info_hash = name[:-len(".torrent")].lower()
            if not _HEX64.match(info_hash):
                return self._send_json({"error": "not a v2 info-hash"}, 400)
            with ns.lock:
                meta = ns.catalog.get(info_hash)
            if not meta:
                return self._send_json({"error": "not found"}, 404)
            try:
                with open(meta["path"], "rb") as f:
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
                    info_hash = resolve(ns, body.get("info_hash"), body.get("name"))
                    self._send_json(add_torrent(ns, info_hash))
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
        description="A collab-cluster node: peer discovery, catalog and "
                    "replication in one self-sufficient process.")
    # A node-local slot number: picks this node's data dir (nodes/<id>/) and, so
    # several nodes can share one host in a dev run, offsets its ports. It is not
    # how the node is identified in the swarm (that's the node_key UUID). One
    # node per host is the common case, so it defaults to 0.
    ap.add_argument("--id", type=int, default=0)
    # What a node does with a dataset it discovers but doesn't hold. Defaults to
    # manual, so a node never commits disk that wasn't asked for.
    ap.add_argument("--replicate", choices=["manual", "all"], default="manual",
                    help="what to do with datasets this node discovers: "
                         "'manual' (default) takes nothing unless told to with "
                         "control.py add; 'all' mirrors every dataset it learns "
                         "about")
    args = ap.parse_args()

    # Treat SIGTERM like Ctrl-C (raise KeyboardInterrupt) so the node shuts down
    # gracefully — checkpointing fast-resume — when stopped by a process manager
    # or `kill`, not just by an interactive Ctrl-C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    node_key = load_or_create_node_key(args.id)
    ns = NodeState(args.id, node_key, make_session(args.id), make_want(args.replicate))
    load_catalog(ns)                 # what this node already knows exists
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
    state = f"{len(ns.catalog)} known, {resumed} held"
    print(f"node {args.id} up [{node_key[:8]}] - bt:{config.bt_port(args.id)} "
          f"http:{config.stats_port(args.id)}  "
          f"beacon:{config.BEACON_GROUP}:{config.BEACON_PORT}  "
          f"replicate:{args.replicate}  ({state})", flush=True)
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
