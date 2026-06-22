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
import glob
import json
import os
import signal
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import libtorrent as lt

import config
import make_torrent
import swarm_stats

# Persist torrents natively via libtorrent fast-resume. save_info_dict embeds the
# torrent's metadata in the resume file, so a restarted node can re-add a torrent
# from the .resume file alone (no catalog lookup needed); flush_disk_cache makes
# the on-disk data match what we record. We rewrite a torrent's <name>.resume
# whenever its state changes, and delete it on /remove, so restarting a node
# restores exactly the torrents it was holding (with download progress intact).
SAVE_FLAGS = lt.torrent_handle.save_info_dict | lt.torrent_handle.flush_disk_cache
# How often (loops) to checkpoint resume data for torrents that changed.
RESUME_EVERY = 5


class NodeState:
    def __init__(self, node_id: int, ses: "lt.session"):
        self.node_id = node_id
        self.ses = ses
        self.lock = threading.Lock()
        # info_hash(v2 str) -> {name, role, save_path, ti, files, handle}
        self.torrents: dict = {}
        self.session_stats: dict = {}
        self.snapshot: dict = {"node_id": node_id, "torrents": [], "peers": []}
        self.stop = threading.Event()


def resume_dir(node_id: int) -> str:
    return os.path.join(config.NODES_DIR, str(node_id), ".resume")


def resume_path(node_id: int, name: str) -> str:
    return os.path.join(resume_dir(node_id), f"{name}.resume")


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
        # Decoded source bits (e.g. ["pex"], ["incoming"]) — with no tracker,
        # this is how you see PEX actually doing the peer discovery.
        "source_flags": swarm_stats.source_labels(int(p.source)),
        "rtt": p.rtt,
    }


def make_session(node_id: int) -> "lt.session":
    settings = {
        "listen_interfaces": f"{config.HOST}:{config.bt_port(node_id)}",
        # No central tracker: peers are discovered by dialing an introducer peer
        # (see bootstrap()) and then PEX gossiping the rest. PEX rides on the
        # default session plugins (ut_pex), which are loaded unless we disable
        # them, so there's nothing to switch on here. We keep DHT/LSD/UPnP/NAT-PMP
        # off so discovery stays confined to our swarm (and so LSD multicast,
        # which a routed VPN wouldn't carry anyway, isn't relied upon).
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        # All nodes share 127.0.0.1; without this libtorrent allows only ONE
        # peer connection per IP per torrent, so the localhost swarm can't mesh.
        "allow_multiple_connections_per_ip": True,
        "alert_mask": lt.alert.category_t.all_categories,
        # Pace the transfer so the monitor sees a real time-series (see config).
        # By default libtorrent exempts loopback/LAN peers from rate limits, so
        # we must turn that off for the cap to apply to our localhost swarm.
        "upload_rate_limit": config.UPLOAD_RATE_LIMIT,
        "ignore_limits_on_local_network": False,
    }
    return lt.session(settings)


def bootstrap(ns: "NodeState", handle) -> None:
    """Enter the swarm without a tracker: dial the configured introducer peers.
    Once any one connects, PEX propagates the remaining peers. Introducers that
    are down (or this node itself) just fail silently — any reachable one does.
    """
    for addr in config.introducer_addrs(ns.node_id):
        try:
            handle.connect_peer(addr)
        except Exception:
            pass


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
    bootstrap(ns, handle)  # trackerless: dial introducers, then PEX takes over

    entry = {"name": meta["name"], "role": role, "save_path": save_path,
             "ti": ti, "files": file_list(ti), "handle": handle}
    with ns.lock:
        ns.torrents[ih] = entry
    # Persist the assignment immediately so a restart before any download still
    # restores it (the loop writes the actual .resume file from the alert).
    handle.save_resume_data(SAVE_FLAGS)
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
    # Drop its resume file so a restart doesn't bring the torrent back.
    try:
        os.remove(resume_path(ns.node_id, entry["name"]))
    except FileNotFoundError:
        pass
    print(f"node {ns.node_id}: -{entry['name']}", flush=True)
    return {"removed": True, "info_hash": ih, "name": entry["name"]}


def derive_role(node_id: int, save_path: str) -> str:
    """A leecher saves under nodes/<id>/...; anything else is serving in place."""
    leech_root = os.path.abspath(os.path.join(config.NODES_DIR, str(node_id)))
    sp = os.path.abspath(save_path)
    return "leech" if sp == leech_root or sp.startswith(leech_root + os.sep) else "seed"


def _write_resume(node_id: int, alert) -> None:
    os.makedirs(resume_dir(node_id), exist_ok=True)
    with open(resume_path(node_id, alert.torrent_name), "wb") as f:
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
        ih = str(ti.info_hashes().v2)
        role = derive_role(ns.node_id, atp.save_path)
        handle = ns.ses.add_torrent(atp)
        bootstrap(ns, handle)  # re-enter the swarm via introducers on restart
        with ns.lock:
            ns.torrents[ih] = {"name": ti.name(), "role": role,
                               "save_path": atp.save_path, "ti": ti,
                               "files": file_list(ti), "handle": handle}
        count += 1
        print(f"node {ns.node_id}: resumed {role} '{ti.name()}'", flush=True)
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
                _write_resume(ns.node_id, a)
                pending -= 1
            elif isinstance(a, lt.save_resume_data_failed_alert):
                pending -= 1
        time.sleep(0.05)


def session_loop(ns: NodeState) -> None:
    loops = 0
    while not ns.stop.is_set():
        ns.ses.post_session_stats()
        ns.stop.wait(config.NODE_LOOP_INTERVAL)  # sleep, but wake promptly on stop
        loops += 1
        for a in ns.ses.pop_alerts():
            if isinstance(a, lt.session_stats_alert):
                ns.session_stats = collect_session_stats(a.values)
            elif isinstance(a, lt.save_resume_data_alert):
                _write_resume(ns.node_id, a)
            # save_resume_data_failed_alert: nothing to persist yet; ignore.

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

        # Checkpoint fast-resume for any torrent whose state changed.
        if loops % RESUME_EVERY == 0:
            for e in entries:
                if e["handle"].need_save_resume_data():
                    e["handle"].save_resume_data(SAVE_FLAGS)

    flush_resume(ns)


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

    # Treat SIGTERM like Ctrl-C (raise KeyboardInterrupt) so the node shuts down
    # gracefully — checkpointing fast-resume — when stopped by a process manager
    # or `kill`, not just by an interactive Ctrl-C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    ses = make_session(args.id)
    ns = NodeState(args.id, ses)
    resumed = load_resumes(ns)  # restore torrents this node held before a restart
    thread = threading.Thread(target=run_session, args=(ns,), daemon=True)
    thread.start()

    srv = ThreadingHTTPServer((config.HOST, config.stats_port(args.id)), make_handler(ns))
    # Handler threads must be daemonic: with HTTP/1.1 keep-alive they otherwise sit
    # blocked reading the next request on a persistent connection, and as non-daemon
    # threads they'd keep the process alive after Ctrl-C, hanging shutdown.
    srv.daemon_threads = True
    state = (f"(resumed {resumed} torrent(s))" if resumed
             else "(empty; assign torrents with control.py)")
    print(f"node {args.id} up — bt:{config.bt_port(args.id)} "
          f"control/stats:http://{config.HOST}:{config.stats_port(args.id)}/  "
          f"{state}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        ns.stop.set()              # let the session loop checkpoint and exit
        thread.join(timeout=8)


if __name__ == "__main__":
    main()
