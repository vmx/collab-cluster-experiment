"""Minimal BitTorrent HTTP tracker (stdlib + libtorrent's bencode).

The info_hash is treated as an opaque key, so this works unchanged for v2-only
torrents (where peers announce the truncated 20-byte v2 hash).

Endpoints:
  GET /announce  - standard BitTorrent announce (compact + dict peer models)
  GET /scrape    - standard scrape
  GET /stats     - JSON (non-BitTorrent) snapshot for the monitor
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote_to_bytes

import libtorrent as lt

import config

LOCK = threading.Lock()
# info_hash(bytes) -> { peer_id(bytes): {ip, port, left, uploaded, downloaded, last_seen} }
SWARM: dict = {}
ANNOUNCE_COUNT: dict = {}  # info_hash(bytes) -> int
START_TIME = time.time()


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

    def _send(self, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(200)
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
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def handle_announce(self, params: dict) -> None:
        info_hash = first(params, "info_hash")
        peer_id = first(params, "peer_id")
        if not info_hash or not peer_id:
            return self._failure("missing info_hash or peer_id")

        ip = self.client_address[0]
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
            for ih, peers in SWARM.items():
                complete = sum(1 for p in peers.values() if p["left"] == 0)
                out["torrents"].append({
                    "info_hash": ih.hex(),
                    "seeders": complete,
                    "leechers": len(peers) - complete,
                    "peers": len(peers),
                    "announces": ANNOUNCE_COUNT.get(ih, 0),
                })
        self._send(json.dumps(out).encode(), "application/json")


def main() -> None:
    srv = ThreadingHTTPServer((config.HOST, config.TRACKER_PORT), Handler)
    print(f"tracker on http://{config.HOST}:{config.TRACKER_PORT}/announce", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
