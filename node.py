"""A single BitTorrent v2 node: a libtorrent session plus a /stats endpoint.

The libtorrent session runs in a background thread that continuously refreshes a
shared snapshot (session metrics + torrent status + per-peer info). A small HTTP
server serves that snapshot as JSON at /stats for the monitor to poll.
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


class NodeState:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot: dict = {}


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


def torrent_dict(st, ti, files_meta) -> dict:
    info_hash_v2 = ""
    try:
        info_hash_v2 = str(st.info_hashes.v2)
    except Exception:
        pass
    return {
        "info_hash_v2": info_hash_v2,
        "name": ti.name(),
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


def peer_dict(p) -> dict:
    client = p.client
    if isinstance(client, bytes):
        client = client.decode("utf-8", "replace")
    return {
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


def session_loop(args, ns: NodeState) -> None:
    settings = {
        "listen_interfaces": f"{config.HOST}:{config.bt_port(args.id)}",
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
    ses = lt.session(settings)

    with open(config.META_PATH) as f:
        meta = json.load(f)

    atp = lt.add_torrent_params()
    ti = lt.torrent_info(meta["torrent"])
    atp.ti = ti
    if args.role == "seed":
        # Serve the content in place from wherever make_torrent.py found it.
        atp.save_path = meta["seed_save_path"]
    else:
        atp.save_path = os.path.join(config.NODES_DIR, str(args.id))
    os.makedirs(atp.save_path, exist_ok=True)
    files_meta = file_list(ti)
    handle = ses.add_torrent(atp)
    print(f"node {args.id}: added '{meta['name']}' "
          f"({len(files_meta)} files), save_path={atp.save_path}", flush=True)

    last_session_stats: dict = {}
    while True:
        ses.post_session_stats()
        time.sleep(config.NODE_LOOP_INTERVAL)
        for a in ses.pop_alerts():
            if isinstance(a, lt.session_stats_alert):
                last_session_stats = collect_session_stats(a.values)
        st = handle.status()
        peers = handle.get_peer_info()
        snap = {
            "node_id": args.id,
            "role": args.role,
            "ts": time.time(),
            "bt_port": config.bt_port(args.id),
            "session": last_session_stats,
            "torrents": [torrent_dict(st, ti, files_meta)],
            "peers": [peer_dict(p) for p in peers],
        }
        with ns.lock:
            ns.snapshot = snap


def make_handler(ns: NodeState):
    class StatsHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/stats":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with ns.lock:
                body = json.dumps(ns.snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return StatsHandler


def run_session(args, ns):
    try:
        session_loop(args, ns)
    except Exception:
        traceback.print_exc()


def main() -> None:
    ap = argparse.ArgumentParser(description="BitTorrent v2 prototype node")
    ap.add_argument("--id", type=int, required=True)
    ap.add_argument("--role", choices=["seed", "leech"], required=True)
    args = ap.parse_args()

    ns = NodeState()
    threading.Thread(target=run_session, args=(args, ns), daemon=True).start()

    srv = ThreadingHTTPServer((config.HOST, config.stats_port(args.id)), make_handler(ns))
    print(f"node {args.id} ({args.role}) bt:{config.bt_port(args.id)} "
          f"stats:http://{config.HOST}:{config.stats_port(args.id)}/stats", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
