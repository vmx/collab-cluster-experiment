"""Summarize a finished (or running) experiment from stats/monitor.db.

Run after (or during) a session to sanity-check the captured metrics: transfer
rates, time-to-complete, bytes moved, how peers were discovered (tracker, etc.)
and per-file replication. Everything is broken out per torrent, since a swarm can
host several at once.
"""
import sqlite3

import config
import swarm_stats


def torrent_names(db) -> dict:
    """info_hash (v2) -> torrent name, from the file_replication table."""
    return {ih: name for ih, name in db.execute(
        "SELECT DISTINCT info_hash, torrent_name FROM file_replication")}


def main() -> None:
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row
    names = torrent_names(db)

    hashes = [r[0] for r in db.execute(
        "SELECT DISTINCT info_hash FROM torrent_status ORDER BY info_hash")]
    if not hashes:
        print("no torrent_status rows yet — has the monitor run with assigned torrents?")
        return

    for ih in hashes:
        name = names.get(ih, ih[:12] + "...")
        print(f"\n##### torrent '{name}'  ({ih[:16]}...) #####")

        print("=== Per-node summary ===")
        for r in db.execute("""
                SELECT node_id,
                       MAX(progress)        AS max_progress,
                       MAX(download_rate)   AS peak_dl,
                       AVG(download_rate)   AS avg_dl,
                       MAX(upload_rate)     AS peak_ul,
                       MAX(total_download)  AS bytes_dl,
                       MAX(total_upload)    AS bytes_ul,
                       MAX(num_peers)       AS max_peers
                FROM torrent_status WHERE info_hash=? GROUP BY node_id ORDER BY node_id
                """, (ih,)):
            print(f" node {r['node_id']}: {r['max_progress'] * 100:5.1f}%  "
                  f"peakDL {r['peak_dl'] / 1024:8.0f}K  avgDL {r['avg_dl'] / 1024:8.0f}K  "
                  f"peakUL {r['peak_ul'] / 1024:8.0f}K  "
                  f"dl {r['bytes_dl'] / 1e6:6.1f}MB ul {r['bytes_ul'] / 1e6:6.1f}MB  "
                  f"peers {r['max_peers']}")

        print("--- Time to complete (since this torrent's first sample) ---")
        base = db.execute("SELECT MIN(ts) FROM torrent_status WHERE info_hash=?",
                          (ih,)).fetchone()[0]
        for (nid,) in db.execute(
                "SELECT DISTINCT node_id FROM torrent_status WHERE info_hash=? "
                "ORDER BY node_id", (ih,)):
            done = db.execute(
                "SELECT MIN(ts) FROM torrent_status "
                "WHERE info_hash=? AND node_id=? AND progress>=1.0",
                (ih, nid)).fetchone()[0]
            print(f" node {nid}: {done - base:6.1f}s" if done else
                  f" node {nid}: never completed")

        print("--- Per-file replication ---")
        nnodes = db.execute(
            "SELECT COUNT(DISTINCT node_id) FROM torrent_status WHERE info_hash=?",
            (ih,)).fetchone()[0]
        frbase = db.execute("SELECT MIN(ts) FROM file_replication WHERE info_hash=?",
                            (ih,)).fetchone()[0]
        if frbase is None:
            print("  (no file_replication rows yet)")
        else:
            print(f"  {'file':<34} {'size':>9} {'final':>7} {'peak':>5} {'t_full(s)':>10}")
            for (path,) in db.execute(
                    "SELECT DISTINCT file_path FROM file_replication "
                    "WHERE info_hash=? ORDER BY file_path", (ih,)):
                size, peak = db.execute(
                    "SELECT MAX(size), MAX(full_copies) FROM file_replication "
                    "WHERE info_hash=? AND file_path=?", (ih, path)).fetchone()
                final = db.execute(
                    "SELECT full_copies FROM file_replication "
                    "WHERE info_hash=? AND file_path=? ORDER BY ts DESC LIMIT 1",
                    (ih, path)).fetchone()[0]
                t_full = db.execute(
                    "SELECT MIN(ts) FROM file_replication "
                    "WHERE info_hash=? AND file_path=? AND full_copies>=?",
                    (ih, path, nnodes)).fetchone()[0]
                disp = path.split("/", 1)[1] if "/" in path else path
                when = f"{t_full - frbase:10.1f}" if t_full is not None else "     never"
                print(f"  {disp:<34} {size / 1e6:7.1f}MB {final:>3}/{nnodes:<3} "
                      f"{peak:>5} {when}")
            print(f"  (t_full = time until all {nnodes} holders had the whole file)")

    print("\n=== Peer discovery (distinct peer endpoints by source) ===")
    print("(tracker-only: peers are learned by announcing to the tracker)")
    for bit, label in swarm_stats.PEER_SOURCE_FLAGS:
        n = db.execute("SELECT COUNT(DISTINCT peer_ip) FROM peer_info "
                       "WHERE source & ?", (bit,)).fetchone()[0]
        if n:
            print(f"  {label:<9} {n}")

    total_session_rows = db.execute(
        "SELECT COUNT(*) FROM node_session_stats").fetchone()[0]
    print(f"\n(recorded {total_session_rows} session-metric data points across "
          f"{config.NUM_NODES} nodes)")


if __name__ == "__main__":
    main()
