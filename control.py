"""Talk to a node.

    python control.py publish 10.0.0.5 ~/photos   # put local data into the swarm
    python control.py list     10.0.0.5           # datasets that node knows of
    python control.py peers    10.0.0.5           # nodes it can see
    python control.py status   10.0.0.5           # datasets it actually holds
    python control.py add      10.0.0.6 photos    # manual mode: take that one
    python control.py remove   10.0.0.6 photos    # drop it

Nodes are addressed by their HTTP endpoint, "host[:port]" (port defaults to the
standard control port, so on its own IP a node is just its address). Several
nodes on one host in a dev run are told apart by port, e.g. 127.0.0.1:8002.

There is nothing central to talk to: every node answers for itself, and any node
will do for `list` — they converge on the same catalog. Datasets are named by the
basename of whatever was published; where a name is ambiguous (two nodes
published different content under the same one), use the info-hash instead.
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request

import catalog
import config

# A dataset reference that looks like hex is treated as an info-hash, in full or
# shortened to any unique leading portion — so the 16-character forms `list` and
# the ambiguity errors print can be pasted straight back in. Below 8 characters
# it's too collision-prone to be worth guessing at, and reads as a name.
HEXREF = re.compile(r"^[0-9a-f]{8,64}$", re.I)

# Publishing hashes the content before returning, which takes as long as it takes
# on a big tree — so this deliberately isn't the short timeout the other calls use.
PUBLISH_TIMEOUT = 3600.0


def _post(endpoint: str, path: str, payload: dict, timeout: float = 5.0):
    base = catalog.base_url(endpoint)
    req = urllib.request.Request(f"{base}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"error": f"HTTP {e.code}: {raw[:200]}"}


def _dataset_ref(ref: str) -> dict:
    """Address a dataset the way the user typed it. The node resolves either
    form, and falls back to matching a name if a hex-looking ref matches no
    hash — so a dataset that happens to be named like one still works."""
    return {"info_hash": ref.lower()} if HEXREF.match(ref) else {"name": ref}


def _unreachable(endpoint: str) -> None:
    print(f"{endpoint}: unreachable - is `python node.py` running there?")
    sys.exit(1)


def _checked(endpoint: str, res: dict) -> dict:
    """Hand back a successful result, or print the node's error and stop. The
    node writes its errors to be read — an ambiguous name comes back with the
    info-hashes to disambiguate it — so they go out as they are rather than
    wrapped in a JSON blob that escapes the punctuation."""
    if res.get("error"):
        print(f"{endpoint}: {res['error']}")
        sys.exit(1)
    return res


def cmd_list(args) -> None:
    base = catalog.base_url(args.endpoint)
    try:
        metas = catalog.fetch_list(base)
    except Exception:
        _unreachable(args.endpoint)
    if not metas:
        print("no datasets yet - publish one with: "
              "python control.py publish <node> <path>")
        return
    # Which of them this node actually holds, and how far along.
    try:
        held = {t.get("info_hash_v2"): t
                for t in (catalog.fetch_stats(base).get("torrents") or [])}
    except Exception:
        held = {}
    print(f"{'name':<24} {'v2 info-hash':<18} on this node")
    for m in metas:
        t = held.get(m["info_hash"])
        if not t:
            state = "-"
        elif t.get("is_seeding") or (t.get("progress") or 0) >= 1.0:
            state = "complete"
        else:
            state = f"{(t.get('progress') or 0) * 100:.0f}%"
        print(f"{m['name']:<24} {m['info_hash'][:16]:<18} {state}")


def cmd_peers(args) -> None:
    try:
        view = catalog.fetch_peers(catalog.base_url(args.endpoint))
    except Exception:
        _unreachable(args.endpoint)
    me = view.get("self") or {}
    print(f"this node  {me.get('node','?')[:8]}  {args.endpoint}  "
          f"({me.get('held',0)} held / {me.get('known',0)} known)")
    peers = view.get("peers") or []
    if not peers:
        print("\nno peers seen yet. If they're on another segment, start the node "
              "with --peer <a-known-node>.")
        return
    print(f"\n{'node':<10} {'address':<22} {'catalog':<13} last seen")
    for p in peers:
        # Its control address, not its BitTorrent one: this is the column you
        # copy into the next command.
        addr = f"{p.get('ip')}:{p.get('http')}"
        print(f"{(p.get('node') or '?')[:8]:<10} {addr:<22} "
              f"{(p.get('cat') or '-'):<13} {p.get('age', '?')}s ago")


def cmd_status(args) -> None:
    try:
        snap = catalog.fetch_stats(catalog.base_url(args.endpoint))
    except Exception:
        _unreachable(args.endpoint)
    torrents = snap.get("torrents") or []
    if not torrents:
        print(f"{args.endpoint}: holding nothing yet")
        return
    parts = []
    for t in torrents:
        complete = t.get("is_seeding") or (t.get("progress") or 0) >= 1.0
        role = "seed" if complete else "leech"
        parts.append(f"{t.get('name', '?')}[{role} "
                     f"{(t.get('progress') or 0) * 100:.0f}% p{t.get('num_peers') or 0}]")
    print(f"{args.endpoint}: " + "  ".join(parts))


def cmd_publish(args) -> None:
    res = _checked(args.endpoint, _post(args.endpoint, "/publish",
                                        {"path": args.path},
                                        timeout=PUBLISH_TIMEOUT))
    if not res.get("published"):
        print(f"{args.endpoint}: already published {res['name']!r} "
              f"[{res['info_hash'][:16]}] - nothing to do")
        return
    print(f"{args.endpoint}: published {res['name']!r} [{res['info_hash'][:16]}]")
    print(f"every node learns about it within a tick (~{config.BEACON_INTERVAL:.0f}s). "
          "Nodes running --replicate all\nfetch it on their own; tell any other "
          "node to keep a copy with:")
    print(f"  python control.py add <node> {res['info_hash'][:16]}")


def cmd_add(args) -> None:
    res = _checked(args.endpoint, _post(args.endpoint, "/add",
                                        _dataset_ref(args.dataset)))
    ref = f"{res['name']!r} [{res['info_hash'][:16]}]"
    if not res.get("added"):
        print(f"{args.endpoint}: already holding {ref} - nothing to do")
        return
    print(f"{args.endpoint}: taking {ref}, pulling from every peer that has it")
    print("watch it arrive with:")
    print(f"  python control.py status {args.endpoint}")


def cmd_remove(args) -> None:
    res = _checked(args.endpoint, _post(args.endpoint, "/remove",
                                        _dataset_ref(args.dataset)))
    if not res.get("removed"):
        print(f"{args.endpoint}: not holding {args.dataset!r} - nothing to do")
        return
    print(f"{args.endpoint}: dropped {res['name']!r} [{res['info_hash'][:16]}] "
          "- it stays in the catalog, and the files stay on disk")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    default_ep = f"{config.HOST}:{config.STATS_PORT_BASE}"
    ep_help = ("node endpoint host[:port] (port defaults to the standard control "
               f"port {config.STATS_PORT_BASE}; default: {default_ep})")

    def with_endpoint(name, help_text, func, required=False):
        p = sub.add_parser(name, help=help_text)
        if required:
            p.add_argument("endpoint", help=ep_help)
        else:
            p.add_argument("endpoint", nargs="?", default=default_ep, help=ep_help)
        p.set_defaults(func=func)
        return p

    with_endpoint("list", "datasets a node knows of", cmd_list)
    with_endpoint("peers", "nodes a node can see", cmd_peers)
    with_endpoint("status", "datasets a node actually holds", cmd_status)

    p_pub = with_endpoint("publish", "put a local file/dir into the swarm",
                          cmd_publish, required=True)
    p_pub.add_argument("path", help="the file/dir to publish, local to that node")

    for name, help_text, func in [
            ("add", "tell a node to take a dataset (manual mode)", cmd_add),
            ("remove", "tell a node to drop a dataset", cmd_remove)]:
        p = with_endpoint(name, help_text, func, required=True)
        p.add_argument("dataset", help="dataset name, or its v2 info-hash")

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
