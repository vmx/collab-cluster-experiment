"""Shared helpers to aggregate what nodes report into swarm-wide views.

The web dashboard (collector.py) and the terminal map (control.py map) both
render from these, so the two can never disagree about how many copies of
something exist.

There are two levels here, and they are separate because the node's API is:

  * From holdings alone — which datasets a node has and whether each is complete
    — comes every list-level number, including the one that matters most: how
    many complete copies of a dataset exist. A complete holder holds every file
    by definition, so counting copies needs no piece-level detail at all. This is
    what overview_row does, and it costs nothing per dataset.

  * From piece bitfields comes everything finer: which pieces are rare, how many
    copies of each *file* exist, what a partial holder actually has. Bitfields
    are fetched one dataset at a time (node.holding_detail), so this level is the
    drill-down and never the list. holder_rows turns those into the row shape
    availability() and per_file() work on.

Stdlib only, deliberately: control.py must work on a machine with no libtorrent.
"""
import math


def piece_size(i: int, piece_length: int, total_size: int, num_pieces: int) -> int:
    """Bytes in piece i. Every piece is piece_length except the last, which is
    whatever is left over."""
    if i < num_pieces - 1:
        return piece_length
    return total_size - piece_length * (num_pieces - 1)


def num_pieces(meta: dict) -> int:
    """How many pieces a dataset has, from its static shape."""
    return max(1, int(meta.get("num_pieces")
                      or math.ceil(meta["total_size"] / meta["piece_length"])))


# --- the list level: copies, from holdings alone ------------------------------

def overview_row(meta: dict, holders: list) -> dict:
    """One dataset's durability and spread, without a single piece bitfield.

    `holders` is one entry per node that holds the dataset, as
    {"label", "state", "progress"} — exactly what a node's holdings stream says,
    plus the progress of an in-flight copy from its transfers.

    Two of the numbers here are deliberately *lower bounds* rather than the exact
    figure the drill-down gives:

      full_copies     nodes holding the whole dataset. Exact.
      durable_copies  the weakest-link per-file count — how many copies really
                      exist, since a dataset is only as replicated as its
                      least-replicated file. A partial holder can only ever raise
                      it, so the complete-holder count is a floor, and reported
                      as such here.
      min_avail       the rarest piece's holder count, likewise floored.

    Both are exact whenever every holder is complete, which is the settled case,
    and both err towards saying a dataset is less safe than it is — the right
    direction for a number you act on. The exact values need every holder's
    bitfield, which is what the per-dataset view fetches.
    """
    complete = [h for h in holders if h.get("state") == "complete"]
    partial = [h for h in holders if h.get("state") != "complete"]
    # Average copies per piece: whole copies, plus how far the partial ones got.
    redundancy = len(complete) + sum(float(h.get("progress") or 0.0) for h in partial)
    spread = [{"label": h["label"],
               "frac": round(1.0 if h.get("state") == "complete"
                             else float(h.get("progress") or 0.0), 3)}
              for h in sorted(holders, key=lambda h: h["label"])]
    return {
        "info_hash": meta["info_hash"], "name": meta.get("name", ""),
        "total_size": meta["total_size"], "piece_length": meta["piece_length"],
        "num_pieces": num_pieces(meta),
        "nodes_seen": len(holders),
        "full_copies": len(complete),
        "durable_copies": len(complete),
        "min_avail": len(complete),
        "redundancy": redundancy,
        "total_stored": int(redundancy * meta["total_size"]),
        "downloading": len(partial), "seeding": len(complete),
        "spread": spread,
    }


# --- the piece level: one dataset at a time -----------------------------------

def holder_rows(meta: dict, holders: list) -> list:
    """Rows for the piece-level views, from each node's /holdings/<info_hash>.

    `holders` is [(node_key, label, detail)] for the nodes that hold the dataset.
    Returns [{"id", "label", "bits", "complete", "progress", "num_peers"}] —
    id = node_key (stable swarm-wide identity, used for holders); label = short
    human name for display. Sorted by label so the two views agree on order.
    """
    total = num_pieces(meta)
    rows = []
    for key, label, detail in sorted(holders, key=lambda h: h[1]):
        bits = [bool(b) for b in (detail.get("pieces") or [])]
        bits = (bits + [False] * total)[:total]
        rows.append({"id": key, "label": label, "bits": bits,
                     "complete": detail.get("state") == "complete",
                     "progress": float(detail.get("progress") or 0.0),
                     "num_peers": int(detail.get("num_peers") or 0)})
    return rows


def availability(rows: list, total_pieces: int) -> list:
    """Per-piece holder count across all nodes."""
    return [sum(r["bits"][i] for r in rows) for i in range(total_pieces)]


def per_file(rows: list, files: list, avail: list) -> list:
    """Per-file replication. For each file returns:
      path, size, num_pieces,
      full_copies / full_holders : nodes holding the entire file (row "id"s, i.e.
                                   node_keys; viewers map them to labels),
      recon_copies               : reconstructable copies (rarest piece in range),
      partial                    : [(id, percent_have)] for incomplete holders.
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
