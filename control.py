"""Talk to a node.

    python control.py publish 10.0.0.5 ~/photos   # put local data into the swarm
    python control.py list     10.0.0.5           # every dataset, + what this node has
    python control.py peers    10.0.0.5           # nodes it can see
    python control.py status   10.0.0.5           # datasets it actually holds
    python control.py add      10.0.0.6 photos    # tell that node to take it
    python control.py remove   10.0.0.6 photos    # drop it
    python control.py map      10.0.0.5           # copies of every dataset
    python control.py map      10.0.0.5 photos     # ...and one dataset's pieces

Nodes are addressed by their HTTP endpoint, "host[:port]" (port defaults to the
standard control port, so on its own IP a node is just its address). Several
nodes on one host in a dev run are told apart by port, e.g. 127.0.0.1:8002.

There is nothing central to talk to, and no node has a catalog: a dataset exists
because some node holds it, so `list` and `map` read every node through the one
you name and union what they hold. Datasets are named by the basename of whatever
was published; where a name is ambiguous (two nodes published different content
under the same one), use the info-hash instead.
"""
import argparse
import heapq
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
# A whole v2 info-hash: the one reference that needs nobody's help to resolve.
_FULL_HASH = re.compile(r"^[0-9a-f]{64}$", re.I)

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
    """Address a dataset the way the user typed it, for /remove — which acts on
    something the node already holds, so the node can resolve either form
    itself, falling back to a name if a hex-looking ref matches no hash. /add is
    different: nothing is held yet and a node has no catalog, so control.py
    resolves the name across the swarm and sends a hash."""
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
    """Every dataset in the swarm, and what the named node has of each.

    Read through that node rather than from it: it knows only what it holds, so
    the list is the union of every node's holdings, and the last column is the
    one thing that is genuinely about the node you asked.

    Printed as the merge turns each dataset up, in info-hash order. Sorting by
    name would mean holding the whole swarm's catalog before the first line —
    which is the one thing a full listing at this scale cannot do."""
    base = catalog.base_url(args.endpoint)
    try:
        nodes = swarm_labels(base)
    except Exception:
        _unreachable(args.endpoint)
    here = next((label for label, node in nodes if node == base), None)
    moving = in_flight([(label, node) for label, node in nodes if node == base])
    header, seen = f"{'name':<24} {'v2 info-hash':<18} {'copies':>6}  on this node", 0
    for info_hash, holders in swarm_stream(nodes):
        if not seen:
            print(header)
        seen += 1
        mine = next((row for label, row in holders if label == here), None)
        if not mine:
            column = "-"
        elif mine.get("state") == "complete":
            column = "complete"
        else:
            live = (moving.get(here) or {}).get(info_hash)
            column = f"{(live['progress'] if live else 0.0) * 100:.0f}%"
        copies = sum(1 for _, row in holders if row.get("state") == "complete")
        name = holders[0][1].get("name") or ""
        print(f"{name:<24} {info_hash[:16]:<18} {copies:>6}  {column}")
    if not seen:
        print("no datasets yet - publish one with: "
              "python control.py publish <node> <path>")


def cmd_peers(args) -> None:
    try:
        view = catalog.fetch_peers(catalog.base_url(args.endpoint))
    except Exception:
        _unreachable(args.endpoint)
    me = view.get("self") or {}
    print(f"this node  {me.get('node','?')[:8]}  {args.endpoint}  "
          f"({me.get('held', 0)} held)")
    peers = view.get("peers") or []
    if not peers:
        print("\nno peers seen yet. Nodes find each other by multicast beacon, so "
              "they must share a segment.")
        return
    print(f"\n{'node':<10} {'address':<22} last seen")
    for p in peers:
        # Its control address, not its BitTorrent one: this is the column you
        # copy into the next command.
        addr = f"{p.get('ip')}:{p.get('http')}"
        print(f"{(p.get('node') or '?')[:8]:<10} {addr:<22} "
              f"{p.get('age', '?')}s ago")


def swarm_stream(nodes: list):
    """Every dataset in the swarm, in info-hash order, as (info_hash, holders).

    The union of the nodes' holdings streams — which is the catalog — assembled
    without being held. Each node hands its listing out in info-hash order, so
    merging them brings every holder of a dataset past at the same moment: its
    copy count falls out of the merge, and what is in memory is one page per
    node however much the swarm holds.

    `nodes` is [(label, base)]. Holders are [(label, row)], the rows as the node
    reported them."""
    def tagged(label, node):
        # A function, not a generator expression: an expression would close over
        # the loop variable and tag every row with the last node's label.
        for row in catalog.holdings_stream(node):
            yield row["info_hash"], label, row

    streams = [tagged(label, node) for label, node in nodes]
    current, holders = None, []
    for info_hash, label, row in heapq.merge(*streams, key=lambda t: t[0]):
        if info_hash != current:
            if holders:
                yield current, holders
            current, holders = info_hash, []
        holders.append((label, row))
    if holders:
        yield current, holders


def swarm_labels(base: str) -> list:
    """[(label, base)] for every node reachable through the one named."""
    stats, _ = catalog.fetch_swarm(base)
    return [(st["label"], f"http://{st['label']}") for st in stats if st.get("label")]


def in_flight(nodes: list) -> dict:
    """label -> {info_hash: transfer row}. Bounded by what is moving, so it is
    read up front and used to colour whatever the streams turn up."""
    out = {}
    for label, node in nodes:
        try:
            out[label] = {t["info_hash"]: t for t in catalog.fetch_transfers(node)}
        except Exception:
            out[label] = {}
    return out


def holder_view(holders: list, moving: dict) -> tuple:
    """(meta, holders) in the shape swarm_stats.overview_row wants."""
    name, row0 = holders[0][1].get("name", ""), holders[0][1]
    meta = {"info_hash": row0["info_hash"], "name": name,
            "total_size": int(row0.get("total_size") or 0),
            "piece_length": int(row0.get("piece_length") or 0)}
    out = []
    for label, row in holders:
        live = (moving.get(label) or {}).get(row["info_hash"])
        out.append({"label": label, "state": row.get("state"),
                    "progress": 1.0 if row.get("state") == "complete"
                                else float(live["progress"]) if live else 0.0,
                    "download_rate": int(live["download_rate"]) if live else 0})
    return meta, out


def swarm_bases(base: str) -> list:
    """Every node reachable through the one named, as addresses and nothing
    else. What a caller wants when it has a question for each node rather than
    a use for everything they hold."""
    stats, _ = catalog.fetch_swarm(base)
    return [f"http://{st.get('label')}" for st in stats if st.get("label")]


# How many held datasets `status` names before it stops. Enough that a node in
# the walkthrough lists everything it has, few enough that a node holding a
# million costs one page of its stream rather than all of it.
STATUS_SHOWN = 12

# How many of the rarest `map` shows. The walk is over the whole swarm either
# way; this is what it keeps while walking, and what an operator can act on.
MAP_TOP = 50


def cmd_status(args) -> None:
    base = catalog.base_url(args.endpoint)
    try:
        stats = catalog.fetch_stats(base)
    except Exception:
        _unreachable(args.endpoint)
    held = int(stats.get("held") or 0)
    if not held:
        print(f"{args.endpoint}: holding nothing yet")
        return
    moving = {t["info_hash"]: t for t in catalog.fetch_transfers(base)}
    parts, shown = [], 0
    for row in catalog.holdings_stream(base):
        if not args.all and shown == STATUS_SHOWN:
            parts.append(f"... and {held - shown} more (--all)")
            break
        shown += 1
        name = row.get("name") or row["info_hash"][:12]
        if row.get("state") == "complete":
            parts.append(f"{name}[seed]")
        else:
            live = moving.get(row["info_hash"])
            parts.append(f"{name}[leech "
                         f"{(live['progress'] if live else 0.0) * 100:.0f}% "
                         f"p{live['num_peers'] if live else 0}]")
    print(f"{args.endpoint}: " + "  ".join(parts))
    print(f"  {stats.get('complete', 0)}/{held} complete, "
          f"{human(stats.get('stored', 0))} stored")


def cmd_publish(args) -> None:
    res = _checked(args.endpoint, _post(args.endpoint, "/publish",
                                        {"path": args.path},
                                        timeout=PUBLISH_TIMEOUT))
    if not res.get("published"):
        print(f"{args.endpoint}: already published {res['name']!r} "
              f"[{res['info_hash'][:16]}] - nothing to do")
        return
    print(f"{args.endpoint}: published {res['name']!r} [{res['info_hash'][:16]}]")
    print("it exists for as long as this node holds it - tell another node to "
          "keep a copy with:")
    print(f"  python control.py add <node> {res['info_hash'][:16]}")


def cmd_add(args) -> None:
    """Tell a node to take a dataset.

    The name is resolved here, not there: a node has no catalog to look one up
    in, so it takes an info-hash and fetches the .torrent from whoever holds the
    dataset. Resolving it asks each node about that one reference; an info-hash
    given in full is not a question at all."""
    base = catalog.base_url(args.endpoint)
    try:
        info_hash = resolve_across(base, args.dataset)
    except Exception:
        _unreachable(args.endpoint)
    res = _checked(args.endpoint, _post(args.endpoint, "/add",
                                        {"info_hash": info_hash}))
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
          "- the files stay on disk")
    print("if that was the last copy, the dataset has left the swarm: nothing "
          "keeps a\ncatalog of datasets nobody holds. Re-publishing the same "
          "path brings it back\nunchanged - the dataset is its content.")


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


def resolve_across(base: str, ref: str) -> str:
    """A dataset reference as typed -> its full info-hash, asking the swarm only
    as much as it has to.

    A full info-hash is already the answer and costs nothing. Anything else — a
    name, or a shortened hash — is a question each node can answer about its own
    holdings, so it is one small filtered request per node rather than every
    node's entire stream read into memory to look one thing up."""
    if _FULL_HASH.match(ref):
        return ref.lower()
    names = {}
    for node in swarm_bases(base):
        try:
            for row in catalog.fetch_matching(node, ref):
                names[row["info_hash"]] = row.get("name") or ""
        except Exception:
            continue          # a node that doesn't answer simply has no match
    return resolve_ref(names, ref)


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
        nodes = swarm_labels(base)
    except Exception:
        _unreachable(args.endpoint)
    if not nodes:
        print("no nodes answered (check: python control.py peers).")
        return

    if not args.dataset:
        moving = in_flight(nodes)
        # Rarest first, and only the rarest kept: the merge walks the whole
        # swarm but nothing but the answer is held. --all keeps everything,
        # which is the same walk without the bound.
        rarest, total = [], 0
        for info_hash, holders in swarm_stream(nodes):
            total += 1
            row = swarm_stats.overview_row(*holder_view(holders, moving))
            key = (-row["durable_copies"], row["name"])
            if args.all or len(rarest) < args.top:
                heapq.heappush(rarest, (key, total, row))
            elif key < rarest[0][0]:
                heapq.heapreplace(rarest, (key, total, row))
        if not total:
            print("no datasets yet - publish one with: "
                  "python control.py publish <node> <path>")
            return
        render_overview([row for _, _, row in sorted(rarest, reverse=True)])
        if not args.all and total > len(rarest):
            print(f"\n{len(rarest)} rarest of {total} datasets (--all for every one)")
        print("\nfor one dataset's pieces and per-file copies: "
              f"python control.py map {args.endpoint} <dataset>")
        return

    info_hash = resolve_across(base, args.dataset)
    holders, holder_bases = [], []
    for label, node in nodes:
        detail = catalog.fetch_holding(node, info_hash)
        if detail:
            holders.append((label, label, detail))
            holder_bases.append(node)
    # A dataset is here at all only because someone holds it, so this list is
    # never empty — but the node we asked may have gone away mid-command.
    if not holders:
        print(f"no node is serving {args.dataset!r} any more.")
        return
    # The file -> piece map is the one thing not in a holdings row, so it comes
    # from a holder: the only kind of node that has the .torrent to answer from.
    meta = catalog.fetch_meta(holder_bases[0], info_hash)
    render_torrent(meta, swarm_stats.holder_rows(meta, holders))


def main() -> None:

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ep_help = ("node endpoint host[:port] (port defaults to the standard control "
               f"port {config.STATS_PORT_BASE})")

    def with_endpoint(name, help_text, func):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("endpoint", help=ep_help)
        p.set_defaults(func=func)
        return p

    with_endpoint("list", "every dataset in the swarm, and what this node has",
                  cmd_list)
    with_endpoint("peers", "nodes a node can see", cmd_peers)
    p_status = with_endpoint("status", "datasets a node actually holds",
                             cmd_status)
    p_status.add_argument("--all", action="store_true",
                          help=f"name every dataset it holds, not the first "
                               f"{STATUS_SHOWN}")
    p_map = with_endpoint("map", "copies of every dataset, or one dataset's "
                          "pieces", cmd_map)
    p_map.add_argument("dataset", nargs="?",
                       help="a dataset name or info-hash; without one, the "
                            "least-replicated datasets with their copy counts")
    p_map.add_argument("--top", type=int, default=MAP_TOP,
                       help=f"how many of the rarest to show (default {MAP_TOP})")
    p_map.add_argument("--all", action="store_true",
                       help="every dataset, rarest first, however many there are")

    p_pub = with_endpoint("publish", "put a local file/dir into the swarm",
                          cmd_publish)
    p_pub.add_argument("path", help="the file/dir to publish, local to that node")

    for name, help_text, func in [
            ("add", "tell a node to take a dataset (manual mode)", cmd_add),
            ("remove", "tell a node to drop a dataset", cmd_remove)]:
        p = with_endpoint(name, help_text, func)
        p.add_argument("dataset", help="dataset name, or its v2 info-hash")

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
