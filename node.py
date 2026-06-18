"""A BitTorrent v2 node daemon: a libtorrent session plus an HTTP control/stats
endpoint. A node starts empty and roleless; you tell it what to do at runtime.

  GET  /stats          -> JSON snapshot (session metrics + per-torrent status +
                          per-peer info) for the monitor to poll.
  POST /add            -> body {"name": <catalog torrent>, "role": "seed"|"leech"}
                          add a torrent from the catalog (seed serves it in place,
                          leech downloads it into nodes/<id>/<name>/).
  POST /remove         -> body {"name": <torrent>} or {"info_hash": <v2 hash>}

The session runs in a background thread that continuously refreshes the shared
snapshot across all torrents the node currently holds. Control requests mutate
the session live (libtorrent's session is thread-safe).
"""
import argparse
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import libtorrent as lt

import config
import make_torrent


class NodeState:
    def __init__(self, node_id: int, ses: "lt.session"):
        self.node_id = node_id
        self.ses = ses
        self.lock = threading.Lock()
        # info_hash(v2 str) -> {name, role, save_path, ti, files, handle}
        self.torrents: dict = {}
        self.session_stats: dict = {}
        self.snapshot: dict = {"node_id": node_id, "torrents": [], "peers": []}


def state_name(state) -> str:
    return str(state).split(".")[-1] if state is not None else ""


def collect_session_stats(values: dict) -> dict:
    """libtorrent 2.0's session_stats_alert.values is already keyed by metric
    name (e.g. 'net.sent_bytes'), so just copy it into a plain dict."""
    return {name: value for name, value in values.items()}


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


def torrent_dict(st, ti, files_meta, role) -> dict:
    info_hash_v2 = ""
    try:
        info_hash_v2 = str(st.info_hashes.v2)
    except Exception:
        pass
    return {
        "info_hash_v2": info_hash_v2,
        "name": ti.name(),
        "role": role,
        "state": state_name(st.state),
        # Per-piece ownership bitfield: which pieces (=which data) THIS node holds.
        # This is the authoritative source for the swarm-wide piece map.
        "pieces": [bool(b) for b in st.pieces],
        "piece_length": ti.piece_length(),
        "total_size": ti.total_size(),
        # Static file -> piece-range map so consumers can do per-file analysis.
        "files": files_meta,
        "progress": st.progress,
        "download_rate": st.download_rate,
        "upload_rate": st.upload_rate,
        "download_payload_rate": st.download_payload_rate,
        "upload_payload_rate": st.upload_payload_rate,
        "total_done": st.total_done,
        "total_wanted": st.total_wanted,
        "total_download": st.total_download,
        "total_upload": st.total_upload,
        "all_time_download": st.all_time_download,
        "all_time_upload": st.all_time_upload,
        "num_peers": st.num_peers,
        "num_seeds": st.num_seeds,
        "num_connections": st.num_connections,
        "num_pieces": st.num_pieces,
        "distributed_copies": st.distributed_copies,
        "last_seen_complete": st.last_seen_complete,
        "is_seeding": bool(st.is_seeding),
        "is_finished": bool(st.is_finished),
    }


def peer_dict(p, torrent_name) -> dict:
    client = p.client
    if isinstance(client, bytes):
        client = client.decode("utf-8", "replace")
    return {
        "torrent": torrent_name,
        "ip": f"{p.ip[0]}:{p.ip[1]}",
        "client": client,
        "down_speed": p.down_speed,
        "up_speed": p.up_speed,
        "payload_down_speed": p.payload_down_speed,
        "payload_up_speed": p.payload_up_speed,
        "progress": p.progress,
        "total_download": p.total_download,
        "total_upload": p.total_upload,
        "flags": int(p.flags),
        "source": int(p.source),
        "rtt": p.rtt,
    }


def make_session(node_id: int) -> "lt.session":
    settings = {
        "listen_interfaces": f"{config.HOST}:{config.bt_port(node_id)}",
        # Discovery is strictly tracker-driven so the stats reflect our swarm.
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        # All nodes share 127.0.0.1; without this libtorrent allows only ONE
        # peer connection per IP per torrent, so the localhost swarm can't mesh.
        "allow_multiple_connections_per_ip": True,
        "alert_mask": lt.alert.category_t.all_categories,
        "announce_ip": config.HOST,
        # Pace the transfer so the monitor sees a real time-series (see config).
        # By default libtorrent exempts loopback/LAN peers from rate limits, so
        # we must turn that off for the cap to apply to our localhost swarm.
        "upload_rate_limit": config.UPLOAD_RATE_LIMIT,
        "ignore_limits_on_local_network": False,
        # libtorrent otherwise refuses to re-announce more than once every 300s
        # (default min_announce_interval), so the tracker would reap a node long
        # before it checks back in. Honour our short announce interval instead so
        # nodes started at different times can still find each other.
        "min_announce_interval": config.ANNOUNCE_INTERVAL,
    }
    return lt.session(settings)


def add_torrent(ns: NodeState, name: str, role: str) -> dict:
    """Add a catalog torrent to the running session. Returns a small status dict."""
    if role not in ("seed", "leech"):
        raise ValueError(f"role must be 'seed' or 'leech', got {role!r}")
    meta = make_torrent.load_meta(name)  # raises FileNotFoundError if unknown
    ti = lt.torrent_info(meta["torrent"])
    ih = str(ti.info_hashes().v2)

    with ns.lock:
        if ih in ns.torrents:
            cur = ns.torrents[ih]
            return {"info_hash": ih, "name": cur["name"], "role": cur["role"],
                    "added": False, "note": "already present"}

    if role == "seed":
        save_path = meta["seed_save_path"]  # serve in place, no copy
    else:
        save_path = os.path.join(config.NODES_DIR, str(ns.node_id), meta["name"])
    os.makedirs(save_path, exist_ok=True)

    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = save_path
    handle = ns.ses.add_torrent(atp)

    entry = {"name": meta["name"], "role": role, "save_path": save_path,
             "ti": ti, "files": file_list(ti), "handle": handle}
    with ns.lock:
        ns.torrents[ih] = entry
    print(f"node {ns.node_id}: +{role} '{meta['name']}' "
          f"({len(entry['files'])} files) save_path={save_path}", flush=True)
    return {"info_hash": ih, "name": meta["name"], "role": role, "added": True}


def remove_torrent(ns: NodeState, name: str = None, info_hash: str = None) -> dict:
    with ns.lock:
        ih = info_hash
        if ih is None:
            ih = next((h for h, e in ns.torrents.items() if e["name"] == name), None)
        entry = ns.torrents.pop(ih, None) if ih else None
    if not entry:
        return {"removed": False, "note": "not found"}
    ns.ses.remove_torrent(entry["handle"])
    print(f"node {ns.node_id}: -{entry['name']}", flush=True)
    return {"removed": True, "info_hash": ih, "name": entry["name"]}


def session_loop(ns: NodeState) -> None:
    while True:
        ns.ses.post_session_stats()
        time.sleep(config.NODE_LOOP_INTERVAL)
        for a in ns.ses.pop_alerts():
            if isinstance(a, lt.session_stats_alert):
                ns.session_stats = collect_session_stats(a.values)

        with ns.lock:
            entries = list(ns.torrents.values())

        torrents, peers = [], []
        for e in entries:
            st = e["handle"].status()
            torrents.append(torrent_dict(st, e["ti"], e["files"], e["role"]))
            for p in e["handle"].get_peer_info():
                peers.append(peer_dict(p, e["name"]))

        snap = {
            "node_id": ns.node_id,
            "ts": time.time(),
            "bt_port": config.bt_port(ns.node_id),
            "session": ns.session_stats,
            "torrents": torrents,
            "peers": peers,
        }
        with ns.lock:
            ns.snapshot = snap


def make_handler(ns: NodeState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw or b"{}")

        def do_GET(self):
            if self.path != "/stats":
                self._send_json({"error": "not found"}, 404)
                return
            with ns.lock:
                snap = ns.snapshot
            self._send_json(snap)

        def do_POST(self):
            try:
                body = self._read_json()
                if self.path == "/add":
                    res = add_torrent(ns, body["name"], body.get("role", "leech"))
                    self._send_json(res)
                elif self.path == "/remove":
                    res = remove_torrent(ns, body.get("name"), body.get("info_hash"))
                    self._send_json(res)
                else:
                    self._send_json({"error": "not found"}, 404)
            except FileNotFoundError:
                self._send_json({"error": f"unknown torrent: {body.get('name')!r} "
                                          f"(see: python control.py list)"}, 404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 400)

    return Handler


def run_session(ns):
    try:
        session_loop(ns)
    except Exception:
        traceback.print_exc()


def main() -> None:
    ap = argparse.ArgumentParser(description="BitTorrent v2 prototype node daemon")
    ap.add_argument("--id", type=int, required=True)
    args = ap.parse_args()

    ses = make_session(args.id)
    ns = NodeState(args.id, ses)
    threading.Thread(target=run_session, args=(ns,), daemon=True).start()

    srv = ThreadingHTTPServer((config.HOST, config.stats_port(args.id)), make_handler(ns))
    print(f"node {args.id} up — bt:{config.bt_port(args.id)} "
          f"control/stats:http://{config.HOST}:{config.stats_port(args.id)}/  "
          f"(empty; assign torrents with control.py)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
