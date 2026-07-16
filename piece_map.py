"""Show which peer holds which pieces (which data), and — equivalently — which
peers each piece is stored on. Works on partial downloads, so you can see how
many copies of the file currently exist across the swarm.

Each node reports its own piece-ownership bitfield, which it pushes to the
collector; this script reads the collector's live view, aggregates those
bitfields and renders:
  * a per-node ownership map  (peer -> pieces)
  * a per-piece availability row + histogram  (piece -> peers)
  * a "copies of the file" summary

It reads the collector's /api/live: the latest snapshot of every node still
reporting.

Usage:
    python piece_map.py                  # live, from the collector, once
    python piece_map.py --collector URL  # a non-default collector

To refresh continuously, wrap it with the `watch` CLI tool:
    watch -n 2 python piece_map.py
"""
import argparse
import json
import urllib.request

import config
import swarm_stats

MAX_COLS = 100            # max width of the piece map (pieces are bucketed above this)
HAVE, MISS = "█", "·"   # full block / middle dot
SHADES = "▁▂▃▄▅▆▇█"  # 1/8 .. 8/8 blocks


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def fetch_collector(base: str) -> list:
    """The collector's live view: latest snapshot of every node still reporting."""
    with urllib.request.urlopen(f"{base}/api/live", timeout=2.0) as r:
        return json.loads(r.read().decode()).get("nodes", [])


def piece_size(i: int, piece_length: int, total_size: int, num_pieces: int) -> int:
    if i < num_pieces - 1:
        return piece_length
    return total_size - piece_length * (num_pieces - 1)


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


def report(nodes: list, source: str) -> None:
    torrents = swarm_stats.collect_by_torrent(nodes)
    if not torrents:
        print("No torrents reported (is the collector running and are nodes pushing "
              "with torrents assigned? check the collector's /api/health).")
        return
    for i, (meta, rows) in enumerate(torrents):
        if i:
            print("\n" + "=" * 78)
        render_torrent(meta, rows, source)


def render_torrent(meta: dict, rows: list, source: str) -> None:
    num_pieces = meta["num_pieces"]
    piece_length = meta["piece_length"]
    total_size = meta["total_size"]
    info_hash = meta["info_hash"]
    name = meta["name"]
    files = meta["files"]

    avail = swarm_stats.availability(rows, num_pieces)
    min_avail = min(avail)
    total_have = sum(avail)
    labels = {r["id"]: r["label"] for r in rows}  # node_key -> short display name
    full_copies = [r["label"] for r in rows if all(r["bits"])]
    cols = min(num_pieces, MAX_COLS)

    print(f"Swarm piece map  —  '{name}'  {human(total_size)} in {len(files)} file(s), "
          f"{num_pieces} pieces × {human(piece_length)}")
    print(f"info hash (v2): {info_hash[:16]}...  |  nodes seen: {len(rows)}  |  source: {source}")
    if cols < num_pieces:
        print(f"(map bucketed: {num_pieces} pieces into {cols} columns)")

    print("\nCopies of the complete dataset:")
    print(f"  full copies (one node has everything) : {len(full_copies)}"
          f"{'  (nodes: ' + ','.join(map(str, full_copies)) + ')' if full_copies else ''}")
    print(f"  complete copies incl. partial holders : {min_avail}"
          f"   (rarest piece is held by {min_avail} node(s))")
    print(f"  redundancy (avg copies per piece)     : {total_have / num_pieces:.2f}×")
    print(f"  fully available in swarm              : {'yes' if min_avail >= 1 else 'NO — missing pieces!'}")
    print(f"  total data stored across swarm        : {human(total_have * piece_length)}")

    label_w = 24
    print(f"\nPer-node ownership ({HAVE} = has piece, {MISS} = missing):")
    for r in rows:
        have = sum(r["bits"])
        pct = 100 * have / num_pieces
        stored = sum(piece_size(i, piece_length, total_size, num_pieces)
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collector", default=config.COLLECTOR_BASE, metavar="URL",
                    help="collector base URL to read /api/live from (default: %(default)s)")
    args = ap.parse_args()

    report(fetch_collector(args.collector), "live")


if __name__ == "__main__":
    main()
