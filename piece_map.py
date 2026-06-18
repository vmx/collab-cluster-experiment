"""Show which peer holds which pieces (which data), and — equivalently — which
peers each piece is stored on. Works on partial downloads, so you can see how
many copies of the file currently exist across the swarm.

Each node reports its own piece-ownership bitfield in /stats; this script polls
every node, aggregates those bitfields and renders:
  * a per-node ownership map  (peer -> pieces)
  * a per-piece availability row + histogram  (piece -> peers)
  * a "copies of the file" summary

By default it polls the live nodes. With --snapshot it reads the most recent
monitor snapshot from stats/snapshots/ instead (useful after a run).

Usage:
    python piece_map.py                # live, once
    python piece_map.py --snapshot     # read newest stats/snapshots/*.json

To refresh continuously, wrap it with the `watch` CLI tool:
    watch -n 2 python piece_map.py
"""
import argparse
import glob
import json
import os
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


def fetch_live() -> list:
    nodes = []
    for nid in range(config.NUM_NODES):
        url = f"http://{config.HOST}:{config.stats_port(nid)}/stats"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                nodes.append(json.loads(r.read().decode()))
        except Exception:
            pass
    return nodes


def fetch_snapshot() -> list:
    files = sorted(glob.glob(os.path.join(config.SNAPSHOT_DIR, "*.json")))
    if not files:
        return []
    with open(files[-1]) as f:
        combined = json.load(f)
    print(f"(snapshot: {os.path.basename(files[-1])})")
    return list(combined.get("nodes", {}).values())


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
    rows, meta = swarm_stats.collect(nodes)
    if not rows:
        print("No nodes responded (are the nodes running? try --snapshot).")
        return

    num_pieces = meta["num_pieces"]
    piece_length = meta["piece_length"]
    total_size = meta["total_size"]
    info_hash = meta["info_hash"]
    name = meta["name"]
    files = meta["files"]

    avail = swarm_stats.availability(rows, num_pieces)
    min_avail = min(avail)
    total_have = sum(avail)
    full_copies = [r["id"] for r in rows if all(r["bits"])]
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
        label = f"  n{r['id']} {r['role']:<5} {pct:5.1f}% {have:>4}/{num_pieces:<4}"
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
            holders = ",".join(f"n{i}" for i in f["full_holders"]) or "-"
            partial = " ".join(f"n{i}={pct:.0f}%" for i, pct in f["partial"])
            extra = ("  partial: " + partial) if partial else ""
            print(f"  {disp:<34} {human(f['size']):>9} {f['full_copies']:>5} "
                  f"{f['recon_copies']:>6}  {holders}{extra}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", action="store_true",
                    help="read newest stats/snapshots/*.json instead of polling live")
    args = ap.parse_args()

    fetch = fetch_snapshot if args.snapshot else fetch_live
    source = "snapshot" if args.snapshot else "live"
    report(fetch(), source)


if __name__ == "__main__":
    main()
