"""Create a BitTorrent v2-only .torrent from an arbitrary local file or
directory (nested directories are included recursively).

    python make_torrent.py /path/to/dir-or-file
    python make_torrent.py                # no arg: generate a nested sample dataset

The seed serves the content *in place* from wherever it lives: we record the
content's parent directory as the seed's save_path in a small JSON sidecar
(config.META_PATH) that the nodes read. Leechers download into nodes/<id>/.

v2-only => SHA-256 merkle hashing, no v1/hybrid.
"""
import json
import os
import sys

import libtorrent as lt

import config

# (relative path, size in bytes) for the built-in sample dataset.
SAMPLE_FILES = [
    ("notes.txt", 12 * 1024),
    ("images/photo_a.bin", 5 * 1024 * 1024),
    ("images/photo_b.bin", 3 * 1024 * 1024),
    ("docs/report.bin", 6 * 1024 * 1024),
    ("docs/appendix/data.bin", 8 * 1024 * 1024),
]


def build_sample(root: str) -> None:
    """Generate a nested sample directory tree (only if not already present)."""
    if os.path.isdir(root) and all(
            os.path.exists(os.path.join(root, p)) for p, _ in SAMPLE_FILES):
        print(f"sample dataset already present: {root}")
        return
    print(f"generating sample dataset -> {root}")
    for rel, size in SAMPLE_FILES:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(size))


def make_torrent(source: str) -> "lt.torrent_info":
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
    ct.set_creator("bittorrent-prototype")
    ct.set_comment(f"v2-only prototype content: {os.path.basename(source)}")
    lt.set_piece_hashes(ct, seed_save_path)  # hash the files as they sit on disk

    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(config.TORRENT_PATH, "wb") as f:
        f.write(lt.bencode(ct.generate()))

    ti = lt.torrent_info(config.TORRENT_PATH)
    meta = {
        "torrent": config.TORRENT_PATH,
        "seed_save_path": seed_save_path,
        "name": ti.name(),
        "source": source,
    }
    with open(config.META_PATH, "w") as f:
        json.dump(meta, f, indent=2)

    real_files = [(fs.file_path(i), fs.file_size(i)) for i in range(fs.num_files())
                  if not _is_pad(fs, i)]
    print(f"wrote {config.TORRENT_PATH}")
    print(f"  name         : {ti.name()}")
    print(f"  v2 info-hash : {ti.info_hashes().v2}")
    print(f"  files        : {len(real_files)}  (total {ti.total_size()} bytes)")
    print(f"  pieces       : {ti.num_pieces()} x {ti.piece_length()} bytes")
    print(f"  seed serves from: {seed_save_path}")
    for path, size in real_files:
        print(f"    - {path}  ({size} bytes)")
    return ti


def _is_pad(fs, i: int) -> bool:
    if hasattr(fs, "pad_file_at"):
        try:
            return fs.pad_file_at(i)
        except Exception:
            pass
    return "/.pad/" in fs.file_path(i).replace(os.sep, "/")


def main(source: str | None = None) -> None:
    if source is None:
        build_sample(config.SAMPLE_DIR)
        source = config.SAMPLE_DIR
    make_torrent(source)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
