"""Minimal BitTorrent HTTP tracker + torrent catalog (stdlib + libtorrent's
bencode).

The info_hash is treated as an opaque key, so this works unchanged for v2-only
torrents (where peers announce the truncated 20-byte v2 hash). No whitelist — any
info-hash is served.

Because the tracker is the one central service every node contacts, it also hosts
the torrent catalog: a node needs only this tracker's URL to get both peers and
the .torrent metadata, with no shared catalog directory.

Endpoints:
  GET /announce          - standard BitTorrent announce (compact + dict peers)
  GET /scrape            - standard scrape
  GET /stats             - JSON (non-BitTorrent) snapshot for manual inspection
  GET /catalog           - JSON list of catalog torrents ({name, info_hash},
                           derived from each .torrent)
  GET /catalog/<name>.torrent - one torrent's .torrent bytes
  GET /catalog/subscribe - Server-Sent Events stream of newly added torrents.
                           A background thread watches TORRENTS_DIR, so anything
                           that drops a torrent into the catalog (make_torrent.py)
                           is pushed to subscribers without polling. By default a
                           subscriber only sees torrents added after it connects;
                           pass ?since=<seq> to resume (each event carries a seq),
                           or ?since=0 to replay the whole catalog then stream.
"""
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote_to_bytes

import libtorrent as lt

import config
import make_torrent
import swarm_stats

LOCK = threading.Lock()
# info_hash(bytes) -> { peer_id(bytes): {ip, port, left, uploaded, downloaded, last_seen} }
SWARM: dict = {}
ANNOUNCE_COUNT: dict = {}  # info_hash(bytes) -> int
START_TIME = time.time()

# Newly-added-torrent notifications. A watcher thread appends each freshly seen
# catalog meta to CATALOG_EVENTS (append-only; index+1 is its seq) and wakes any
# open /catalog/subscribe streams via the condition. In-memory only, like the
# swarm state — a restart rebuilds it from disk on the first scan.
CATALOG_COND = threading.Condition()
CATALOG_EVENTS: list = []
# How long a subscribe stream blocks between events before emitting a heartbeat
# comment (which also lets it notice a client that has gone away).
SUBSCRIBE_HEARTBEAT = 15.0
# How often the watcher rescans the catalog directory for new torrents.
CATALOG_SCAN_INTERVAL = 1.0


def watch_catalog(interval: float = CATALOG_SCAN_INTERVAL) -> None:
    """Poll TORRENTS_DIR for newly registered torrents and publish them.

    The catalog is just files on disk (make_torrent.py writes them, offline), so
    the tracker learns of additions by scanning rather than by being told. The
    first scan seeds the known-set *without* notifying, so torrents that already
    existed at startup are available for ?since=0 replay but are not announced as
    "new" to default subscribers.
    """
    known: set = set()
    seeded = False
    while True:
        try:
            metas = make_torrent.list_catalog()
        except Exception:
            metas = []
        new = [m for m in metas if m["name"] not in known]
        for m in new:
            known.add(m["name"])
        if new:
            with CATALOG_COND:
                CATALOG_EVENTS.extend(new)
                if seeded:
                    CATALOG_COND.notify_all()
        seeded = True
        time.sleep(interval)


def parse_query(qs: str) -> dict:
    """Parse a raw query string preserving binary values (info_hash, peer_id).

    urllib's parse_qs would mangle the percent-encoded binary, so decode each
    key/value with unquote_to_bytes by hand.
    """
    params: dict = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        key, _, val = pair.partition("=")
        params.setdefault(unquote_to_bytes(key).decode("latin-1"), []).append(
            unquote_to_bytes(val))
    return params


def first(params: dict, key: str, default=None):
    vals = params.get(key)
    return vals[0] if vals else default


def _to_int(b: bytes, default: int = 0) -> int:
    try:
        return int(b)
    except (TypeError, ValueError):
        return default


def _valid_ipv4(s: str) -> bool:
    """Whether s is a dotted-quad we can store and later compact-encode."""
    try:
        socket.inet_aton(s)
        return "." in s  # inet_aton also accepts "123"; require dotted form
    except OSError:
        return False


def announce_ip(handler, params: dict) -> str:
    """The address to record for an announcing peer.

    Prefer the peer's self-declared &ip= (validated) over the announce
    connection's source: on a real network a node knows its routable address
    better than the tracker, whose view of the source is the NAT edge, and in a
    dev run it's loopback. Falls back to the socket source when no valid ip= is
    given. This is a private, trusted swarm, so honouring the declared ip is safe;
    a public tracker would gate this to loopback/trusted clients."""
    declared = first(params, "ip")
    if declared:
        d = declared.decode("latin-1")
        if _valid_ipv4(d):
            return d
    return handler.client_address[0]


def reap(info_hash: bytes) -> None:
    """Drop peers we haven't heard from in a few announce intervals.

    The window must comfortably exceed how often peers actually re-announce, or a
    node that started earlier gets evicted before a later one can discover it.
    """
    cutoff = time.time() - 3 * config.ANNOUNCE_INTERVAL
    peers = SWARM.get(info_hash, {})
    for pid in [pid for pid, p in peers.items() if p["last_seen"] < cutoff]:
        del peers[pid]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence default logging
        pass

    def _send(self, body: bytes, content_type: str = "text/plain", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _failure(self, msg: str) -> None:
        self._send(lt.bencode({b"failure reason": msg.encode()}))

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/announce":
            self.handle_announce(parse_query(parts.query))
        elif parts.path == "/scrape":
            self.handle_scrape(parse_query(parts.query))
        elif parts.path == "/stats":
            self.handle_stats()
        elif parts.path == "/catalog":
            self.handle_catalog_list()
        elif parts.path == "/catalog/subscribe":
            self.handle_catalog_subscribe(parse_query(parts.query))
        elif parts.path.startswith("/catalog/"):
            self.handle_catalog_file(parts.path[len("/catalog/"):])
        else:
            self._send(b"", code=404)

    def handle_announce(self, params: dict) -> None:
        info_hash = first(params, "info_hash")
        peer_id = first(params, "peer_id")
        if not info_hash or not peer_id:
            return self._failure("missing info_hash or peer_id")

        ip = announce_ip(self, params)
        port = _to_int(first(params, "port"))
        event = (first(params, "event", b"") or b"").decode("latin-1")
        left = _to_int(first(params, "left"))
        uploaded = _to_int(first(params, "uploaded"))
        downloaded = _to_int(first(params, "downloaded"))
        compact = first(params, "compact", b"1") == b"1"
        numwant = _to_int(first(params, "numwant"), 50)

        with LOCK:
            ANNOUNCE_COUNT[info_hash] = ANNOUNCE_COUNT.get(info_hash, 0) + 1
            peers = SWARM.setdefault(info_hash, {})
            if event == "stopped":
                peers.pop(peer_id, None)
            else:
                peers[peer_id] = {
                    "ip": ip, "port": port, "left": left,
                    "uploaded": uploaded, "downloaded": downloaded,
                    "last_seen": time.time(),
                }
            reap(info_hash)
            others = [(pid, p) for pid, p in peers.items() if pid != peer_id][:numwant]
            complete = sum(1 for p in peers.values() if p["left"] == 0)
            incomplete = len(peers) - complete

        if compact:
            peers_field = b"".join(
                socket.inet_aton(p["ip"]) + int(p["port"]).to_bytes(2, "big")
                for _, p in others)
        else:
            peers_field = [
                {b"peer id": pid, b"ip": p["ip"].encode(), b"port": p["port"]}
                for pid, p in others]

        self._send(lt.bencode({
            b"interval": config.ANNOUNCE_INTERVAL,
            b"min interval": max(1, config.ANNOUNCE_INTERVAL // 2),
            b"complete": complete,
            b"incomplete": incomplete,
            b"peers": peers_field,
        }))

    def handle_scrape(self, params: dict) -> None:
        files = {}
        with LOCK:
            hashes = params.get("info_hash") or list(SWARM.keys())
            for ih in hashes:
                peers = SWARM.get(ih, {})
                complete = sum(1 for p in peers.values() if p["left"] == 0)
                files[ih] = {
                    b"complete": complete,
                    b"incomplete": len(peers) - complete,
                    b"downloaded": 0,
                }
        self._send(lt.bencode({b"files": files}))

    def handle_stats(self) -> None:
        with LOCK:
            out = {"uptime": time.time() - START_TIME, "torrents": []}
            now = time.time()
            for ih, peers in SWARM.items():
                out["torrents"].append({
                    "info_hash": ih.hex(),
                    "announces": ANNOUNCE_COUNT.get(ih, 0),
                    "peers": [
                        {
                            **swarm_stats.peer_addr(p["ip"], p["port"]),
                            "role": swarm_stats.peer_role(p["left"] == 0),
                            "age": round(now - p["last_seen"]),  # whole seconds
                        }
                        for p in peers.values()
                    ],
                })
        self._send(json.dumps(out).encode(), "application/json")

    # --- catalog (the torrents live here) ------------------------------------

    def handle_catalog_list(self) -> None:
        # Read fresh from disk each time so torrents added by make_torrent.py show
        # up without restarting the tracker.
        self._send(json.dumps(make_torrent.list_catalog()).encode(),
                   "application/json")

    def handle_catalog_subscribe(self, params: dict) -> None:
        """Stream newly added torrents as Server-Sent Events until the client
        disconnects. Runs in its own handler thread (ThreadingHTTPServer), so
        blocking here doesn't hold up other requests."""
        since = first(params, "since")
        with CATALOG_COND:
            if since is None:
                cursor = len(CATALOG_EVENTS)  # only torrents added from now on
            else:
                cursor = _to_int(since, 0)
                cursor = max(0, min(cursor, len(CATALOG_EVENTS)))

        # No Content-Length: this is an open-ended stream terminated by close.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # Let the browser EventSource reach this from the collector-hosted dashboard.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            while True:
                with CATALOG_COND:
                    if cursor >= len(CATALOG_EVENTS):
                        CATALOG_COND.wait(timeout=SUBSCRIBE_HEARTBEAT)
                    start = cursor
                    pending = CATALOG_EVENTS[cursor:]
                    cursor = len(CATALOG_EVENTS)
                if pending:
                    chunks = []
                    for offset, meta in enumerate(pending):
                        payload = dict(meta, seq=start + offset + 1)
                        chunks.append(f"data: {json.dumps(payload)}\n\n")
                    self.wfile.write("".join(chunks).encode())
                else:
                    self.wfile.write(b": heartbeat\n\n")  # keep-alive / liveness probe
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client went away; end the stream

    def handle_catalog_file(self, name: str) -> None:
        # name is "<torrent>.torrent"; reject path tricks and anything else.
        if "/" in name or "\\" in name or name.startswith(".") \
                or not name.endswith(".torrent"):
            return self._send(b"", code=404)
        path = os.path.join(config.TORRENTS_DIR, name)
        if not os.path.isfile(path):
            return self._send(b"", code=404)
        with open(path, "rb") as f:
            body = f.read()
        self._send(body, "application/x-bittorrent")


def main() -> None:
    srv = ThreadingHTTPServer((config.TRACKER_BIND, config.TRACKER_PORT), Handler)
    srv.daemon_threads = True  # don't let keep-alive handler threads block Ctrl-C
    # Watch the catalog dir so /catalog/subscribe can push new torrents.
    threading.Thread(target=watch_catalog, daemon=True).start()
    print(f"tracker on {config.TRACKER_BASE}/  "
          f"(announce + catalog + subscribe)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
