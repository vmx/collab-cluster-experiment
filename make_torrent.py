"""Create BitTorrent v2-only .torrent files from arbitrary local files or
directories (nested directories are included recursively) and register them in
a small on-disk catalog the nodes can resolve by name.

    python make_torrent.py /path/to/dir-or-file [more paths...]
    python make_torrent.py                # no args: generate two sample torrents

Each torrent is written to the catalog as a pair under config.TORRENTS_DIR:
    <name>.torrent   the torrent itself
    <name>.json      sidecar meta: {id, torrent, seed_save_path, name, source,
                                     info_hash}
where <name> is the torrent's name (the basename of the shared file/dir). The
seed serves the content *in place* from where it lives: we record the content's
parent directory as the seed's save_path so a node can seed it without copying.
Leechers download into nodes/<id>/<name>/.

v2-only => SHA-256 merkle hashing, no v1/hybrid.
"""
import glob
import json
import os
import sys

import libtorrent as lt

import config

# Built-in sample: two separate content roots => two separate torrents, so the
# multi-torrent machinery is exercised out of the box. (relative path, size).
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


# --- catalog helpers (used by node.py and control.py) ------------------------

def catalog_torrent_path(name: str) -> str:
    return os.path.join(config.TORRENTS_DIR, f"{name}.torrent")


def catalog_meta_path(name: str) -> str:
    return os.path.join(config.TORRENTS_DIR, f"{name}.json")


def load_meta(name: str) -> dict:
    """Resolve a catalog torrent by name. Raises FileNotFoundError if unknown."""
    with open(catalog_meta_path(name)) as f:
        return json.load(f)


def list_catalog() -> list:
    """All registered torrents (meta dicts), sorted by name."""
    metas = []
    for path in sorted(glob.glob(os.path.join(config.TORRENTS_DIR, "*.json"))):
        with open(path) as f:
            metas.append(json.load(f))
    return sorted(metas, key=lambda m: m["name"])


def write_whitelist() -> str:
    """(Re)write the opentracker whitelist covering the whole catalog.

    opentracker keys on the 20-byte info-hash. A v2 torrent announces using the
    first 20 bytes of its SHA-256 v2 hash, i.e. the first 40 hex chars of the
    sidecar's `info_hash`. Run the tracker with `-w config.WHITELIST_PATH`.
    """
    lines = [m["info_hash"][:40] for m in list_catalog()]
    with open(config.WHITELIST_PATH, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    print(f"wrote {config.WHITELIST_PATH} ({len(lines)} torrent(s)) — "
          f"run: opentracker -i {config.HOST} -p {config.TRACKER_PORT} "
          f"-P {config.TRACKER_PORT} -w {config.WHITELIST_PATH}")
    return config.WHITELIST_PATH


# --- building ----------------------------------------------------------------

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


def _is_pad(fs, i: int) -> bool:
    if hasattr(fs, "pad_file_at"):
        try:
            return fs.pad_file_at(i)
        except Exception:
            pass
    return "/.pad/" in fs.file_path(i).replace(os.sep, "/")


def make_torrent(source: str) -> dict:
    source = os.path.abspath(source)
    if not os.path.exists(source):
        sys.exit(f"error: content path does not exist: {source}")
    seed_save_path = os.path.dirname(source)  # parent of the content root

    fs = lt.file_storage()
    # add_files recurses into directories and preserves the nested layout.
    lt.add_files(fs, source)
    if fs.total_size() == 0:
        sys.exit(f"error: no data found under {source}")

    ct = lt.create_torrent(fs, config.PIECE_SIZE, flags=lt.create_torrent.v2_only)
    ct.add_tracker(config.TRACKER_URL)
    # Private => discovery is tracker-only: libtorrent disables PEX, DHT and LSD
    # for this torrent, so peers find each other purely by announcing to the
    # tracker. (The private flag lives in the info-dict, so it is part of the
    # info-hash — rebuild torrents after changing it.)
    ct.set_priv(True)
    ct.set_creator("bittorrent-prototype")
    ct.set_comment(f"v2-only prototype content: {os.path.basename(source)}")
    lt.set_piece_hashes(ct, seed_save_path)  # hash the files as they sit on disk

    os.makedirs(config.TORRENTS_DIR, exist_ok=True)
    torrent_path = catalog_torrent_path(fs.name())
    with open(torrent_path, "wb") as f:
        f.write(lt.bencode(ct.generate()))

    ti = lt.torrent_info(torrent_path)
    name = ti.name()
    meta = {
        "id": name,
        "torrent": torrent_path,
        "seed_save_path": seed_save_path,
        "name": name,
        "source": source,
        "info_hash": str(ti.info_hashes().v2),
    }
    with open(catalog_meta_path(name), "w") as f:
        json.dump(meta, f, indent=2)

    real_files = [(fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files())
                  if not _is_pad(fs, i)]
    print(f"wrote {torrent_path}")
    print(f"  name         : {name}")
    print(f"  v2 info-hash : {meta['info_hash']}")
    print(f"  files        : {len(real_files)}  (total {ti.total_size()} bytes)")
    print(f"  pieces       : {ti.num_pieces()} x {ti.piece_length()} bytes")
    print(f"  seed serves from: {seed_save_path}")
    for path, size in real_files:
        print(f"    - {path}  ({size} bytes)")
    return meta


def main(sources=None) -> None:
    if not sources:
        sources = build_sample(config.SAMPLE_DIR)
    elif isinstance(sources, str):
        sources = [sources]
    for src in sources:
        make_torrent(src)
    # Keep the tracker whitelist in sync with the full catalog.
    write_whitelist()


if __name__ == "__main__":
    main(sys.argv[1:] or None)
