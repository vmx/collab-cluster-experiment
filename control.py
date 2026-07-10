"""Drive the running swarm by hand: list the torrent catalog, inspect what each
node holds, and tell nodes to serve or download specific torrents.

    python control.py list                         # catalog + live peers per torrent
    python control.py status                       # what every node holds now
    python control.py add 0 media --mode serve --path /data/media  # serve in place
    python control.py add 1 media --mode download                  # node 1 downloads
    python control.py remove 1 media               # node 1 drops 'media'
    python control.py subscribe                    # watch for newly added torrents

Torrents are referred to by name (the basename of the shared file/dir), as shown
by `list`. Build the catalog first with `python make_torrent.py [paths...]`.
"""
import argparse
import json
import sys
import urllib.request

import catalog
import config
import swarm_stats


def _node_url(node_id: int, path: str) -> str:
    return f"http://{config.HOST}:{config.stats_port(node_id)}{path}"


def _get(node_id: int):
    with urllib.request.urlopen(_node_url(node_id, "/stats"), timeout=2.0) as r:
        return json.loads(r.read().decode())


def _post(node_id: int, path: str, payload: dict):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(_node_url(node_id, path), data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"error": f"HTTP {e.code}: {raw[:200]}"}


def cmd_list(_args) -> None:
    try:
        metas = catalog.fetch_list()
    except Exception:
        print("can't reach the tracker catalog — is bittorrent_tracker.py running?")
        sys.exit(1)
    if not metas:
        print("catalog empty — build one with: python make_torrent.py [paths...]")
        return
    # The tracker holds no content — only the catalog and which peers announce it.
    # So show live seeders/leechers (where the data actually lives) rather than the
    # local path the torrent happened to be built from. Tracker keys torrents by the
    # 40-hex truncated v2 info-hash, so match on meta['info_hash'][:40].
    try:
        live = {t["info_hash"]: t.get("peers", [])
                for t in catalog.fetch_stats().get("torrents", [])}
    except Exception:
        live = {}
    print(f"{'name':<20} {'v2 info-hash':<20} peers")
    for m in metas:
        peers = live.get(m["info_hash"][:40], [])
        seeders = sum(1 for p in peers if p.get("role") == swarm_stats.PEER_ROLE_SEEDER)
        print(f"{m['name']:<20} {m['info_hash'][:18]:<20} "
              f"{seeders} seed / {len(peers) - seeders} leech")


def cmd_status(args) -> None:
    # Control is operator-local: you ask the node you run (on this host) what it
    # holds. Swarm-wide status across all nodes comes from the collector
    # (piece_map / report), not by scanning every node.
    nid = args.node
    try:
        snap = _get(nid)
    except Exception:
        print(f"node {nid}: (down)")
        return
    torrents = snap.get("torrents") or []
    if not torrents:
        print(f"node {nid}: idle (no torrents)")
        return
    parts = []
    for t in torrents:
        complete = t.get("is_seeding") or (t.get("progress") or 0) >= 1.0
        role = "seed" if complete else "leech"
        parts.append(f"{t.get('name', '?')}[{role} "
                     f"{(t.get('progress') or 0) * 100:.0f}% p{t.get('num_peers') or 0}]")
    print(f"node {nid}: " + "  ".join(parts))


def cmd_add(args) -> None:
    if args.mode == "serve" and not args.path:
        sys.exit("--mode serve needs --path <the local file/dir to serve>")
    res = _post(args.node, "/add",
                {"name": args.name, "mode": args.mode, "path": args.path})
    print(f"node {args.node}: {json.dumps(res)}")
    if res.get("error"):
        sys.exit(1)


def cmd_remove(args) -> None:
    res = _post(args.node, "/remove", {"name": args.name})
    print(f"node {args.node}: {json.dumps(res)}")


def cmd_subscribe(args) -> None:
    # A tail -f-style watch: block on the tracker's stream and print each newly
    # added torrent as it appears, in the same columns as `list`.
    print(f"{'name':<20} v2 info-hash")
    print("(watching for new torrents — Ctrl-C to stop)")

    def on_torrent(meta: dict) -> None:
        print(f"{meta.get('name', '?'):<20} {meta.get('info_hash', '')[:18]}",
              flush=True)

    try:
        catalog.subscribe(on_torrent, since=args.since)
    except KeyboardInterrupt:
        pass
    except Exception:
        print("subscription ended — is bittorrent_tracker.py running?")
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list catalog torrents").set_defaults(func=cmd_list)

    p_status = sub.add_parser("status", help="show what one (local) node holds")
    p_status.add_argument("node", type=int, help="node id to query on this host")
    p_status.set_defaults(func=cmd_status)

    p_add = sub.add_parser("add", help="tell a node to serve/download a torrent")
    p_add.add_argument("node", type=int)
    p_add.add_argument("name", help="catalog torrent name (see `list`)")
    p_add.add_argument("--mode", choices=["serve", "download"], default="download",
                       help="serve in place from --path, or download a copy")
    p_add.add_argument("--path", help="for --mode serve: the local file/dir to serve "
                                      "(what you passed to make_torrent.py)")
    p_add.set_defaults(func=cmd_add)

    p_rm = sub.add_parser("remove", help="tell a node to drop a torrent")
    p_rm.add_argument("node", type=int)
    p_rm.add_argument("name")
    p_rm.set_defaults(func=cmd_remove)

    p_sub = sub.add_parser("subscribe",
                           help="watch for newly added catalog torrents (blocks)")
    p_sub.add_argument("--since", type=int, default=None,
                       help="resume from this event seq (0 replays the whole "
                            "catalog first); default: only torrents added from now")
    p_sub.set_defaults(func=cmd_subscribe)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
