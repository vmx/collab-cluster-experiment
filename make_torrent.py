"""Generate the sample payload and a BitTorrent v2-only .torrent file.

v2-only means SHA-256 merkle hashing with no v1 (SHA-1) fallback or hybrid info.
"""
import os

import libtorrent as lt

import config


def make_payload() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if (os.path.exists(config.PAYLOAD_PATH)
            and os.path.getsize(config.PAYLOAD_PATH) == config.PAYLOAD_SIZE):
        print(f"payload already present: {config.PAYLOAD_PATH}")
        return
    print(f"generating {config.PAYLOAD_SIZE} bytes -> {config.PAYLOAD_PATH}")
    chunk = 1024 * 1024
    remaining = config.PAYLOAD_SIZE
    with open(config.PAYLOAD_PATH, "wb") as f:
        while remaining > 0:
            n = min(chunk, remaining)
            f.write(os.urandom(n))
            remaining -= n


def make_torrent() -> "lt.torrent_info":
    fs = lt.file_storage()
    lt.add_files(fs, config.PAYLOAD_PATH)
    # v2_only: produce a pure BitTorrent v2 torrent (no v1 SHA-1 hashes).
    ct = lt.create_torrent(fs, config.PIECE_SIZE, flags=lt.create_torrent.v2_only)
    ct.add_tracker(config.TRACKER_URL)
    ct.set_creator("bittorrent-prototype")
    ct.set_comment("v2-only prototype payload")
    # Hash the file(s) found under DATA_DIR (the parent of payload.bin).
    lt.set_piece_hashes(ct, config.DATA_DIR)
    with open(config.TORRENT_PATH, "wb") as f:
        f.write(lt.bencode(ct.generate()))

    ti = lt.torrent_info(config.TORRENT_PATH)
    ih = ti.info_hashes()
    print(f"wrote {config.TORRENT_PATH}")
    print(f"  v2 info-hash : {ih.v2}")
    print(f"  has_v1={ih.has_v1()}  has_v2={ih.has_v2()}")
    print(f"  pieces={ti.num_pieces()}  piece_len={ti.piece_length()}  size={ti.total_size()}")
    return ti


def main() -> None:
    make_payload()
    make_torrent()


if __name__ == "__main__":
    main()
