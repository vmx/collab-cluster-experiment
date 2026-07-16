"""A BitTorrent v2 node daemon: a libtorrent session plus an HTTP control/stats
endpoint. A node starts empty, holding no torrents; you tell it what to do at runtime.

  GET  /stats          -> JSON snapshot (session metrics + per-torrent status +
                          per-peer info); the node also pushes this to the
                          collector, and you can GET it directly for a local peek.
  POST /add            -> body {"name": <catalog torrent>, "mode": "serve"|"download",
                          "path": <local <name> file/dir, serve mode only>}
                          add a torrent from the catalog (serve hosts a copy already
                          on this host at `path`; download fetches it into
                          nodes/<id>/<name>/).
  POST /remove         -> body {"name": <torrent>} or {"info_hash": <v2 hash>}

The session runs in a background thread that continuously refreshes the shared
snapshot across all torrents the node currently holds. Control requests mutate
the session live (libtorrent's session is thread-safe).
"""
import argparse
import glob
import json
import os
import shutil
import signal
import threading
import time
import traceback
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import libtorrent as lt

import catalog
import config
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
    def __init__(self, node_id: int, node_key: str, ses: "lt.session"):
        self.node_id = node_id
        # Stable swarm-wide identity (a persisted UUID). The integer node_id is a
        # local convenience (ports, resume dir); node_key is what the collector
        # keys every metric by, so a restart keeps the same series. The display
        # label is the node's swarm address (see config.node_label).
        self.node_key = node_key
        self.ses = ses
        self.lock = threading.Lock()
        # info_hash(v2 str) -> {name, save_path, ti, files, handle}
        self.torrents: dict = {}
        self.session_stats: dict = {}
        self.snapshot: dict = {"node_key": node_key, "label": config.node_label(node_id),
                               "torrents": [], "peers": []}
        self.stop = threading.Event()


def node_key_path(node_id: int) -> str:
    return os.path.join(config.NODES_DIR, str(node_id), "node_key")


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


def resume_dir(node_id: int) -> str:
    return os.path.join(config.NODES_DIR, str(node_id), ".resume")


def resume_path(node_id: int, name: str) -> str:
    return os.path.join(resume_dir(node_id), f"{name}.resume")


def node_disk(node_id: int) -> dict:
    """Free/total bytes of the filesystem holding this node's data directory —
    the disk that fills up as the node stores more torrent data. Reported in the
    snapshot so the dashboard can show how much room each node has left."""
    try:
        usage = shutil.disk_usage(os.path.join(config.NODES_DIR, str(node_id)))
        return {"free": usage.free, "total": usage.total}
    except OSError:
        return {"free": 0, "total": 0}


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


def peer_dict(p, torrent_name) -> dict:
    client = p.client
    if isinstance(client, bytes):
        client = client.decode("utf-8", "replace")
    return {
        "torrent": torrent_name,
        **swarm_stats.peer_addr(p.ip[0], p.ip[1]),
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
        # Decoded source bits (e.g. ["tracker"], ["incoming"]) — shows how this
        # peer was discovered; tracker-only here, so expect tracker/incoming.
        "source_flags": swarm_stats.source_labels(int(p.source)),
        "rtt": p.rtt,
    }


def make_session(node_id: int) -> "lt.session":
    settings = {
        # Bind all interfaces so peers on other hosts can reach us (not just
        # loopback); we advertise a routable address separately, below.
        "listen_interfaces": f"{config.BIND_HOST}:{config.bt_port(node_id)}",
        # Discovery is tracker-only: each node announces to the tracker baked into
        # the (private) torrent and gets back the peer list. The torrents are
        # private, so libtorrent disables PEX/DHT/LSD anyway; we also keep these
        # off explicitly so nothing pulls in peers from outside our swarm.
        "enable_dht": False,
        "enable_lsd": False,
        "enable_upnp": False,
        "enable_natpmp": False,
        # Distinct hosts have distinct IPs, but many nodes on one dev host share
        # an IP; without this libtorrent allows only ONE peer connection per IP
        # per torrent, so a single-host swarm couldn't mesh.
        "allow_multiple_connections_per_ip": True,
        "alert_mask": lt.alert.category_t.all_categories,
        # Advertise our routable address in announces so the tracker hands other
        # peers an address they can dial, rather than the loopback/NAT-edge IP the
        # tracker would otherwise infer from the announce connection.
        "announce_ip": config.ADVERTISE_IP,
        # Honour our short announce interval instead of libtorrent's 300s floor,
        # so nodes started at different times still get paired promptly.
        "min_announce_interval": config.ANNOUNCE_INTERVAL,
        # Pace the transfer so progress is observable as it happens (see config).
        # By default libtorrent exempts loopback/LAN peers from rate limits, so
        # we must turn that off for the cap to apply within a single-host swarm.
        "upload_rate_limit": config.UPLOAD_RATE_LIMIT,
        "ignore_limits_on_local_network": False,
    }
    return lt.session(settings)


def add_torrent(ns: NodeState, name: str, mode: str, serve_path: str = None) -> dict:
    """Add a catalog torrent to the running session. `mode` says where the data
    comes from: "serve" hosts a copy already on this host — `serve_path` is the
    local <name> file/dir itself (what was given to make_torrent.py) — while
    "download" fetches a fresh copy into nodes/<id>/. Returns a status dict."""
    if mode not in ("serve", "download"):
        raise ValueError(f"mode must be 'serve' or 'download', got {mode!r}")
    if mode == "serve" and not serve_path:
        raise ValueError("serve mode needs a path to the local content directory")
    # The catalog holds only .torrent files; fetch and parse the one we want. Its
    # name and info-hash come from the torrent itself, the single source of truth.
    # (fetch_torrent_bytes raises FileNotFoundError if the catalog has no `name`.)
    ti = lt.torrent_info(lt.bdecode(catalog.fetch_torrent_bytes(name)))
    tname = ti.name()
    ih = str(ti.info_hashes().v2)

    with ns.lock:
        if ih in ns.torrents:
            cur = ns.torrents[ih]
            return {"info_hash": ih, "name": cur["name"],
                    "added": False, "note": "already present"}

    # For serve, `serve_path` is the content itself — the file/dir named <tname>,
    # i.e. what was passed to make_torrent.py. libtorrent wants its *parent* as
    # save_path and rechecks the pieces already on disk; the on-disk name must
    # match the torrent's, so validate it rather than silently seeding nothing.
    if mode == "serve":
        content = os.path.abspath(serve_path)
        if not os.path.exists(content):
            raise ValueError(f"serve path does not exist: {content}")
        if os.path.basename(content) != tname:
            raise ValueError(f"serve path must be the '{tname}' file/dir itself, "
                             f"got {os.path.basename(content)!r}")
        save_path = os.path.dirname(content)
    else:
        save_path = os.path.join(config.NODES_DIR, str(ns.node_id), tname)
        os.makedirs(save_path, exist_ok=True)

    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = save_path
    handle = ns.ses.add_torrent(atp)

    entry = {"name": tname, "save_path": save_path,
             "ti": ti, "files": file_list(ti), "handle": handle}
    with ns.lock:
        ns.torrents[ih] = entry
    # Persist the assignment immediately so a restart before any download still
    # restores it (the loop writes the actual .resume file from the alert).
    handle.save_resume_data(SAVE_FLAGS)
    print(f"node {ns.node_id}: +{mode} '{tname}' "
          f"({len(entry['files'])} files) save_path={save_path}", flush=True)
    return {"info_hash": ih, "name": tname, "mode": mode, "added": True}


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
        handle = ns.ses.add_torrent(atp)
        with ns.lock:
            ns.torrents[ih] = {"name": ti.name(),
                               "save_path": atp.save_path, "ti": ti,
                               "files": file_list(ti), "handle": handle}
        count += 1
        print(f"node {ns.node_id}: resumed '{ti.name()}'", flush=True)
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
            torrents.append(torrent_dict(st, e["ti"], e["files"]))
            for p in e["handle"].get_peer_info():
                peers.append(peer_dict(p, e["name"]))

        snap = {
            "node_key": ns.node_key,    # stable swarm-wide identity
            "label": config.node_label(ns.node_id),  # swarm address, used for display
            "ts": time.time(),
            # The routable address this node announces to the tracker; the
            # collector matches tracker membership against (advertise_ip, bt_port)
            # so both sides key on the same self-reported address.
            "advertise_ip": config.ADVERTISE_IP,
            "bt_port": config.bt_port(ns.node_id),
            "session": ns.session_stats,
            "disk": node_disk(ns.node_id),
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
                    res = add_torrent(ns, body["name"], body.get("mode", "download"),
                                      body.get("path"))
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


def push_loop(ns: NodeState, collector_url: str) -> None:
    """Best-effort: POST the node's latest snapshot to the collector every
    PUSH_INTERVAL. Like a tracker announce, a failed POST is ignored — it just
    shows up as this node briefly going stale in the collector's view. The node
    dials out, so it needs no inbound reachability of its own."""
    while not ns.stop.wait(config.PUSH_INTERVAL):
        with ns.lock:
            snap = ns.snapshot
        if not snap.get("torrents") and not snap.get("session"):
            continue  # nothing meaningful to report yet
        try:
            req = urllib.request.Request(
                collector_url, data=json.dumps(snap).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3).close()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="BitTorrent v2 prototype node daemon")
    # A node-local slot number: picks this node's data dir (nodes/<id>/) and,
    # so several nodes can share one host in a dev run, offsets its bt/control
    # ports. It is not how the node is addressed or identified in the swarm
    # (that's the node_key UUID). One node per host is the common case, so id
    # is optional and defaults to 0.
    ap.add_argument("--id", type=int, default=0)
    ap.add_argument("--collector", default=config.COLLECTOR_URL,
                    help="collector ingest URL to push stats to "
                         "(default: %(default)s; pass '' to run without reporting)")
    args = ap.parse_args()

    # Treat SIGTERM like Ctrl-C (raise KeyboardInterrupt) so the node shuts down
    # gracefully — checkpointing fast-resume — when stopped by a process manager
    # or `kill`, not just by an interactive Ctrl-C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    node_key = load_or_create_node_key(args.id)
    ses = make_session(args.id)
    ns = NodeState(args.id, node_key, ses)
    resumed = load_resumes(ns)  # restore torrents this node held before a restart
    thread = threading.Thread(target=run_session, args=(ns,), daemon=True)
    thread.start()

    if args.collector:
        threading.Thread(target=push_loop, args=(ns, args.collector),
                         daemon=True).start()

    # Bind the control/stats server on all interfaces (not loopback) so control
    # and the collector can reach it at this node's routable address — e.g. from
    # the host into a container, or across separate servers. The port is never
    # exposed to the internet; it lives on the private/bridge network only.
    srv = ThreadingHTTPServer((config.BIND_HOST, config.stats_port(args.id)), make_handler(ns))
    # Handler threads must be daemonic: with HTTP/1.1 keep-alive they otherwise sit
    # blocked reading the next request on a persistent connection, and as non-daemon
    # threads they'd keep the process alive after Ctrl-C, hanging shutdown.
    srv.daemon_threads = True
    state = (f"(resumed {resumed} torrent(s))" if resumed
             else "(empty; assign torrents with control.py)")
    reporting = f"-> collector {args.collector}" if args.collector else "(not reporting)"
    print(f"node {args.id} up [{node_key[:8]}] — bt:{config.bt_port(args.id)} "
          f"control/stats:http://{config.ADVERTISE_IP}:{config.stats_port(args.id)}/  "
          f"{reporting}  {state}", flush=True)
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
