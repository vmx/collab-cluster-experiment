"""Optional dashboard for the swarm, and the HTTP face of its index.

Purely a client. Who holds what comes from index.py, which follows every node's
holdings stream; this file turns that into views, and adds the live and
per-piece detail the index doesn't keep: what is moving, read off each node's
/transfers, and piece bitfields, fetched for the one dataset being looked at
from the nodes that hold it. Like the index, it needs no address and no node
knows it exists.

The nodes are read at most once per POLL_TTL however many browsers are open —
and not at all while none is.

Endpoints:
  Every machine endpoint lives under /api/ so it never collides with the SPA's
  client-side page routes (the dashboard uses the History API: /,
  /dataset/<info_hash>, /transfers, /nodes). The rule is simply: a GET that is
  not a static asset or an /api/ endpoint is served the app shell (index.html),
  so those page routes deep-link and reload correctly.

  GET  /api/overview[?limit=&q=&status=] - a page of the list view, rarest
                   copies first: one light row per dataset (size, copy counts,
                   live throughput, a per-node held-fraction strip) with NO
                   per-piece bitfields, and the swarm-wide totals above it. Both
                   the page and the totals come off the index, so neither
                   grows with the number of datasets.
  GET  /api/dataset/<info_hash>
                 - full render-ready detail for ONE dataset (per-node piece maps,
                   availability histogram, per-file replication, copies summary).
                   The heavy payload, fetched only on drill-down. 404 if no fresh
                   node reports that info_hash.
  GET  /api/transfers - {"ts", "transfers": [...]} in-flight transfers (one row
                   per incomplete (node, dataset)) with progress, rate and ETA.
  GET  /api/nodes - {"ts", "nodes": [...]} per-node storage + activity: bytes
                   stored, datasets held/complete, throughput, peer count.
  GET  /api/node/<addr>
                 - one node's held datasets (drill-down from /nodes): per torrent
                   completion, stored, rate + info_hash. 404 if not reporting.
  GET  /api/rescue?node=<node_key>[&limit=]
                 - for a node with spare space: {"ts", "candidates", "evictable"},
                   a random sample of the rarest datasets it doesn't hold, and its
                   own holdings with the most copies. See index.rescue().
  GET  /          - the web UI; any other GET path also serves the app shell.
"""
import argparse
import concurrent.futures
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import config
import index
import node_client
import swarm_stats
from index import poll

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

# How many rows a list view hands back unless asked for more. The rarest are
# what an operator acts on, and past that there is the search box: no screen
# shows every dataset, so no response carries them all.
LIST_PAGE = 50
LIST_MAX = 2000

_META_LOCK = threading.Lock()
# info_hash -> a dataset's file -> piece map. Fixed for the life of the dataset —
# it is what the info-hash hashes — so it is fetched once and never invalidated.
# Only the per-file views need it; the list view is built from holdings alone.
_META: dict = {}


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
            got = node_client.fetch_meta(base, info_hash)
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
    ids are resolved to display names. Colouring of the columns is left to the UI.
    """
    num_pieces = meta["num_pieces"]
    piece_length = meta["piece_length"]
    total_size = meta["total_size"]
    cols = min(num_pieces, config.WEBUI_MAX_COLS)
    avail = swarm_stats.availability(rows, num_pieces)
    names = {r["id"]: r["name"] for r in rows}  # node_key -> display name
    min_avail = min(avail) if avail else 0
    total_have = sum(avail)
    full_holders = [r["name"] for r in rows if all(r["bits"])]

    out_rows = []
    for r in rows:
        stored = sum(swarm_stats.piece_size(i, piece_length, total_size, num_pieces)
                     for i, b in enumerate(r["bits"]) if b)
        have = sum(r["bits"])
        complete = have == num_pieces
        num_peers = int(r.get("num_peers") or 0)
        out_rows.append({
            "name": r["name"], "addr": r["addr"], "role": "seed" if complete else "leech",
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
            "full_holders": [names.get(i, i) for i in f["full_holders"]],
            "recon_copies": f["recon_copies"],
            "partial": [{"name": names.get(i, i), "pct": pct}
                        for i, pct in f["partial"]],
        })

    return {
        "info_hash": meta["info_hash"], "name": meta["name"],
        "num_pieces": num_pieces, "piece_length": piece_length,
        "total_size": total_size, "nodes_seen": len(rows),
        "torrent": {k: meta.get(k) for k in ("creator", "comment", "creation_date",
                                              "trackers", "web_seeds", "extras")},
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
# Each is a pure function of poll(), the index's queries and the metadata cache.
# The list views are built from holdings alone — no piece bitfields anywhere near
# them — and the one view that needs bitfields fetches them for its single
# dataset.

def _moving(nodes: list) -> dict:
    """(node key, info_hash) -> that node's live transfer row. Bounded by what
    is moving, not by what is held."""
    return {(rec["stats"]["node_key"], t["info_hash"]): t
            for rec in nodes for t in rec.get("transfers") or []}


def holders_of(ds: dict, moving: dict) -> list:
    """The holder rows swarm_stats.overview_row expects — O(copies), and only
    for the datasets on the page."""
    rows = [{"name": index.name(key), "state": "complete", "progress": 1.0,
             "download_rate": 0} for key in ds["complete"]]
    for key in ds["partial"]:
        live = moving.get((key, ds["info_hash"]))
        rows.append({"name": index.name(key), "state": "downloading",
                     "progress": float(live["progress"]) if live else 0.0,
                     "download_rate": int(live["download_rate"]) if live else 0})
    return rows


def summary(nodes: list, moving: dict) -> dict:
    """The numbers above the list, none of which walk the aggregate."""
    totals = index.totals()
    return {**totals,
            "nodes": len(nodes),
            "replicating": len({h for _, h in moving}),
            "stored": totals["stored"] + sum(int(t.get("bytes_done") or 0)
                                             for t in moving.values()),
            "download_rate": sum(int(t.get("download_rate") or 0)
                                 for t in moving.values())}


def build_overview(limit: int = LIST_PAGE, query: str = "",
                   status: str = "all") -> dict:
    """The list view: a page of datasets, rarest first, and the totals above it.

    Never every dataset — no screen can show them all, and the browser used to
    sort and filter what it had been sent. Both happen here now, over an index
    kept as the nodes report changes, so the common poll touches the rarest
    classes and nothing else.
    """
    now = time.time()
    nodes = poll(now)
    limit = max(1, min(int(limit or LIST_PAGE), LIST_MAX))
    query = (query or "").strip().lower()
    moving = _moving(nodes)
    picked, matched = index.rarest(limit, query, status)
    rows = [swarm_stats.overview_row(ds, holders_of(ds, moving)) for ds in picked]
    return {"ts": now, **summary(nodes, moving), "matched": matched,
            "datasets": rows}


def build_torrent_detail(info_hash: str) -> dict:
    """Full detail for a single dataset, or None if the swarm doesn't know it.

    The only place piece bitfields are fetched, and it costs one request to each
    node that holds this dataset — not to every node, and not for anything else
    in the swarm. That is the whole reason the bitfield lives on its own
    endpoint."""
    nodes = poll()
    ds = index.dataset(info_hash)
    holders = set(ds["complete"] + ds["partial"]) if ds else set()
    have = [rec for rec in nodes
            if (rec.get("stats") or {}).get("node_key") in holders]
    # Asked of a holder, which is the only kind of node that has the .torrent to
    # answer from — and, since holding is what makes a dataset exist, the only
    # kind there is when the dataset is there at all.
    meta = meta_for(info_hash, [rec["base"] for rec in have])
    if not meta:
        return None
    holders, addrs = [], {}
    if have:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for rec, detail in zip(have, pool.map(
                    lambda r: _holding_or_none(r["base"], info_hash), have)):
                if detail:
                    key = rec["stats"]["node_key"]
                    holders.append((key, rec["name"], detail))
                    addrs[key] = rec["addr"]
    rows = swarm_stats.holder_rows(meta, holders)
    for r in rows:
        r["addr"] = addrs[r["id"]]   # what each row links to
    return {"ts": time.time(), **torrent_detail(meta, rows)}


def _holding_or_none(base: str, info_hash: str):
    try:
        return node_client.fetch_holding(base, info_hash)
    except Exception:
        return None


def build_transfers() -> dict:
    """In-flight transfers across the swarm: one row per (node, dataset) still
    moving, with progress, the live download rate and an ETA.

    Read straight off each node's /transfers — there's no history — so this is
    "what is moving right now", the live counterpart to the overview's copy
    counts. A node holding an incomplete copy but not downloading shows as
    stalled (no ETA) rather than being hidden, so a stuck transfer is visible.
    Sorted by dataset name, then node, so a row keeps its place between polls.
    """
    now = time.time()
    transfers = []
    for rec in poll(now):
        for t in rec["transfers"]:
            rate = int(t.get("download_rate") or 0)
            remaining = t["total_size"] * (1.0 - t["progress"])
            transfers.append({**t, "node": rec["name"], "addr": rec["addr"],
                              "complete": False,
                              "stored": int(t.get("bytes_done") or 0),
                              "eta": (remaining / rate) if rate > 0 else None})
    transfers.sort(key=lambda x: (x["name"], x["node"]))
    return {"ts": now, "transfers": transfers}


# --- per-node rows ------------------------------------------------------------
# The Nodes list and one node's drill-down are two slicings of the same thing:
# what a node reports about itself, plus the datasets we have accumulated from
# its holdings stream. The totals come from the node — it keeps them as it goes,
# so neither it nor we have to add up everything it holds.

def node_summary(rec: dict) -> dict:
    """One line for a node, however much it holds."""
    st = rec.get("stats") or {}
    disk = st.get("disk") or {}
    return {"addr": rec["addr"], "name": rec["name"],
            "datasets": int(st.get("held") or 0),
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
    nodes.sort(key=lambda n: (n["name"], n["addr"]))
    return {"ts": now, "nodes": nodes}


def build_node_detail(addr: str, limit: int = LIST_PAGE,
                      query: str = "") -> dict:
    """A page of one node's datasets, or None if it isn't reporting.

    In info-hash order rather than by name: a node at any size holds more than a
    screen, and ordering by something a human reads would mean sorting all of it
    per request. Finding one is what the search is for."""
    for rec in poll():
        if rec["addr"] != addr:
            continue
        moving = {t["info_hash"]: t for t in rec["transfers"]}
        rows = []
        limit = max(1, min(int(limit or LIST_PAGE), LIST_MAX))
        q = (query or "").strip().lower()
        key = (rec.get("stats") or {}).get("node_key")
        held, matched = index.held_by(key, limit, q)
        for ds in held:
            info_hash = ds["info_hash"]
            live = moving.get(info_hash)
            complete = key in ds["complete"]
            size = ds["total_size"]
            rows.append({
                "info_hash": info_hash,
                "name": ds["name"] or info_hash[:12],
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
        return {"ts": time.time(), **node_summary(rec), "matched": matched,
                "torrents": rows}
    return None


def build_rescue(node_key: str, limit: int = LIST_PAGE) -> dict:
    """What a node with spare space could take and let go; see index.rescue()."""
    now = time.time()
    poll(now)
    limit = max(1, min(int(limit or LIST_PAGE), LIST_MAX))
    return {"ts": now, **index.rescue(node_key, limit)}


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
            elif path.split("?")[0] == "/api/overview":
                q = parse_qs(urlsplit(path).query)
                self._send_json(build_overview(
                    limit=(q.get("limit") or [LIST_PAGE])[0],
                    query=(q.get("q") or [""])[0],
                    status=(q.get("status") or ["all"])[0]))
            elif path.startswith("/api/dataset/"):
                # On-demand detail for one dataset (the drill-down).
                self._send_or_404(build_torrent_detail(path[len("/api/dataset/"):]))
            elif path == "/api/transfers":
                self._send_json(build_transfers())
            elif path == "/api/nodes":
                self._send_json(build_nodes())
            elif path.startswith("/api/node/"):
                # Drill-down for one node. The addr is a URL-encoded "ip:port"
                # (the ':' is percent-escaped by the client), so decode it back
                # before matching.
                split = urlsplit(path)
                q = parse_qs(split.query)
                self._send_or_404(build_node_detail(
                    unquote(split.path[len("/api/node/"):]),
                    limit=(q.get("limit") or [LIST_PAGE])[0],
                    query=(q.get("q") or [""])[0]))
            elif path.split("?")[0] == "/api/rescue":
                q = parse_qs(urlsplit(path).query)
                node_key = (q.get("node") or [""])[0]
                if not node_key:
                    return self._send_json({"error": "node=<node_key> is required"}, 400)
                self._send_json(build_rescue(node_key,
                                             limit=(q.get("limit") or [LIST_PAGE])[0]))
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
    seed = node_client.base_url(args.node) if args.node else ""

    index.start(seed)
    srv = ThreadingHTTPServer((config.COLLECTOR_HOST, config.COLLECTOR_PORT),
                              make_handler())
    srv.daemon_threads = True
    via = seed or f"any node beaconing on {config.BEACON_GROUP}:{config.BEACON_PORT}"
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
