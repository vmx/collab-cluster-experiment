"""Optional dashboard for the swarm.

Purely observability, and entirely a client: it reads the whole swarm through
any one node's peer table (catalog.fetch_swarm), and finds that node the way
nodes find each other — by listening to the multicast beacon. So it is told no
addresses and configured with nothing, exactly like the nodes it watches. Nor
are they configured for it: they do not report to it and cannot tell whether
anyone is watching, since the dashboard only ever listens and never beacons
back. The same relationship control.py has.

Everything shown is derived from node snapshots alone. There is nothing else to
ask: the nodes are the only thing that knows who holds what. A node's /stats is
already public, so there is nothing for a collector to be *sent*.

Stateless: snapshots are pulled on demand and cached for POLL_TTL, so the nodes
are read at most once a second however many browsers are open — and not at all
while none is. That one cache is the whole rate limit; each request then renders
from the snapshots it finds there. A node that doesn't answer is simply absent.

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

_SNAP_LOCK = threading.Lock()
# {"at", "snaps", "bases"} — the last fan-out. `bases` is every node address it
# reached, which is what lets us carry on when our way in goes away.
_SNAPS: dict = {"at": 0.0, "snaps": [], "bases": []}


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
    for base in ([SEED] if SEED else []) + _SNAPS["bases"] + heard:
        if base not in out:
            out.append(base)
    return out


def fresh_snapshots(now: float = None) -> list:
    """Every node's current snapshot, pulled through one of them at most once per
    POLL_TTL. Every view in this file is a pure function of this list.

    A node that doesn't answer is absent: liveness is "responded", not a timer.
    The same goes for the node we read through — when it stops answering we work
    down the rest of ways_in(), so restarting it doesn't blank the dashboard."""
    now = now if now is not None else time.time()
    with _SNAP_LOCK:
        if now - _SNAPS["at"] < config.POLL_TTL:
            return _SNAPS["snaps"]
        for base in ways_in():
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

    downloading = sum(1 for r in rows if not r["complete"])

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


# --- per-node rows -----------------------------------------------------------
# The Nodes list, one node's drill-down and the in-flight transfers are three
# slicings of the same table: one row per (node, dataset) the node holds. Built
# once here so the three views cannot drift apart, and aggregated before it is
# served so the endpoints that don't need the rows don't carry them.

def node_label(snap: dict) -> str:
    """How a node is named in every view: the address we reached it at. A node
    cannot supply this itself — it never learns its own address."""
    return snap.get("label", snap.get("node_key", "?"))


def node_disk(snap: dict) -> dict:
    disk = snap.get("disk") or {}
    return {"disk_free": int(disk.get("free") or 0),
            "disk_total": int(disk.get("total") or 0)}


def node_rows(snap: dict) -> list:
    """One node's datasets, one row each. `stored` is the bytes actually present
    on that node; `complete` and the ghost-free `download_rate` are decided by
    the node itself (see node.torrent_dict), not re-derived here."""
    return [{"info_hash": t.get("info_hash_v2") or t.get("name"),
             "name": t.get("name", ""),
             "progress": float(t.get("progress") or 0.0),
             "complete": bool(t.get("complete")),
             "stored": int(t.get("total_done") or 0),
             "total_size": int(t.get("total_size") or 0),
             "download_rate": int(t.get("download_rate") or 0),
             "upload_rate": int(t.get("upload_rate") or 0),
             "num_peers": int(t.get("num_peers") or 0)}
            for t in snap.get("torrents", [])]


def node_totals(rows: list) -> dict:
    """What one node adds up to across the datasets it holds."""
    return {"datasets": len(rows),
            "complete": sum(1 for r in rows if r["complete"]),
            "stored": sum(r["stored"] for r in rows),
            "download_rate": sum(r["download_rate"] for r in rows),
            "upload_rate": sum(r["upload_rate"] for r in rows),
            "num_peers": sum(r["num_peers"] for r in rows)}


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
        for r in node_rows(snap):
            if r["complete"]:
                continue
            dl = r["download_rate"]
            remaining = r["total_size"] * (1.0 - r["progress"])
            transfers.append({**r, "node": node_label(snap),
                              "eta": (remaining / dl) if dl > 0 else None})
    transfers.sort(key=lambda x: (x["eta"] is None,
                                  x["eta"] if x["eta"] is not None else 0.0,
                                  -x["progress"]))
    return {"ts": now, "transfers": transfers}


def build_nodes() -> dict:
    """Per-node storage and activity: how much each node stores, how many datasets
    it holds (and how many of those complete), and its current throughput.

    The "where is the data" question answered from the infrastructure side, the
    complement to the overview's per-dataset placement.

    Aggregated here rather than shipped as rows: this stays one line per node
    however large the catalog grows, which is what keeps the Nodes screen cheap
    on a swarm holding thousands of datasets.
    """
    now = time.time()
    nodes = [{"label": node_label(snap), **node_totals(node_rows(snap)),
              **node_disk(snap)}
             for snap in fresh_snapshots(now)]
    nodes.sort(key=lambda n: n["label"])
    return {"ts": now, "nodes": nodes}


def build_node_detail(label: str) -> dict:
    """One node's held datasets, or None if no fresh node reports that label.

    The drill-down from the Nodes screen: which torrents this node holds, each
    with its completion and live rate, plus the node's totals. info_hash is
    included so the UI can link every row back to that dataset's detail.
    """
    for snap in fresh_snapshots():
        if node_label(snap) != label:
            continue
        rows = sorted(node_rows(snap), key=lambda r: r["name"])
        return {"ts": time.time(), "label": label,
                **node_totals(rows), **node_disk(snap), "torrents": rows}
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
