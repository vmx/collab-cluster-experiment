# BitTorrent v2 prototype + stats monitoring

A small local BitTorrent swarm for experimenting with how well the protocol's
metrics/stats work. **5 nodes** (1 seed + 4 leechers) share content (any file or
directory tree), coordinated by a **local HTTP tracker**, while a **separate
monitoring service** captures as many stats as possible into SQLite + JSON
snapshots.

- **BitTorrent v2 only** (SHA-256 merkle, no v1/hybrid).
- **Python stdlib only**, plus the `libtorrent` python binding (tested with
  libtorrent 2.0.13 / Python 3.13). The tracker even reuses libtorrent's
  `bencode`, so there are no third-party dependencies.

## Run

```bash
python run_network.py                       # share the built-in sample dataset
python run_network.py --content ~/some/dir  # share any file or directory tree
python run_network.py --until-complete      # stop once every leecher hits 100%
```

With no `--content`, it generates a small nested **sample dataset** (a few files
under `data/sample/`). Otherwise it builds a v2-only `.torrent` of the file or
directory you point at (nested directories included) and the seed serves it
**in place** — nothing is copied. Then it starts the tracker, the 5 nodes and the
monitor, which prints a live per-poll summary:

```
n0:100% ▲ 512K ▼   0K p4   n1: 47% ▲  64K ▼ 256K p2  ...
```

(`▲` upload KiB/s, `▼` download KiB/s, `pN` connected peers.)

## Run each component separately

`run_network.py` is only a convenience wrapper — you can start every piece by
hand (e.g. one terminal each) to watch individual logs or restart a single
component. Start them **in this order**; each keeps running until you Ctrl-C it.

```bash
# 1. Build the v2-only .torrent (writes data/shared.torrent + data/torrent_meta.json).
#    No arg = built-in sample dataset; or pass any file/directory to share.
#    Run once; the nodes read the meta sidecar to find the torrent + seed path.
python make_torrent.py                 # built-in sample
python make_torrent.py ~/some/dir      # or share your own files

# 2. Tracker (must be up before the nodes announce). Listens on :8000.
python bittorrent_tracker.py

# 3. The 5 nodes — one process each. node 0 is the seed, 1-4 are leechers.
#    BitTorrent port = 6881+id, stats HTTP port = 8001+id.
python node.py --id 0 --role seed
python node.py --id 1 --role leech
python node.py --id 2 --role leech
python node.py --id 3 --role leech
python node.py --id 4 --role leech

# 4. The monitor (start any time after the nodes; it prints the live summary).
python monitor.py
```

Notes:
- The seed (`--role seed`) serves the content in place from the path recorded in
  `data/torrent_meta.json` (the parent of what you shared); leechers
  (`--role leech`) reconstruct the tree under `nodes/<id>/`.
- Ports, node count and the seed/leecher split all come from `config.py`, so the
  commands above match the defaults there. Change `config.py` and the same
  commands keep working.
- Order matters only loosely: the tracker should be up before nodes announce,
  but nodes and the monitor tolerate each other starting in any order (the
  monitor just shows `n*:--` for nodes that aren't answering yet).
- Stop with Ctrl-C in each terminal. There's no shared shutdown when run this
  way, so stop the nodes/tracker individually.

## Inspect

```bash
# live JSON from a single node (session + torrent + per-peer stats)
curl -s http://127.0.0.1:8001/stats | python -m json.tool

# tracker's view of the swarm
curl -s http://127.0.0.1:8000/stats | python -m json.tool

# summary report from the recorded time-series
python report.py

# who has which pieces/files, and how many copies of each file exist
python piece_map.py             # live snapshot, once (incl. per-file copy counts)
python piece_map.py --snapshot  # read newest stats/snapshots/*.json instead of live
watch -n 2 python piece_map.py  # refresh every 2s (use the `watch` CLI tool)

# raw SQL
sqlite3 stats/monitor.db ".tables"
sqlite3 stats/monitor.db "SELECT node_id, MAX(progress) FROM torrent_status GROUP BY node_id;"
```

## Layout

| File | Role |
|---|---|
| `config.py` | Ports, counts, paths, timing — the single source of truth. |
| `make_torrent.py` | Build a v2-only `.torrent` from any file/dir (+ a meta sidecar); generates a sample if none given. |
| `bittorrent_tracker.py` | Stdlib HTTP tracker (`/announce`, `/scrape`, `/stats`). |
| `node.py` | One node: libtorrent session + `/stats` JSON endpoint. |
| `monitor.py` | Polls all nodes + tracker → SQLite + JSON snapshots. |
| `run_network.py` | Orchestrates the whole network. |
| `report.py` | Prints a summary from `stats/monitor.db`. |
| `piece_map.py` | Shows which peer holds which pieces/files + how many copies of each file exist. |
| `swarm_stats.py` | Shared helpers that aggregate node snapshots into per-piece/per-file copy stats (used by `monitor.py` + `piece_map.py`). |

## Ports

- Tracker: `8000`
- Node *i* BitTorrent: `6881 + i`
- Node *i* stats HTTP: `8001 + i`

## Outputs (generated, safe to delete)

- `data/` — `shared.torrent`, `torrent_meta.json`, and the `sample/` dataset
- `nodes/<id>/` — each leecher's download directory (mirrors the shared tree)
- `stats/monitor.db` — SQLite time-series (`node_session_stats`,
  `torrent_status`, `peer_info`, `tracker_stats`, `file_replication`)
- `stats/snapshots/*.json` — full combined snapshot per poll
- `stats/*.log` — per-process logs

## Captured stats

- **Session** (`node_session_stats`, long format): all ~296 libtorrent counters
  and gauges via `session_stats_metrics()` (net bytes, peer counts, disk, piece
  picker, …).
- **Torrent** (`torrent_status`): progress, up/download rates (incl. payload),
  totals, all-time totals, peer/seed/connection counts, pieces, distributed
  copies, seeding/finished flags.
- **Peers** (`peer_info`): per-connection up/down speed, payload speed, progress,
  totals, flags, source, RTT.
- **Tracker** (`tracker_stats`): seeders, leechers, peers, announce count.
- **Pieces & files** (in each node's live `/stats` + the JSON snapshots; consumed
  by `piece_map.py`): per-piece ownership bitfield and the static file→piece-range
  map, enabling who-has-what and per-file copy counts.
- **Per-file replication time-series** (`file_replication`): every poll the monitor
  records, for each file, `full_copies` (nodes holding the whole file),
  `recon_copies` (reconstructable copies = replication of the file's rarest piece)
  and the holder node-ids — so you can chart how copies grow over time.
  `report.py` summarises it (final/peak copies, time-to-full-replication per file).
