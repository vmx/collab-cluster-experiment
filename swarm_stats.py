"""Shared helpers to aggregate per-node /stats snapshots into swarm-wide views.

Both collector.py (for the DB time-series) and piece_map.py (for the live
display) use these so they always compute "copies" the same way. A node may hold
several torrents at once, so everything is grouped per torrent (by v2 info-hash).
"""
import math

# libtorrent peer_info.source bit flags -> label. These are stable library
# values; we keep them here (plain ints) so viewers without a libtorrent import
# can decode the `source` a node reports. With tracker-only discovery, "tracker"
# and "incoming" are how peers are expected to be learned.
PEER_SOURCE_FLAGS = [
    (0x1, "tracker"),
    (0x2, "dht"),
    (0x4, "pex"),
    (0x8, "lsd"),
    (0x10, "resume"),
    (0x20, "incoming"),
]


def source_labels(source: int) -> list:
    """Decode a peer_info.source bitmask into its source labels."""
    return [label for bit, label in PEER_SOURCE_FLAGS if source & bit]


def peer_addr(ip: str, port) -> dict:
    """The canonical shape for a peer's network address in any stats payload.

    Kept structured (not a joined "ip:port" string) so consumers compare and
    aggregate addresses without re-parsing. Both sources of peer data emit this:
    the tracker (addresses learned from announces) and each node (the endpoints
    libtorrent is actually connected to)."""
    return {"ip": ip, "port": int(port)}


def addr_key(ip: str, port) -> str:
    """A hashable "ip:port" key for matching the same address across sources
    (e.g. reconciling tracker membership against node-reported connections)."""
    return f"{ip}:{int(port)}"


# A peer is in exactly one of these states per torrent, decided solely by whether
# it still has bytes left to fetch. The two are mutually exclusive — a peer that
# holds every piece is a seeder, anything else is a leecher — so stats carry this
# closed set as a role string rather than a boolean that could read as "neither"
# or "both".
PEER_ROLE_SEEDER = "seeder"
PEER_ROLE_LEECHER = "leecher"


def peer_role(complete: bool) -> str:
    """A peer's role for a torrent: seeder if it holds the whole torrent (nothing
    left to download), otherwise leecher."""
    return PEER_ROLE_SEEDER if complete else PEER_ROLE_LEECHER


def collect_by_torrent(nodes: list) -> list:
    """Group every node's torrent entries by torrent.

    Returns a list of (meta, rows), one per distinct torrent, sorted by name:
      rows: [{"id", "label", "bits": list[bool] of length num_pieces,
              "state", "progress", "dl", "ul", "num_peers"}]
            id = node_key (stable swarm-wide identity, used for the DB / holders);
            label = short human name for display;
            state/progress/dl/ul/num_peers = this node's live transfer activity
            for the torrent (aggregate throughput / "replicating" signal, and the
            live connection count the detail view shows). Ownership views ignore
            these; only `bits` matters there.
      meta: {info_hash, name, num_pieces, piece_length, total_size, files}
    """
    groups: dict = {}  # info_hash -> {"meta", "rows"}
    for n in sorted(nodes, key=lambda n: n.get("label", "")):
        key = n.get("node_key")
        label = n.get("label", key)
        for t in n.get("torrents", []):
            ih = t.get("info_hash_v2") or t.get("name")
            piece_length = t["piece_length"]
            total_size = t["total_size"]
            num_pieces = max(1, math.ceil(total_size / piece_length))
            bits = [bool(b) for b in (t.get("pieces") or [])]
            bits = (bits + [False] * num_pieces)[:num_pieces]
            g = groups.setdefault(ih, {
                "meta": {"info_hash": ih, "name": t.get("name", ""),
                         "num_pieces": num_pieces, "piece_length": piece_length,
                         "total_size": total_size, "files": t.get("files") or []},
                "rows": []})
            g["rows"].append({"id": key, "label": label,
                              "bits": bits,
                              "state": t.get("state", ""),
                              "progress": float(t.get("progress") or 0.0),
                              "dl": int(t.get("download_rate") or 0),
                              "ul": int(t.get("upload_rate") or 0),
                              "num_peers": int(t.get("num_peers") or 0)})
    return [(g["meta"], g["rows"])
            for g in sorted(groups.values(), key=lambda g: g["meta"]["name"])]


def availability(rows: list, num_pieces: int) -> list:
    """Per-piece holder count across all nodes."""
    return [sum(r["bits"][i] for r in rows) for i in range(num_pieces)]


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
