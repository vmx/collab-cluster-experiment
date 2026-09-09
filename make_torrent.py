"""Build BitTorrent v2-only .torrent files from arbitrary local files or
directories.

This is a **library** first: `build()` is what a node calls when you publish a
local path to it (`control.py publish`). Torrents are:

  - **v2-only** => SHA-256 merkle hashing, no v1/hybrid.
  - **trackerless** => no announce URL at all; there is no tracker in this system.
  - **private** => libtorrent disables DHT, PEX and LSD for them, so the *only*
    peers a torrent ever sees are the ones the node injects from its beacon-
    discovered peer table (see node.py). Discovery is entirely ours.

Run as a script it does one thing — generate the built-in sample content, so
there's something to publish:

    python make_torrent.py            # writes data/sample/{media,documents}
"""
import argparse
import os
import sys

import libtorrent as lt

import config

# Built-in sample: two separate content roots => two separate datasets, so the
# multi-dataset machinery is exercised out of the box. (relative path, size).
SAMPLE_GROUPS = {
    "media": [
        ("photo_a.bin", 5 * 1024 * 1024),
        ("photo_b.bin", 3 * 1024 * 1024),
        ("clips/intro.bin", 4 * 1024 * 1024),
    ],
    "documents": [
        ("notes.txt", 12 * 1024),
        ("report.bin", 6 * 1024 * 1024),
        ("appendix/data.bin", 8 * 1024 * 1024),
    ],
}


# --- building ----------------------------------------------------------------

def build(source: str) -> tuple:
    """Hash a local file/dir into a .torrent. Returns (name, info_hash, blob).

    Returns the bencoded bytes rather than writing them: the caller decides where
    they live (a node drops them in its own catalog, where peers then find it).
    Raises ValueError on bad input — this runs inside a node's HTTP handler, so
    it must not exit the process.
    """
    source = os.path.abspath(source)
    if not os.path.exists(source):
        raise ValueError(f"content path does not exist: {source}")

    fs = lt.file_storage()
    # add_files recurses into directories and preserves the nested layout.
    lt.add_files(fs, source)
    if fs.total_size() == 0:
        raise ValueError(f"no data found under {source}")

    ct = lt.create_torrent(fs, config.PIECE_SIZE, flags=lt.create_torrent.v2_only)
    # No add_tracker(): there is no tracker. Peers arrive only via node.py's
    # beacon-driven connect_peer(), so private costs us nothing and keeps
    # libtorrent from reaching for DHT/PEX/LSD. (The private flag lives in the
    # info-dict, so it is part of the info-hash.)
    ct.set_priv(True)
    ct.set_creator("collab-cluster-experiment")
    ct.set_comment(f"v2-only dataset: {os.path.basename(source)}")
    # Hash the files as they sit on disk; save_path is the content root's parent.
    lt.set_piece_hashes(ct, os.path.dirname(source))

    blob = lt.bencode(ct.generate())
    ti = lt.torrent_info(lt.bdecode(blob))
    return ti.name(), str(ti.info_hashes().v2), blob


def serve_save_path(ti, source: str) -> str:
    """Where libtorrent must look to find `source`'s data already on disk.

    Usually a torrent's root *is* the content directory, so the save path is its
    parent. But libtorrent collapses a directory holding exactly one file into a
    single-file torrent named after that file, dropping the directory level — for
    those, the save path is the directory we were handed. So derive it from the
    torrent we actually built rather than assuming a shape.
    """
    source = os.path.abspath(source)
    if os.path.isdir(source) and ti.name() != os.path.basename(source):
        return source
    return os.path.dirname(source)


# --- sample content ----------------------------------------------------------

def build_sample(root: str) -> list:
    """Generate the nested sample content roots; return their paths."""
    roots = []
    for group, files in SAMPLE_GROUPS.items():
        group_root = os.path.join(root, group)
        roots.append(group_root)
        if all(os.path.exists(os.path.join(group_root, p)) for p, _ in files):
            print(f"sample group already present: {group_root}")
            continue
        print(f"generating sample group -> {group_root}")
        for rel, size in files:
            path = os.path.join(group_root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(os.urandom(size))
    return roots


def main() -> None:
    argparse.ArgumentParser(
        description="Generate the built-in sample content to publish.",
        epilog="Nothing is catalogued here: hand a path to a node with "
               "`control.py publish` and the swarm takes it from there.",
    ).parse_args()
    roots = build_sample(config.SAMPLE_DIR)
    rel = os.path.relpath(roots[0], config.BASE_DIR)
    print("\nnow publish it to any running node:")
    print(f"  python control.py publish <node> {rel}")


if __name__ == "__main__":
    sys.exit(main())
