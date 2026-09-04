"""Optional dashboard for the swarm.

Purely observability, and entirely a client: point it at any node and it reads
the swarm through that node's peer table (catalog.fetch_swarm). Nodes are not
configured for it, do not report to it, and cannot tell whether anyone is
watching — the same relationship control.py and piece_map.py have.

Everything shown is derived from node snapshots alone. There is nothing else to
ask: the nodes are the only thing that knows who holds what. A node's /stats is
already public, so there is nothing for a collector to be *sent*.

Stateless: snapshots are pulled on demand and cached for POLL_TTL, so the nodes
are read at most once a second however many browsers are open — and not at all
while none is. A node that doesn't answer is simply absent.

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
  GET  /api/torrent/<info_hash>
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
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

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

# The node we read the swarm through. Any node will do: it is a way in, not a
# source of truth — every node knows the whole peer table.
SEED = ""

_SNAP_LOCK = threading.Lock()
# {"at", "snaps", "bases"} — the last fan-out. `bases` is every node address it
# reached, which is what lets us carry on when SEED itself goes away.
_SNAPS: dict = {"at": 0.0, "snaps": [], "bases": []}

def fresh_snapshots(now: float = None) -> list:
    """Every node's current snapshot, pulled through SEED at most once per
    POLL_TTL. Every view in this file is a pure function of this list.

    A node that doesn't answer is absent: liveness is "responded", not a timer.
    If SEED stops answering we retry through the addresses the last successful
    fan-out found, so restarting the node the dashboard was pointed at doesn't
    blank it."""
    now = now if now is not None else time.time()
    with _SNAP_LOCK:
        if now - _SNAPS["at"] < config.POLL_TTL:
            return _SNAPS["snaps"]
        for base in [SEED] + [b for b in _SNAPS["bases"] if b != SEED]:
            try:
                snaps, bases = catalog.fetch_swarm(base)
            except Exception:
                continue
            _SNAPS.update({"at": now, "snaps": snaps, "bases": bases})
            return snaps
        # Nothing answered: report an empty swarm rather than stale data, but
        # keep `bases` so the next poll can try them again.
        _SNAPS.update({"at": now, "snaps": []})
        return []


def piece_size(i: int, piece_length: int, total_size: int, num_pieces: int) -> int:
    """Bytes in piece i (the last piece is usually short). Mirrors piece_map."""
    if i < num_pieces - 1:
        return piece_length
    return total_size - piece_length * (num_pieces - 1)


def bucket_fracs(bits: list, num_pieces: int, cols: int) -> list:
    """Held-fraction of each display column (one column per piece when they fit).
    The dashboard draws at most WEBUI_MAX_COLS columns, so bucketing here keeps
    the payload tiny regardless of piece count. Column boundaries match
    piece_map.render_bits so the web and terminal views agree."""
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
    backs the on-demand drill-down (/torrent/<info_hash>), not the list view.
    Aggregation comes straight from swarm_stats (the same code piece_map uses),
    so the web UI and the terminal view never drift; only presentation differs.
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
        stored = sum(piece_size(i, piece_length, total_size, num_pieces)
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


def torrent_overview(meta: dict, rows: list) -> dict:
    """One dataset's row in the list view: durability and live-activity numbers,
    but no per-piece bitfields — only a per-node held-fraction (the heat strip).

    "Copies" is reported three ways because they answer different questions:
      full_copies    nodes holding the entire dataset (whole-torrent copies),
      durable_copies the weakest-link full-file count (min over files of nodes
                     holding that whole file) — the honest "how many copies do I
                     really have", since a dataset is only as replicated as its
                     least-replicated file,
      min_avail      rarest piece's holder count (reconstructable copies).
    Throughput and node activity are aggregated from each node's live state so the
    list can show what is replicating right now without the detail payload.
    """
    num_pieces = meta["num_pieces"]
    piece_length = meta["piece_length"]
    avail = swarm_stats.availability(rows, num_pieces)
    min_avail = min(avail) if avail else 0
    total_have = sum(avail)
    full_copies = sum(1 for r in rows if all(r["bits"]))

    per_file = swarm_stats.per_file(rows, meta["files"], avail)
    durable_copies = (min(f["full_copies"] for f in per_file)
                      if per_file else full_copies)

    # Per-node held fraction, ordered by label — the list's compact "spread" strip.
    spread = [{"label": r["label"], "frac": round(sum(r["bits"]) / num_pieces, 3)}
              for r in sorted(rows, key=lambda r: r["label"])]

    downloading = sum(1 for r in rows if r["progress"] < 1.0)

    return {
        "info_hash": meta["info_hash"], "name": meta["name"],
        "total_size": meta["total_size"], "num_pieces": num_pieces,
        "piece_length": piece_length, "nodes_seen": len(rows),
        "full_copies": full_copies, "durable_copies": durable_copies,
        "min_avail": min_avail,
        "redundancy": (total_have / num_pieces) if num_pieces else 0,
        "total_stored": total_have * piece_length,
        "download_rate": sum(r["dl"] for r in rows),
        "upload_rate": sum(r["ul"] for r in rows),
        "downloading": downloading, "seeding": len(rows) - downloading,
        "spread": spread,
    }


def build_overview() -> dict:
    """The list view: every dataset as one light row (no piece bitfields)."""
    return {"ts": time.time(),
            "datasets": [torrent_overview(meta, rows)
                         for meta, rows in
                         swarm_stats.collect_by_torrent(fresh_snapshots())]}


def build_torrent_detail(info_hash: str) -> dict:
    """Full detail for a single dataset, or None if no fresh node reports it."""
    snaps = fresh_snapshots()
    for meta, rows in swarm_stats.collect_by_torrent(snaps):
        if meta["info_hash"] == info_hash:
            # Stamp the response time like the other endpoints so the dashboard's
            # "updated" clock keeps ticking on the detail (merged swarm) view.
            return {"ts": time.time(), **torrent_detail(meta, rows)}
    return None


def build_transfers() -> dict:
    """In-flight transfers across the swarm: one row per (node, dataset) that is
    not yet complete, with progress, the live download rate and an ETA.

    Derived straight from the latest node snapshots — there's no history — so this
    is "what is moving right now", the live counterpart to the overview's static
    copy counts. A node that holds an incomplete copy but isn't downloading shows
    up as stalled (no ETA) rather than being hidden, so a stuck transfer is
    visible. Active transfers (an ETA) sort ahead of stalled ones, soonest first.
    """
    now = time.time()
    transfers = []
    for snap in fresh_snapshots(now):
        node = snap.get("label", snap.get("node_key", "?"))
        for t in snap.get("torrents", []):
            progress = float(t.get("progress") or 0.0)
            if progress >= 1.0 or t.get("is_seeding"):
                continue
            dl = int(t.get("download_rate") or 0)
            total = int(t.get("total_size") or 0)
            eta = (total * (1.0 - progress) / dl) if dl > 0 else None
            transfers.append({
                "node": node, "name": t.get("name", ""),
                "info_hash": t.get("info_hash_v2") or t.get("name"),
                "progress": progress,
                "download_rate": dl, "upload_rate": int(t.get("upload_rate") or 0),
                "num_peers": int(t.get("num_peers") or 0),
                "total_size": total, "eta": eta,
            })
    transfers.sort(key=lambda x: (x["eta"] is None,
                                  x["eta"] if x["eta"] is not None else 0.0,
                                  -x["progress"]))
    return {"ts": now, "transfers": transfers}


def build_nodes() -> dict:
    """Per-node storage and activity: how much each node stores, how many datasets
    it holds (and how many of those complete), and its current throughput.

    The "where is the data" question answered from the infrastructure side, the
    complement to the overview's per-dataset placement. Derived from the latest
    snapshots; `total_done` is each torrent's locally-present bytes on that node.
    """
    now = time.time()
    nodes = []
    for snap in fresh_snapshots(now):
        ts = snap.get("torrents", [])
        done = [bool(t.get("is_seeding") or float(t.get("progress") or 0) >= 1.0)
                for t in ts]
        disk = snap.get("disk") or {}
        nodes.append({
            "label": snap.get("label", snap.get("node_key", "?")),
            "datasets": len(ts), "complete": sum(done),
            "stored": sum(int(t.get("total_done") or 0) for t in ts),
            "disk_free": int(disk.get("free") or 0),
            "disk_total": int(disk.get("total") or 0),
            # Skip completed torrents' download_rate: libtorrent's decaying average
            # lingers after completion, so a node holding only complete copies would
            # otherwise show an inbound rate while not actually downloading.
            "download_rate": sum(int(t.get("download_rate") or 0)
                                 for t, c in zip(ts, done) if not c),
            "upload_rate": sum(int(t.get("upload_rate") or 0) for t in ts),
            "num_peers": sum(int(t.get("num_peers") or 0) for t in ts),
        })
    nodes.sort(key=lambda n: n["label"])
    return {"ts": now, "nodes": nodes}


def build_node_detail(label: str) -> dict:
    """One node's held datasets, or None if no fresh node reports that label.

    The drill-down from the Nodes screen: which torrents this node holds, each
    with its completion and live rate, plus the node's totals. info_hash is
    included so the UI can link every row back to that dataset's detail.
    """
    for snap in fresh_snapshots():
        if snap.get("label", snap.get("node_key")) != label:
            continue
        torrents = []
        for t in snap.get("torrents", []):
            progress = float(t.get("progress") or 0.0)
            complete = bool(t.get("is_seeding") or progress >= 1.0)
            # libtorrent's download_rate is a decaying moving average that lingers
            # for several seconds after a torrent completes. A node that holds the
            # whole dataset isn't downloading, so report 0 — otherwise a "complete"
            # row keeps showing an inbound rate and looks like it's still pulling.
            torrents.append({
                "info_hash": t.get("info_hash_v2") or t.get("name"),
                "name": t.get("name", ""),
                "state": t.get("state", ""), "progress": progress,
                "stored": int(t.get("total_done") or 0),
                "total_size": int(t.get("total_size") or 0),
                "download_rate": 0 if complete else int(t.get("download_rate") or 0),
                "upload_rate": int(t.get("upload_rate") or 0),
                "num_peers": int(t.get("num_peers") or 0),
                "complete": complete,
            })
        torrents.sort(key=lambda x: x["name"])
        disk = snap.get("disk") or {}
        return {
            "ts": time.time(),
            "label": label, "datasets": len(torrents),
            "complete": sum(1 for t in torrents if t["complete"]),
            "stored": sum(t["stored"] for t in torrents),
            "disk_free": int(disk.get("free") or 0),
            "disk_total": int(disk.get("total") or 0),
            "download_rate": sum(t["download_rate"] for t in torrents),
            "upload_rate": sum(t["upload_rate"] for t in torrents),
            "num_peers": sum(t["num_peers"] for t in torrents),
            "torrents": torrents,
        }
    return None


# Each cached endpoint shares one build per tick across all viewers. The ETag is
# keyed to the payload state (not the timestamp), so an idle swarm keeps a stable
# ETag and viewers get cheap 304s instead of resent bytes.
_CACHE_LOCK = threading.Lock()
# endpoint key -> {built_at, body, etag}. /overview is polled by every viewer;
# the heavy per-torrent detail is cached per info_hash so several operators
# drilled into different datasets don't evict each other. This caches the
# *rendering*; the snapshots underneath have their own poll cache.
_CACHE: dict = {}


def cached_payload(key: str, builder, state_key) -> tuple:
    """(body_bytes, etag) for `key`, rebuilt at most once per POLL_TTL.

    `builder()` returns the dict to serve; `state_key(data)` returns the part the
    ETag should track (so the timestamp alone doesn't churn it)."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None or now - entry["built_at"] >= config.POLL_TTL:
            data = builder()
            digest = hashlib.md5(
                json.dumps(state_key(data), sort_keys=True).encode()).hexdigest()
            entry = {"built_at": now, "body": json.dumps(data).encode(),
                     "etag": f'"{digest}"'}
            _CACHE[key] = entry
        return entry["body"], entry["etag"]


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

        def _send_cached_json(self, body: bytes, etag: str) -> None:
            """Serve a cached JSON body, honouring If-None-Match with a 304."""
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(body, "application/json",
                           extra_headers={"ETag": etag, "Cache-Control": "no-cache"})

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
                body, etag = cached_payload("overview", build_overview,
                                            lambda d: d["datasets"])
                self._send_cached_json(body, etag)
            elif path.startswith("/api/torrent/"):
                # On-demand detail for one dataset (the drill-down). Cached per
                # info_hash; build_torrent_detail returns None when no fresh node
                # reports it, which we serve as a 404 rather than caching empty.
                info_hash = path[len("/api/torrent/"):]
                body, etag = cached_payload(
                    "torrent:" + info_hash,
                    lambda: build_torrent_detail(info_hash) or {"error": "unknown"},
                    lambda d: d)
                if b'"info_hash"' not in body:  # the {"error": ...} sentinel
                    self._send(body, "application/json", 404)
                else:
                    self._send_cached_json(body, etag)
            elif path == "/api/transfers":
                body, etag = cached_payload("transfers", build_transfers,
                                            lambda d: d["transfers"])
                self._send_cached_json(body, etag)
            elif path == "/api/nodes":
                body, etag = cached_payload("nodes", build_nodes,
                                            lambda d: d["nodes"])
                self._send_cached_json(body, etag)
            elif path.startswith("/api/node/"):
                # Drill-down for one node. Cached per label; None (node not
                # reporting) is served as a 404 rather than cached empty. The
                # label is a URL-encoded "ip:port" (the ':' is percent-escaped by
                # the client), so decode it back before matching.
                label = unquote(path[len("/api/node/"):])
                body, etag = cached_payload(
                    "node:" + label,
                    lambda: build_node_detail(label) or {"error": "unknown"},
                    lambda d: d)
                if b'"label"' not in body:  # the {"error": ...} sentinel
                    self._send(body, "application/json", 404)
                else:
                    self._send_cached_json(body, etag)
            elif path.startswith("/api/"):
                # The /api/ namespace is machine-only, so an unknown endpoint
                # under it is an error — never the app shell. Falling through
                # would hand a JSON client a 200 and a page of HTML, which reads
                # as success right up until it tries to parse it.
                self._send(json.dumps({"error": "no such endpoint"}).encode(),
                           "application/json", 404)
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
        description="Optional dashboard for the swarm. Reads everything through "
                    "one node; nothing has to be configured to report to it.")
    ap.add_argument("node", nargs="?",
                    default=f"{config.HOST}:{config.STATS_PORT_BASE}",
                    help="any node's endpoint host[:port] - a way into the "
                         "swarm, not a source of truth (default: %(default)s)")
    SEED = catalog.base_url(ap.parse_args().node)

    srv = ThreadingHTTPServer((config.COLLECTOR_HOST, config.COLLECTOR_PORT),
                              make_handler())
    srv.daemon_threads = True
    print(f"collector on http://{config.COLLECTOR_HOST}:{config.COLLECTOR_PORT}/  "
          f"(web UI + /api/*) reading the swarm through {SEED}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
