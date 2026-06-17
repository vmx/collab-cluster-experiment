"""Shared helpers to aggregate per-node /stats snapshots into swarm-wide views.

Both monitor.py (for the DB time-series) and piece_map.py (for the live display)
use these so they always compute "copies" the same way.
"""
import math


def collect(nodes: list):
    """Normalise a list of node /stats snapshots.

    Returns (rows, meta) or (None, None) if no node reported a torrent.
      rows: [{"id", "role", "bits": list[bool] of length num_pieces}]
      meta: {num_pieces, piece_length, total_size, name, files, info_hash}
    """
    nodes = [n for n in nodes if n.get("torrents")]
    if not nodes:
        return None, None
    t0 = nodes[0]["torrents"][0]
    piece_length = t0["piece_length"]
    total_size = t0["total_size"]
    num_pieces = max(1, math.ceil(total_size / piece_length))

    rows = []
    for n in sorted(nodes, key=lambda n: n["node_id"]):
        bits = [bool(b) for b in (n["torrents"][0].get("pieces") or [])]
        bits = (bits + [False] * num_pieces)[:num_pieces]
        rows.append({"id": n["node_id"], "role": n.get("role", "?"), "bits": bits})

    meta = {"num_pieces": num_pieces, "piece_length": piece_length,
            "total_size": total_size, "name": t0.get("name", ""),
            "files": t0.get("files") or [], "info_hash": t0.get("info_hash_v2", "")}
    return rows, meta


def availability(rows: list, num_pieces: int) -> list:
    """Per-piece holder count across all nodes."""
    return [sum(r["bits"][i] for r in rows) for i in range(num_pieces)]


def per_file(rows: list, files: list, avail: list) -> list:
    """Per-file replication. For each file returns:
      path, size, num_pieces,
      full_copies / full_holders : nodes holding the entire file,
      recon_copies               : reconstructable copies (rarest piece in range),
      partial                    : [(node_id, percent_have)] for incomplete holders.
    """
    out = []
    for f in files:
        fp, lp = f["first_piece"], f["last_piece"]
        npc = max(0, lp - fp + 1)               # 0 for empty files
        full_holders, partial = [], []
        for r in rows:
            have = npc if npc == 0 else sum(r["bits"][p] for p in range(fp, lp + 1))
            if npc == 0 or have == npc:
                full_holders.append(r["id"])
            elif have:
                partial.append((r["id"], 100 * have / npc))
        recon = len(rows) if npc == 0 else min(avail[p] for p in range(fp, lp + 1))
        out.append({"path": f["path"], "size": f["size"], "num_pieces": npc,
                    "full_copies": len(full_holders), "full_holders": full_holders,
                    "recon_copies": recon, "partial": partial})
    return out
