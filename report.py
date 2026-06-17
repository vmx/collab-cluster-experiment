"""Summarize a finished (or running) experiment from stats/monitor.db.

Run after (or during) `python run_network.py` to sanity-check the captured
metrics: transfer rates, time-to-complete, bytes moved, and tracker activity.
"""
import sqlite3

import config


def main() -> None:
    db = sqlite3.connect(config.DB_PATH)
    db.row_factory = sqlite3.Row

    print("=== Per-node torrent summary ===")
    rows = db.execute("""
        SELECT node_id,
               MAX(progress)        AS max_progress,
               MAX(download_rate)   AS peak_dl,
               AVG(download_rate)   AS avg_dl,
               MAX(upload_rate)     AS peak_ul,
               MAX(total_download)  AS bytes_dl,
               MAX(total_upload)    AS bytes_ul,
               MAX(num_peers)       AS max_peers
        FROM torrent_status GROUP BY node_id ORDER BY node_id
    """).fetchall()
    for r in rows:
        print(f" node {r['node_id']}: {r['max_progress'] * 100:5.1f}%  "
              f"peakDL {r['peak_dl'] / 1024:8.0f}K  avgDL {r['avg_dl'] / 1024:8.0f}K  "
              f"peakUL {r['peak_ul'] / 1024:8.0f}K  "
              f"dl {r['bytes_dl'] / 1e6:6.1f}MB ul {r['bytes_ul'] / 1e6:6.1f}MB  "
              f"peers {r['max_peers']}")

    print("\n=== Time to complete (since first sample) ===")
    base_row = db.execute("SELECT MIN(ts) FROM torrent_status").fetchone()
    base = base_row[0] if base_row else None
    if base is not None:
        for (nid,) in db.execute(
                "SELECT DISTINCT node_id FROM torrent_status ORDER BY node_id"):
            done = db.execute(
                "SELECT MIN(ts) FROM torrent_status WHERE node_id=? AND progress>=1.0",
                (nid,)).fetchone()[0]
            print(f" node {nid}: {done - base:6.1f}s" if done else
                  f" node {nid}: never completed")

    print("\n=== Tracker (max observed) ===")
    for r in db.execute("""
            SELECT info_hash, MAX(seeders) AS s, MAX(leechers) AS l,
                   MAX(peers) AS p, MAX(announces) AS a
            FROM tracker_stats GROUP BY info_hash"""):
        print(f" {r['info_hash'][:16]}...  seeders {r['s']}  leechers {r['l']}  "
              f"peers {r['p']}  announces {r['a']}")

    print("\n=== Selected session metrics (node 0 seed, peak/cumulative) ===")
    wanted = ("net.sent_payload_bytes", "net.recv_payload_bytes",
              "net.sent_bytes", "net.recv_bytes",
              "peer.num_peers_connected", "ses.num_downloading_torrents",
              "ses.num_seeding_torrents")
    placeholders = ",".join("?" * len(wanted))
    for r in db.execute(
            f"SELECT metric, MAX(value) AS v FROM node_session_stats "
            f"WHERE node_id=0 AND metric IN ({placeholders}) GROUP BY metric",
            wanted):
        print(f"  {r['metric']}: {r['v']:.0f}")

    total_session_rows = db.execute(
        "SELECT COUNT(*) FROM node_session_stats").fetchone()[0]
    print(f"\n(recorded {total_session_rows} session-metric data points across "
          f"{config.NUM_NODES} nodes)")


if __name__ == "__main__":
    main()
