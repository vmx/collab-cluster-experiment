# BitTorrent v2 prototype + stats monitoring

A small local BitTorrent swarm for experimenting with how well the protocol's
metrics/stats work. **5 nodes** (1 seed + 4 leechers) share one file, coordinated
by a **local HTTP tracker**, while a **separate monitoring service** captures as
many stats as possible into SQLite + JSON snapshots.

- **BitTorrent v2 only** (SHA-256 merkle, no v1/hybrid).
- **Python stdlib only**, plus the `libtorrent` python binding (tested with
  libtorrent 2.0.13 / Python 3.13). The tracker even reuses libtorrent's
  `bencode`, so there are no third-party dependencies.

## Run

```bash
python run_network.py                 # runs until Ctrl-C
python run_network.py --until-complete # stops once every leecher hits 100%
```

This generates a 32 MiB random payload + a v2-only `.torrent`, starts the
tracker, the 5 nodes and the monitor. The monitor prints a live per-poll summary:

```
n0:100% ▲ 512K ▼   0K p4   n1: 47% ▲  64K ▼ 256K p2  ...
```

(`▲` upload KiB/s, `▼` download KiB/s, `pN` connected peers.)

## Run each component separately

`run_network.py` is only a convenience wrapper — you can start every piece by
hand (e.g. one terminal each) to watch individual logs or restart a single
component. Start them **in this order**; each keeps running until you Ctrl-C it.

```bash
# 1. Generate the payload + the v2-only .torrent (writes data/).
#    Run once; the other components read data/shared.torrent.
python make_torrent.py

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
- The seed (`--role seed`) reads the existing payload from `data/`; leechers
  (`--role leech`) download into `nodes/<id>/`.
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

# raw SQL
sqlite3 stats/monitor.db ".tables"
sqlite3 stats/monitor.db "SELECT node_id, MAX(progress) FROM torrent_status GROUP BY node_id;"
```

## Layout

| File | Role |
|---|---|
| `config.py` | Ports, counts, paths, timing — the single source of truth. |
| `make_torrent.py` | Generate the payload + the v2-only `.torrent`. |
| `bittorrent_tracker.py` | Stdlib HTTP tracker (`/announce`, `/scrape`, `/stats`). |
| `node.py` | One node: libtorrent session + `/stats` JSON endpoint. |
| `monitor.py` | Polls all nodes + tracker → SQLite + JSON snapshots. |
| `run_network.py` | Orchestrates the whole network. |
| `report.py` | Prints a summary from `stats/monitor.db`. |

## Ports

- Tracker: `8000`
- Node *i* BitTorrent: `6881 + i`
- Node *i* stats HTTP: `8001 + i`

## Outputs (generated, safe to delete)

- `data/` — payload + `.torrent`
- `nodes/<id>/` — each leecher's download directory
- `stats/monitor.db` — SQLite time-series (`node_session_stats`,
  `torrent_status`, `peer_info`, `tracker_stats`)
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
