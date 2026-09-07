"""Optional dashboard for the swarm.

Purely observability, and entirely a client: it reads the whole swarm through
any one node's peer table (catalog.fetch_swarm), and finds that node the way
nodes find each other — by listening to the multicast beacon. So it is told no
addresses and configured with nothing, exactly like the nodes it watches. Nor
are they configured for it: they do not report to it and cannot tell whether
anyone is watching, since the dashboard only ever listens and never beacons
back. The same relationship control.py has.

Everything shown is derived from what the nodes say. There is nothing else to
ask: the nodes are the only thing that knows who holds what, and their API is
already public, so there is nothing for a collector to be *sent*.

A refresh is deliberately not a snapshot of everything. Each node is asked three
things, and only the first grows with nothing at all:

  /stats      what the node is as a whole, including its cursor. Constant size.
  /holdings   which datasets it has — but only when its cursor has moved, and
              then only the transitions since the last one we saw.
  /transfers  what is moving right now, bounded by what is in flight.

The catalog is not among them, because there isn't one to ask for: a dataset
exists because some node holds it, so unioning the holdings streams both lists
the datasets and counts their copies. That also makes this the place where the
swarm's catalog exists at all — no node has one — and the place where the
difference between "nobody holds this any more" and "the node that holds it is
down" would be settled, since only something watching over time can tell them
apart.

So a settled swarm costs one small request per node, and a busy one costs the
changes and nothing else. Which datasets a node holds is *accumulated* here
(_NODES below) rather than refetched — that state is the price of the cursor,
and it is what makes this affordable on a swarm holding far more than it moves.
Piece bitfields are never part of a refresh: they are fetched for the one dataset
being looked at, from the nodes that hold it.

The nodes are read at most once per POLL_TTL however many browsers are open —
and not at all while none is. A node that doesn't answer is simply absent, but
its cursor is kept, so a node that blips does not cost a full re-list.

Endpoints:
  Every machine endpoint lives under /api/ so it never collides with the SPA's
  client-side page routes (the dashboard uses the History API: /,
  /dataset/<info_hash>, /transfers, /nodes). The rule is simply: a GET that is
  not a static asset or an /api/ endpoint is served the app shell (index.html),
  so those page routes deep-link and reload correctly.

  GET  /api/overview - {"ts", "datasets": [...]} the list view: one light row per
                   dataset (size, copy counts, live throughput, a per-node
                   held-fraction strip) with NO per-piece bitfields, so it stays
                   small and cheap to poll no matter how many datasets/nodes.
  GET  /api/dataset/<info_hash>
                 - full render-ready detail for ONE dataset (per-node piece maps,
                   availability histogram, per-file replication, copies summary).
                   The heavy payload, fetched only on drill-down. 404 if no fresh
                   node reports that info_hash.
  GET  /api/transfers - {"ts", "transfers": [...]} in-flight transfers (one row
                   per incomplete (node, dataset)) with progress, rate and ETA.
  GET  /api/nodes - {"ts", "nodes": [...]} per-node storage + activity: bytes
                   stored, datasets held/complete, throughput, peer count.
  GET  /api/node/<label>
                 - one node's held datasets (drill-down from /nodes): per torrent
                   completion, stored, rate + info_hash. 404 if not reporting.
  GET  /          - the web UI; any other GET path also serves the app shell.
"""
import argparse
import concurrent.futures
import json
import os
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import beacon
import catalog
import config
import swarm_stats

# The web UI is plain static files (no build step); the collector is already the
# one inbound service, so it serves them alongside the data endpoints.
WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")
# path -> (filename under WEBUI_DIR, content-type). A small whitelist keeps the
# static surface explicit and sidesteps any path-traversal concern.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/tutuca.js": ("tutuca.js", "text/javascript; charset=utf-8"),
}

# A node named on the command line, for where multicast doesn't reach. Normally
# empty: any node will do, so the beacon picks one. Whichever way it is found,
# it is a way in and not a source of truth — every node knows the whole swarm.
SEED = ""

_HEARD_LOCK = threading.Lock()
# node_key -> {"base", "at"} — nodes heard beaconing lately. This stands in for
# the configuration the dashboard doesn't have: it listens where the nodes
# announce themselves, and any one of them is a way in.
_HEARD: dict = {}

_POLL_LOCK = threading.Lock()
# When we last fanned out, and every node address it reached — which is what lets
# us carry on when our way in goes away.
_POLL: dict = {"at": 0.0, "bases": []}
# node_key -> {"base", "label", "stats", "cursor", "holdings", "transfers",
#              "live"} — what we know about each node, carried between polls.
# `holdings` (info_hash -> state) is built up from the transitions each node
# reports and kept; `cursor` is where we are in that node's stream, opaque and
# handed straight back. This is the only state the dashboard keeps, and it exists
# so a refresh costs the changes rather than the whole world.
_NODES: dict = {}

_META_LOCK = threading.Lock()
# info_hash -> a dataset's file -> piece map. Fixed for the life of the dataset —
# it is what the info-hash hashes — so it is fetched once and never invalidated.
# Only the per-file views need it; the list view is built from holdings alone.
_META: dict = {}


def listen_for_nodes() -> None:
    """Track the nodes announcing themselves on the local segment, forever.

    The dashboard finds its way into the swarm exactly as a node does, which is
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
    the dashboard normally needs no address at all; the middle one is what keeps
    it reading through a node whose beacons we happen to be missing."""
    with _HEARD_LOCK:
        heard = [e["base"] for e in sorted(_HEARD.values(),
                                           key=lambda e: -e["at"])]
    out = []
    for base in ([SEED] if SEED else []) + _POLL["bases"] + heard:
        if base not in out:
            out.append(base)
    return out


# --- following the nodes ------------------------------------------------------
# One refresh, and the two rules that keep it from growing with the swarm: ask
# every node what it is (cheap, always), and ask what it holds only when it says
# that changed (and then only for the change).

def follow(rec: dict, base: str) -> None:
    """Bring one node's holdings up to date by following its cursor.

    Two attempts at most: with the cursor we hold, and — if the node says it
    cannot answer from that one — with none, taking the full list. Nothing here
    parses the cursor. That is the node's business, which is exactly why it can
    tell us the cursor is stale instead of us having to work it out: a restarted
    node would otherwise answer "nothing has changed since 4417233" forever, and
    be believed.
    """
    for cursor in (rec["cursor"], None):
        held = dict(rec["holdings"]) if cursor else {}
        try:
            while True:
                page = catalog.fetch_holdings(base, since=cursor)
                for row in page.get("holdings") or []:
                    if row.get("state") == "gone":
                        # The tombstone. Without it a dropped dataset would be
                        # indistinguishable from one simply not mentioned — and
                        # since holding is what makes a dataset exist, the last
                        # of these is a dataset leaving the swarm.
                        held.pop(row["info_hash"], None)
                    else:
                        held[row["info_hash"]] = row
                cursor = page["cursor"]
                if not page.get("more"):
                    break
        except catalog.Resync:
            continue
        rec["cursor"], rec["holdings"] = cursor, held
        return


def refresh(st: dict, now: float) -> None:
    """Take in one node's current state. Called per node, in parallel."""
    key, label = st["node_key"], st.get("label", st["node_key"])
    base = f"http://{label}"
    rec = _NODES.setdefault(key, {"cursor": None, "holdings": {}, "transfers": []})
    rec.update({"base": base, "label": label, "stats": st, "at": now})
    try:
        # The whole economy of this file: holdings are refetched only when the
        # node's cursor says something actually changed.
        if st.get("cursor") != rec["cursor"]:
            follow(rec, base)
        rec["transfers"] = catalog.fetch_transfers(base)
    except Exception:
        pass


def poll(now: float = None) -> list:
    """Every live node's record, refreshed at most once per POLL_TTL.

    Every view in this file is a pure function of this list plus the dataset
    metadata cache. A node that doesn't answer drops out of it but keeps its
    record, so a node that blips comes back on its cursor rather than re-listing
    everything it holds."""
    now = now if now is not None else time.time()
    with _POLL_LOCK:
        if now - _POLL["at"] >= config.POLL_TTL:
            _POLL["at"] = now
            for base in ways_in():
                try:
                    stats, bases = catalog.fetch_swarm(base)
                except Exception:
                    continue
                _POLL["bases"] = bases
                with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                    list(pool.map(lambda st: refresh(st, now), stats))
                answered = {st["node_key"] for st in stats}
                for key, rec in _NODES.items():
                    rec["live"] = key in answered
                break
        return [rec for rec in _NODES.values() if rec.get("live")]


def meta_for(info_hash: str, bases: list) -> dict:
    """A dataset's file -> piece map, fetched once from a holder and kept.

    Immutable, so there is nothing to invalidate. Only the per-file drill-down
    needs it — the list view is built from holdings alone — so this is one
    request per dataset *looked at*, not per dataset that exists."""
    with _META_LOCK:
        got = _META.get(info_hash)
    if got:
        return got
    for base in bases:
        try:
            got = catalog.fetch_meta(base, info_hash)
        except Exception:
            continue
        with _META_LOCK:
            _META[info_hash] = got
        return got
    return None


# --- one dataset's pieces -----------------------------------------------------
# The only place piece bitfields are handled, and only ever for the dataset being
# looked at. Bucketed into display columns here rather than shipped raw, so the
# payload stays small even for a torrent with thousands of pieces.

def bucket_fracs(bits: list, num_pieces: int, cols: int) -> list:
    """Held-fraction of each display column (one column per piece when they fit).
    The dashboard draws at most WEBUI_MAX_COLS columns, so bucketing here keeps
    the payload tiny regardless of piece count. Column boundaries match
    control.py's render_bits so the web and terminal views agree."""
    if cols >= num_pieces:
        return [1.0 if b else 0.0 for b in bits]
    out = []
    for c in range(cols):
        seg = bits[c * num_pieces // cols:(c + 1) * num_pieces // cols]
        out.append(round(sum(1 for b in seg if b) / len(seg), 3) if seg else 0.0)
    return out


def bucket_avail(avail: list, num_pieces: int, cols: int) -> list:
    """Worst-case (min) holder count per display column. Mirrors render_avail."""
    if cols >= num_pieces:
        return list(avail)
    return [min(avail[c * num_pieces // cols:(c + 1) * num_pieces // cols])
            for c in range(cols)]


def torrent_detail(meta: dict, rows: list) -> dict:
    """The full render-ready view of one torrent: per-node piece maps, the
    availability row + histogram, per-file replication and the copies summary.

    This is the heavy payload — it carries bucketed per-node bitfields — so it
    backs the on-demand drill-down (/api/dataset/<info_hash>), not the list view.
    Aggregation comes straight from swarm_stats (the same code the map uses),
    so the web UI and the terminal map never drift; only presentation differs.
    Piece bitfields are bucketed into display columns here rather than shipped raw,
    so the payload stays small even for torrents with thousands of pieces; holder
    ids are resolved to display labels. Colouring of the columns is left to the UI.
    """
    num_pieces = meta["num_pieces"]
    piece_length = meta["piece_length"]
    total_size = meta["total_size"]
    cols = min(num_pieces, config.WEBUI_MAX_COLS)
    avail = swarm_stats.availability(rows, num_pieces)
    labels = {r["id"]: r["label"] for r in rows}  # node_key -> display label
    min_avail = min(avail) if avail else 0
    total_have = sum(avail)
    full_holders = [r["label"] for r in rows if all(r["bits"])]

    out_rows = []
    for r in rows:
        stored = sum(swarm_stats.piece_size(i, piece_length, total_size, num_pieces)
                     for i, b in enumerate(r["bits"]) if b)
        have = sum(r["bits"])
        complete = have == num_pieces
        num_peers = int(r.get("num_peers") or 0)
        out_rows.append({
            "label": r["label"], "role": "seed" if complete else "leech",
            "have": have, "stored": stored,
            "num_peers": num_peers,
            # A node still needing data with 0 peers is stuck; a complete one
            # is just idle-seeding.
            "isolated": (not complete) and num_peers == 0,
            "cells": bucket_fracs(r["bits"], num_pieces, cols),
        })

    # Availability histogram: how many pieces are held by exactly k nodes.
    histogram = [{"holders": k, "pieces": cnt}
                 for k in range(len(rows), -1, -1)
                 for cnt in [sum(1 for a in avail if a == k)] if cnt]

    files = []
    for f in swarm_stats.per_file(rows, meta["files"], avail):
        files.append({
            "path": f["path"], "size": f["size"],
            "full_copies": f["full_copies"],
            "full_holders": [labels.get(i, i) for i in f["full_holders"]],
            "recon_copies": f["recon_copies"],
            "partial": [{"label": labels.get(i, i), "pct": pct}
                        for i, pct in f["partial"]],
        })

    return {
        "info_hash": meta["info_hash"], "name": meta["name"],
        "num_pieces": num_pieces, "piece_length": piece_length,
        "total_size": total_size, "nodes_seen": len(rows),
        "rows": out_rows,
        "avail_cells": bucket_avail(avail, num_pieces, cols),
        "histogram": histogram, "files": files,
        "summary": {
            "full_copies": len(full_holders), "full_holders": full_holders,
            "min_avail": min_avail,
            "redundancy": (total_have / num_pieces) if num_pieces else 0,
            "fully_available": min_avail >= 1,
            "total_stored": total_have * piece_length,
        },
    }


# --- the views ----------------------------------------------------------------
# Each is a pure function of poll() plus the metadata cache. The list views are
# built from holdings alone — no piece bitfields anywhere near them — and the one
# view that needs bitfields fetches them for its single dataset.

def build_overview() -> dict:
    """The list view: one light row per dataset in the swarm.

    The dataset list and the copy counts are the same pass over the same data —
    the union of what the nodes hold — because that union is the catalog. No
    per-dataset lookup happens here at all: everything on a row comes out of the
    holdings stream itself.
    """
    now = time.time()
    catalog = swarm_stats.catalog_from(
        [(rec["label"], rec["holdings"], rec["transfers"]) for rec in poll(now)])
    return {"ts": now,
            "datasets": [swarm_stats.overview_row(meta, holders)
                         for meta, holders in catalog.values()]}


def build_torrent_detail(info_hash: str) -> dict:
    """Full detail for a single dataset, or None if the swarm doesn't know it.

    The only place piece bitfields are fetched, and it costs one request to each
    node that holds this dataset — not to every node, and not for anything else
    in the catalog. That is the whole reason the bitfield lives on its own
    endpoint."""
    nodes = poll()
    have = [rec for rec in nodes if info_hash in rec["holdings"]]
    # Asked of a holder, which is the only kind of node that has the .torrent to
    # answer from — and, since holding is what makes a dataset exist, the only
    # kind there is when the dataset is there at all.
    meta = meta_for(info_hash, [rec["base"] for rec in have])
    if not meta:
        return None
    holders = []
    if have:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for rec, detail in zip(have, pool.map(
                    lambda r: _holding_or_none(r["base"], info_hash), have)):
                if detail:
                    holders.append((rec["stats"]["node_key"], rec["label"], detail))
    rows = swarm_stats.holder_rows(meta, holders)
    return {"ts": time.time(), **torrent_detail(meta, rows)}


def _holding_or_none(base: str, info_hash: str):
    try:
        return catalog.fetch_holding(base, info_hash)
    except Exception:
        return None


def build_transfers() -> dict:
    """In-flight transfers across the swarm: one row per (node, dataset) still
    moving, with progress, the live download rate and an ETA.

    Read straight off each node's /transfers — there's no history — so this is
    "what is moving right now", the live counterpart to the overview's copy
    counts. A node holding an incomplete copy but not downloading shows as
    stalled (no ETA) rather than being hidden, so a stuck transfer is visible.
    Active transfers (an ETA) sort ahead of stalled ones, soonest first.
    """
    now = time.time()
    transfers = []
    for rec in poll(now):
        for t in rec["transfers"]:
            rate = int(t.get("download_rate") or 0)
            remaining = t["total_size"] * (1.0 - t["progress"])
            transfers.append({**t, "node": rec["label"], "complete": False,
                              "stored": int(t.get("bytes_done") or 0),
                              "eta": (remaining / rate) if rate > 0 else None})
    transfers.sort(key=lambda x: (x["eta"] is None,
                                  x["eta"] if x["eta"] is not None else 0.0,
                                  -x["progress"]))
    return {"ts": now, "transfers": transfers}


# --- per-node rows ------------------------------------------------------------
# The Nodes list and one node's drill-down are two slicings of the same thing:
# what a node reports about itself, plus the datasets we have accumulated from
# its holdings stream. The totals come from the node — it keeps them as it goes,
# so neither it nor we have to add up everything it holds.

def node_summary(rec: dict) -> dict:
    """One line for a node, however large its catalog grows."""
    st = rec.get("stats") or {}
    disk = st.get("disk") or {}
    return {"label": rec["label"],
            "datasets": len(rec["holdings"]),
            "complete": int(st.get("complete") or 0),
            "stored": int(st.get("stored") or 0),
            "download_rate": int(st.get("download_rate") or 0),
            "upload_rate": int(st.get("upload_rate") or 0),
            "num_peers": int(st.get("num_peers") or 0),
            "disk_free": int(disk.get("free") or 0),
            "disk_total": int(disk.get("total") or 0)}


def build_nodes() -> dict:
    """Per-node storage and activity: how much each node stores, how many
    datasets it holds (and how many of those complete), and its throughput.

    The "where is the data" question answered from the infrastructure side, the
    complement to the overview's per-dataset placement."""
    now = time.time()
    nodes = [node_summary(rec) for rec in poll(now)]
    nodes.sort(key=lambda n: n["label"])
    return {"ts": now, "nodes": nodes}


def build_node_detail(label: str) -> dict:
    """One node's held datasets, or None if it isn't reporting.

    The drill-down from the Nodes screen. Names and sizes ride along in the
    holdings rows, so this needs nothing but what the poll already brought
    back."""
    for rec in poll():
        if rec["label"] != label:
            continue
        moving = {t["info_hash"]: t for t in rec["transfers"]}
        rows = []
        for info_hash, row in rec["holdings"].items():
            live = moving.get(info_hash)
            state = row.get("state")
            complete = state == "complete"
            size = int(row.get("total_size") or 0)
            rows.append({
                "info_hash": info_hash,
                "name": row.get("name") or info_hash[:12],
                "total_size": size,
                "complete": complete,
                "progress": 1.0 if complete
                            else float(live["progress"]) if live else 0.0,
                "stored": size if complete
                          else int(live["bytes_done"]) if live else 0,
                "download_rate": int(live["download_rate"]) if live else 0,
                "upload_rate": int(live["upload_rate"]) if live else 0,
                "num_peers": int(live["num_peers"]) if live else 0,
            })
        rows.sort(key=lambda r: r["name"])
        return {"ts": time.time(), **node_summary(rec), "torrents": rows}
    return None


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle_one_request(self):
            # A client that goes away mid-response — a browser navigating off
            # the page, a reload, a curl piped into head — leaves us writing to
            # a closed socket. That is normal traffic, not an error, but
            # socketserver's default is to print a full traceback per dropped
            # request, which buries anything real in the log.
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _send(self, body: bytes, ctype: str = "text/plain", code: int = 200,
                  extra_headers: dict = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, data, code: int = 200) -> None:
            self._send(json.dumps(data).encode(), "application/json", code,
                       {"Cache-Control": "no-cache"})

        def _send_or_404(self, data) -> None:
            """A builder returns None when no fresh node reports what was asked
            for — an unknown dataset or node, which is a 404 and not an empty
            page of data."""
            if data is None:
                self._send_json({"error": "unknown"}, 404)
            else:
                self._send_json(data)

        def _send_static(self, filename: str, ctype: str) -> None:
            try:
                with open(os.path.join(WEBUI_DIR, filename), "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send(b"web UI not found (is ./webui/ present?)",
                                  code=404)
            self._send(body, ctype)

        def do_GET(self):
            path = self.path
            # Every data/machine endpoint lives under /api/ so it can't collide
            # with the SPA's client-side page routes (/, /dataset/<hash>,
            # /transfers, /nodes), which all fall through to the app shell below.
            if path in STATIC_FILES:
                filename, ctype = STATIC_FILES[path]
                self._send_static(filename, ctype)
            elif path == "/api/overview":
                self._send_json(build_overview())
            elif path.startswith("/api/dataset/"):
                # On-demand detail for one dataset (the drill-down).
                self._send_or_404(build_torrent_detail(path[len("/api/dataset/"):]))
            elif path == "/api/transfers":
                self._send_json(build_transfers())
            elif path == "/api/nodes":
                self._send_json(build_nodes())
            elif path.startswith("/api/node/"):
                # Drill-down for one node. The label is a URL-encoded "ip:port"
                # (the ':' is percent-escaped by the client), so decode it back
                # before matching.
                self._send_or_404(
                    build_node_detail(unquote(path[len("/api/node/"):])))
            elif path.startswith("/api/"):
                # The /api/ namespace is machine-only, so an unknown endpoint
                # under it is an error — never the app shell. Falling through
                # would hand a JSON client a 200 and a page of HTML, which reads
                # as success right up until it tries to parse it.
                self._send_json({"error": "no such endpoint"}, 404)
            else:
                # SPA fallback: any other GET is a client-side page route
                # (/, /dataset/<hash>, /transfers, /nodes, ...). Serve the app
                # shell and let the browser route it; <base href="/"> keeps its
                # assets resolving from the root rather than under a nested path.
                self._send_static("index.html", "text/html; charset=utf-8")

    return Handler


def main() -> None:
    global SEED
    ap = argparse.ArgumentParser(
        description="Optional dashboard for the swarm. Finds a node on the "
                    "beacon and reads everything through it, so it needs no "
                    "configuration; nothing reports to it, or knows it is there.")
    ap.add_argument("node", nargs="?", default="",
                    help="read the swarm through this node, host[:port], instead "
                         "of finding one on the beacon - for where multicast "
                         "doesn't reach. Any node will do: it is a way into the "
                         "swarm, not a source of truth.")
    args = ap.parse_args()
    SEED = catalog.base_url(args.node) if args.node else ""

    threading.Thread(target=listen_for_nodes, daemon=True).start()
    srv = ThreadingHTTPServer((config.COLLECTOR_HOST, config.COLLECTOR_PORT),
                              make_handler())
    srv.daemon_threads = True
    via = SEED or f"any node beaconing on {config.BEACON_GROUP}:{config.BEACON_PORT}"
    print(f"collector on http://{config.COLLECTOR_HOST}:{config.COLLECTOR_PORT}/  "
          f"(web UI + /api/*) reading the swarm through {via}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
