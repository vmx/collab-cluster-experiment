"""Orchestrate the whole prototype: build data, then start the tracker, the 5
nodes (1 seed + 4 leechers) and the monitor as child processes.

The monitor inherits this process's stdout so its live per-poll summary is shown
here; the tracker and nodes log to files under stats/. Ctrl-C tears everything
down cleanly. Pass --until-complete to stop automatically once all leechers
finish.
"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import json

import config
import make_torrent

PROCS = []  # list of (name, Popen, logfile-or-None)


def wait_port(host, port, timeout=15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def spawn(name, argv, inherit_stdout=False):
    log = None
    if inherit_stdout:
        stdout = None
    else:
        log = open(os.path.join(config.STATS_DIR, f"{name}.log"), "w")
        stdout = log
    p = subprocess.Popen([sys.executable, *argv], stdout=stdout,
                         stderr=subprocess.STDOUT if not inherit_stdout else None)
    PROCS.append((name, p, log))
    return p


def shutdown(*_):
    print("\nshutting down...", flush=True)
    for _, p, _log in PROCS:
        if p.poll() is None:
            p.terminate()
    for _, p, log in PROCS:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        if log:
            log.close()
    sys.exit(0)


def all_leechers_done() -> bool:
    for i in range(config.NUM_NODES):
        if config.is_seed(i):
            continue
        try:
            with urllib.request.urlopen(
                    f"http://{config.HOST}:{config.stats_port(i)}/stats", timeout=1) as r:
                snap = json.loads(r.read().decode())
        except Exception:
            return False
        torrents = snap.get("torrents") or []
        if not torrents or (torrents[0].get("progress") or 0) < 1.0:
            return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--until-complete", action="store_true",
                    help="stop once every leecher reaches 100%%")
    ap.add_argument("--content", metavar="PATH",
                    help="file or directory to share (default: built-in sample)")
    args = ap.parse_args()

    os.makedirs(config.STATS_DIR, exist_ok=True)
    make_torrent.main(args.content)
    here = config.BASE_DIR

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    spawn("tracker", [os.path.join(here, "bittorrent_tracker.py")])
    if not wait_port(config.HOST, config.TRACKER_PORT):
        print("tracker failed to start; see stats/tracker.log")
        shutdown()

    for i in range(config.NUM_NODES):
        role = "seed" if config.is_seed(i) else "leech"
        spawn(f"node{i}", [os.path.join(here, "node.py"), "--id", str(i), "--role", role])
    for i in range(config.NUM_NODES):
        wait_port(config.HOST, config.stats_port(i))

    spawn("monitor", [os.path.join(here, "monitor.py")], inherit_stdout=True)
    print("network up. Ctrl-C to stop. logs in stats/  (db: stats/monitor.db)", flush=True)

    while True:
        time.sleep(1)
        for name, p, _ in PROCS:
            if p.poll() is not None:
                print(f"{name} exited (code {p.returncode}); shutting down")
                shutdown()
        if args.until_complete and all_leechers_done():
            print("all leechers complete; letting the monitor capture the "
                  "final sample, then shutting down")
            time.sleep(config.POLL_INTERVAL + 0.5)
            shutdown()


if __name__ == "__main__":
    main()
