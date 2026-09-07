"""Talk to a node.

    python control.py publish 10.0.0.5 ~/photos   # put local data into the swarm
    python control.py list     10.0.0.5           # datasets that node knows of
    python control.py peers    10.0.0.5           # nodes it can see
    python control.py status   10.0.0.5           # datasets it actually holds
    python control.py add      10.0.0.6 photos    # manual mode: take that one
    python control.py remove   10.0.0.6 photos    # drop it
    python control.py map      10.0.0.5           # copies of every dataset
    python control.py map      10.0.0.5 photos     # ...and one dataset's pieces

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
import swarm_stats

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
    # Which of them this node actually holds, and how far along. Two calls, and
    # neither carries a piece bitfield: what it holds, and what is moving.
    held, moving = _held(base)
    print(f"{'name':<24} {'v2 info-hash':<18} on this node")
    for m in metas:
        state = held.get(m["info_hash"])
        if not state:
            column = "-"
        elif state == "complete":
            column = "complete"
        else:
            live = moving.get(m["info_hash"])
            column = f"{(live['progress'] if live else 0.0) * 100:.0f}%"
        print(f"{m['name']:<24} {m['info_hash'][:16]:<18} {column}")


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
        print("\nno peers seen yet. Nodes find each other by multicast beacon, so "
              "they must share a segment.")
        return
    print(f"\n{'node':<10} {'address':<22} {'catalog':<13} last seen")
    for p in peers:
        # Its control address, not its BitTorrent one: this is the column you
        # copy into the next command.
        addr = f"{p.get('ip')}:{p.get('http')}"
        print(f"{(p.get('node') or '?')[:8]:<10} {addr:<22} "
              f"{(p.get('cat') or '-'):<13} {p.get('age', '?')}s ago")


def _held(base: str) -> tuple:
    """What a node holds, and what of it is moving: (state by info-hash,
    live transfer by info-hash). Neither call scales with the catalog — the
    first is one row per dataset held, the second only what is in flight."""
    try:
        held = {r["info_hash"]: r["state"]
                for r in (catalog.fetch_holdings(base).get("holdings") or [])}
        moving = {t["info_hash"]: t for t in catalog.fetch_transfers(base)}
    except Exception:
        return {}, {}
    return held, moving


def _names(base: str) -> dict:
    """info-hash -> name, from a node's catalog. Names are labels only; every
    call below addresses datasets by hash."""
    try:
        return {m["info_hash"]: m["name"] for m in catalog.fetch_list(base)}
    except Exception:
        return {}


def cmd_status(args) -> None:
    base = catalog.base_url(args.endpoint)
    try:
        stats = catalog.fetch_stats(base)
    except Exception:
        _unreachable(args.endpoint)
    held, moving = _held(base)
    if not held:
        print(f"{args.endpoint}: holding nothing yet")
        return
    names = _names(base)
    parts = []
    for info_hash, state in sorted(held.items(),
                                   key=lambda kv: names.get(kv[0], kv[0])):
        name = names.get(info_hash, info_hash[:12])
        if state == "complete":
            parts.append(f"{name}[seed]")
        else:
            live = moving.get(info_hash)
            parts.append(f"{name}[leech "
                         f"{(live['progress'] if live else 0.0) * 100:.0f}% "
                         f"p{live['num_peers'] if live else 0}]")
    print(f"{args.endpoint}: " + "  ".join(parts))
    print(f"  {stats.get('complete', 0)}/{stats.get('held', 0)} complete, "
          f"{human(stats.get('stored', 0))} stored, "
          f"{stats.get('known', 0)} datasets known")


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


# --- the swarm map -----------------------------------------------------------
# Two views, and the split between them is the node API's own.
#
# Without a dataset: how many copies of everything exist, built from what each
# node reports it *holds*. No piece bitfields are fetched at all, so this costs
# the same whether the swarm has two datasets or two million.
#
# With a dataset: that one dataset's pieces, per node, and equivalently how many
# copies of each file exist. The bitfields come from the nodes that hold it, one
# request each — which is why they live on their own endpoint.
#
# The arithmetic is swarm_stats, shared with the web dashboard, so the two views
# can never disagree about how many copies exist.

MAX_COLS = 100            # max width of the piece map (pieces are bucketed above this)
HAVE, MISS = "█", "·"   # full block / middle dot
SHADES = "▁▂▃▄▅▆▇█"  # 1/8 .. 8/8 blocks


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def render_bits(bits: list, num_pieces: int, cols: int) -> str:
    if cols >= num_pieces:
        return "".join(HAVE if b else MISS for b in bits)
    out = []
    for c in range(cols):
        seg = bits[c * num_pieces // cols:(c + 1) * num_pieces // cols]
        frac = sum(seg) / len(seg)
        if frac == 0:
            out.append(MISS)
        elif frac >= 1:
            out.append(HAVE)
        else:
            out.append(SHADES[min(len(SHADES) - 2, int(frac * len(SHADES)))])
    return "".join(out)


def render_avail(avail: list, num_pieces: int, cols: int) -> str:
    def cell(v):
        return str(v) if v < 10 else "+"
    if cols >= num_pieces:
        return "".join(cell(a) for a in avail)
    return "".join(cell(min(avail[c * num_pieces // cols:(c + 1) * num_pieces // cols]))
                   for c in range(cols))


def render_torrent(meta: dict, rows: list) -> None:
    num_pieces = meta["num_pieces"]
    piece_length = meta["piece_length"]
    total_size = meta["total_size"]
    name = meta["name"]
    files = meta["files"]

    avail = swarm_stats.availability(rows, num_pieces)
    min_avail = min(avail)
    total_have = sum(avail)
    labels = {r["id"]: r["label"] for r in rows}  # node_key -> short display name
    full_copies = [r["label"] for r in rows if all(r["bits"])]
    cols = min(num_pieces, MAX_COLS)

    print(f"Swarm piece map  -  '{name}'  {human(total_size)} in {len(files)} file(s), "
          f"{num_pieces} pieces x {human(piece_length)}")
    print(f"info hash (v2): {meta['info_hash'][:16]}...  |  nodes seen: {len(rows)}")
    if cols < num_pieces:
        print(f"(map bucketed: {num_pieces} pieces into {cols} columns)")

    print("\nCopies of the complete dataset:")
    print(f"  full copies (one node has everything) : {len(full_copies)}"
          f"{'  (nodes: ' + ','.join(map(str, full_copies)) + ')' if full_copies else ''}")
    print(f"  complete copies incl. partial holders : {min_avail}"
          f"   (rarest piece is held by {min_avail} node(s))")
    print(f"  redundancy (avg copies per piece)     : {total_have / num_pieces:.2f}x")
    print(f"  fully available in swarm              : {'yes' if min_avail >= 1 else 'NO - missing pieces!'}")
    print(f"  total data stored across swarm        : {human(total_have * piece_length)}")

    label_w = 24
    print(f"\nPer-node ownership ({HAVE} = has piece, {MISS} = missing):")
    for r in rows:
        have = sum(r["bits"])
        pct = 100 * have / num_pieces
        stored = sum(swarm_stats.piece_size(i, piece_length, total_size, num_pieces)
                     for i, b in enumerate(r["bits"]) if b)
        role = "seed" if have == num_pieces else "leech"
        label = f"  {r['label']} {role:<5} {pct:5.1f}% {have:>4}/{num_pieces:<4}"
        print(f"{label:<{label_w}} {render_bits(r['bits'], num_pieces, cols)}  {human(stored)}")
    print(f"{'  availability  (#holders)':<{label_w}} {render_avail(avail, num_pieces, cols)}")

    print("\nAvailability histogram (pieces grouped by #holders):")
    for k in range(len(rows), -1, -1):
        cnt = sum(1 for a in avail if a == k)
        if cnt:
            tag = "  <- MISSING from swarm" if k == 0 else ""
            print(f"  {k} node(s): {cnt:>4} pieces{tag}")

    if files:
        print("\nPer-file copies (full = a node holds the entire file):")
        print(f"  {'file':<34} {'size':>9} {'full':>5} {'recon':>6}  "
              f"holders / partial%")
        for f in sorted(swarm_stats.per_file(rows, files, avail),
                        key=lambda f: f["path"]):
            disp = f["path"]
            if name and disp.startswith(name + "/"):
                disp = disp[len(name) + 1:]
            holders = ",".join(f"{labels.get(i, i)}" for i in f["full_holders"]) or "-"
            partial = " ".join(f"{labels.get(i, i)}={pct:.0f}%" for i, pct in f["partial"])
            extra = ("  partial: " + partial) if partial else ""
            print(f"  {disp:<34} {human(f['size']):>9} {f['full_copies']:>5} "
                  f"{f['recon_copies']:>6}  {holders}{extra}")


def render_overview(rows: list) -> None:
    """Every dataset, rarest first — the question the swarm exists to answer.

    `copies` is the number of nodes holding the whole dataset. Where a partial
    holder could push the real figure higher, this reports the floor; ask for the
    dataset by name to see the exact per-file picture."""
    print(f"{'name':<24} {'v2 info-hash':<18} {'size':>10} {'copies':>7}  "
          f"{'spread':<14} activity")
    for row in sorted(rows, key=lambda r: (r["full_copies"], r["name"])):
        flag = "  <- NO COPY" if row["full_copies"] == 0 else ""
        spread = f"{row['seeding']} seed"
        if row["downloading"]:
            spread += f" / {row['downloading']} moving"
        rate = (f"\u25bc{human(row['download_rate'])}/s"
                if row["download_rate"] else "-")
        print(f"{row['name']:<24} {row['info_hash'][:16]:<18} "
              f"{human(row['total_size']):>10} {row['full_copies']:>7}  "
              f"{spread:<14} {rate}{flag}")


def swarm_nodes(base: str) -> list:
    """Every node, with what it holds. One /stats each plus one full holdings
    listing — control.py keeps no cursor between runs, so it always lists in
    full; a long-running reader follows the cursor instead (see collector.py)."""
    stats, _ = catalog.fetch_swarm(base)
    out = []
    for st in stats:
        node_base = f"http://{st.get('label')}"
        held, moving = _held(node_base)
        out.append({"key": st["node_key"], "label": st.get("label", ""),
                    "base": node_base, "held": held, "moving": moving})
    return out


def resolve_ref(names: dict, ref: str) -> str:
    """A dataset reference as typed -> its full info-hash. Same rules as the
    node's own resolver: an info-hash in full or shortened to any unique leading
    portion, otherwise a name, and a name matching several datasets is an error
    rather than a guess."""
    lowered = ref.lower()
    matches = [h for h in names if h.startswith(lowered)] if HEXREF.match(ref) else []
    if not matches:
        matches = [h for h, n in names.items() if n == ref]
    if not matches:
        print(f"unknown dataset: {ref!r} (see: python control.py list)")
        sys.exit(1)
    if len(matches) > 1:
        print(f"{ref!r} is ambiguous - {len(matches)} datasets match; use one of "
              f"these info-hashes: {', '.join(h[:16] for h in sorted(matches))}")
        sys.exit(1)
    return matches[0]


def cmd_map(args) -> None:
    base = catalog.base_url(args.endpoint)
    try:
        nodes = swarm_nodes(base)
    except Exception:
        _unreachable(args.endpoint)
    if not nodes:
        print("no nodes answered (check: python control.py peers).")
        return

    if not args.dataset:
        entries = catalog.fetch_list(base)
        if not entries:
            print("no datasets yet - publish one with: "
                  "python control.py publish <node> <path>")
            return
        rows = []
        for entry in entries:
            info_hash = entry["info_hash"]
            meta = catalog.fetch_meta(base, info_hash)
            holders, rate = [], 0
            for node in nodes:
                state = node["held"].get(info_hash)
                if not state:
                    continue
                live = node["moving"].get(info_hash)
                holders.append({"label": node["label"], "state": state,
                                "progress": (live or {}).get("progress", 0.0)})
                rate += (live or {}).get("download_rate", 0)
            row = swarm_stats.overview_row(meta, holders)
            row["download_rate"] = rate
            rows.append(row)
        render_overview(rows)
        print("\nfor one dataset's pieces and per-file copies: "
              f"python control.py map {args.endpoint} <dataset>")
        return

    names = {m["info_hash"]: m["name"] for m in catalog.fetch_list(base)}
    info_hash = resolve_ref(names, args.dataset)
    meta = catalog.fetch_meta(base, info_hash)
    holders = []
    for node in nodes:
        if info_hash not in node["held"]:
            continue
        detail = catalog.fetch_holding(node["base"], info_hash)
        if detail:
            holders.append((node["key"], node["label"], detail))
    if not holders:
        print(f"'{meta['name']}' [{info_hash[:16]}] is in the catalog but no node "
              "holds it.\ntake a copy with: python control.py add <node> "
              f"{info_hash[:16]}")
        return
    render_torrent(meta, swarm_stats.holder_rows(meta, holders))


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
    p_map = with_endpoint("map", "copies of every dataset, or one dataset's "
                          "pieces", cmd_map, required=True)
    p_map.add_argument("dataset", nargs="?",
                       help="a dataset name or info-hash; without one, every "
                            "dataset with its copy count")

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
