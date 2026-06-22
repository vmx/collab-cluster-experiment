# BitTorrent v2 prototype + stats monitoring

A small local BitTorrent swarm for experimenting with how well the protocol's
metrics/stats work. You build a **catalog** of one or more torrents (each from
any file or directory tree), start a handful of **roleless node daemons**, then
drive the swarm by hand: tell each node to **seed** or **leech** specific
torrents via a small control API. A separate **monitoring service** captures as
many stats as possible into SQLite + JSON snapshots.

- **No tracker** — discovery is decentralized. A joining torrent dials one of a
  few **introducer** peers (`config.INTRODUCERS`) to enter the swarm, then
  **PEX** (peer exchange) gossips the rest of the peers. An introducer is just an
  ordinary peer, so any node can serve as one and a down introducer is simply
  swapped for the next — no central service to operate.
- **BitTorrent v2 only** (SHA-256 merkle, no v1/hybrid).
- **Multiple torrents at once** — a node can seed some and leech others
  simultaneously; all stats are reported per torrent.
- **Manually driven** — there's no catch-all orchestrator; you start each piece
  yourself and issue commands, so you can see exactly what's going on.
- **Restart-safe** — nodes persist their torrents via libtorrent fast-resume, so
  a node that's stopped (Ctrl-C / SIGTERM) and restarted comes back with the same
  torrents and download progress, no re-download and no re-issuing commands.
- **Python stdlib only**, plus the `libtorrent` python binding (tested with
  libtorrent 2.0.13 / Python 3.13). No third-party dependencies.

## Run

Start each piece by hand (e.g. one terminal each). Nodes are long-running and
start **empty**; you assign torrents to them at runtime with `control.py`.

```bash
# 1. Build the torrent catalog (writes data/torrents/<name>.torrent + <name>.json).
#    No args = built-in sample (two torrents: "media" and "documents").
#    Or pass any number of files/directories — each becomes its own torrent.
#    Torrents are trackerless; peers discover each other via introducer + PEX.
python make_torrent.py                       # built-in sample (two torrents)
python make_torrent.py ~/photos ~/some/file  # or build your own catalog

# 2. Start some node daemons — one process each, identified only by --id.
#    BitTorrent port = 6881+id, control/stats HTTP port = 8001+id. No role yet.
#    The default introducer (config.INTRODUCERS = [0]) is the bootstrap peer,
#    so node 0 should be up for newcomers to dial. List more ids for resilience.
python node.py --id 0
python node.py --id 1
python node.py --id 2
python node.py --id 3
python node.py --id 4

# 3. Drive the swarm with control.py (see `list` for available torrent names).
python control.py list                    # what's in the catalog
python control.py add 0 media --role seed     # node 0 seeds "media"
python control.py add 0 documents --role seed # ...and "documents"
python control.py add 1 media --role leech    # node 1 leeches "media"
python control.py add 2 media --role leech
python control.py add 3 documents --role leech
python control.py status                  # what every node currently holds
python control.py remove 3 documents      # tell a node to drop a torrent

# 4. The monitor (start any time; it prints the live per-poll summary).
python monitor.py
```

The seed serves its content **in place** from where `make_torrent.py` found it
(recorded in the catalog sidecar) — nothing is copied. Leechers reconstruct each
torrent's tree under `nodes/<id>/<name>/`. The monitor prints a live summary:

```
n0/media:100% ▲127K ▼3K p1  n1/media: 47% ▲ 64K ▼256K p2·pex1  n2/documents: 31% ...
```

(`nN/<torrent>`, `▲` upload KiB/s, `▼` download KiB/s, `pN` connected peers,
`·pexK` = of those peers, how many were discovered via PEX rather than a direct
introducer dial — i.e. peer exchange actually doing the work.)

Notes:
- Order matters only loosely: nodes and the monitor tolerate each other starting
  in any order (the monitor shows `nN:--` for nodes that aren't answering yet).
  Because discovery is trackerless, a newcomer can only bootstrap once one of its
  `config.INTRODUCERS` is up — so start an introducer (node 0 by default) early.
- Ports, node count and timing all come from `config.py`. `NUM_NODES` only sets
  how many node ports the monitor and `control.py status` scan; you can start
  as few or as many nodes as you like.
- Stop with Ctrl-C (or `kill`/SIGTERM) in each terminal. There's no shared
  shutdown. A node checkpoints fast-resume on the way down and on restart
  re-adds whatever it was holding (with progress intact) — so just relaunch
  `python node.py --id N` and it picks up where it left off, no `control.py`
  needed. To make a node truly forget a torrent, `control.py remove` it (that
  deletes its resume file); deleting `nodes/<id>/.resume/` forgets everything.

## Inspect

```bash
# live JSON from a single node (session + per-torrent status + per-peer stats,
# incl. each peer's discovery source — "pex", "incoming", ...)
curl -s http://127.0.0.1:8001/stats | python -m json.tool

# what each node is doing right now
python control.py status

# summary report from the recorded time-series (broken out per torrent)
python report.py

# who has which pieces/files, and how many copies of each file exist (per torrent)
python piece_map.py             # live snapshot, once (incl. per-file copy counts)
python piece_map.py --snapshot  # read newest stats/snapshots/*.json instead of live
watch -n 2 python piece_map.py  # refresh every 2s (use the `watch` CLI tool)

# raw SQL
sqlite3 stats/monitor.db ".tables"
sqlite3 stats/monitor.db "SELECT node_id, info_hash, MAX(progress) FROM torrent_status GROUP BY node_id, info_hash;"
```

## Layout

| File | Role |
|---|---|
| `config.py` | Ports, counts, paths, timing, `INTRODUCERS` — the single source of truth. |
| `make_torrent.py` | Build v2-only (trackerless) `.torrent`s from any files/dirs into the catalog (`data/torrents/`); generates a two-torrent sample if no args. Also the catalog-lookup helpers used by the node/control. |
| `node.py` | One roleless node daemon: libtorrent session (trackerless — bootstraps via `config.INTRODUCERS` + PEX) + HTTP `/stats` (GET) and `/add` `/remove` (POST) control endpoints. |
| `control.py` | CLI to list the catalog, inspect nodes, and tell nodes to seed/leech torrents. |
| `monitor.py` | Polls all nodes → SQLite + JSON snapshots (per torrent); surfaces PEX-discovered peer counts. |
| `report.py` | Prints a per-torrent summary from `stats/monitor.db`. |
| `piece_map.py` | Per torrent: which peer holds which pieces/files + how many copies of each file exist. |
| `swarm_stats.py` | Shared helpers that group node snapshots by torrent and aggregate per-piece/per-file copy stats (used by `monitor.py` + `piece_map.py`). |

## Ports

- Node *i* BitTorrent: `6881 + i`
- Node *i* control/stats HTTP: `8001 + i`

## Outputs (generated, safe to delete)

- `data/torrents/` — `<name>.torrent` + `<name>.json` per catalog entry
- `data/sample/` — the built-in sample datasets (when no content is given)
- `nodes/<id>/<name>/` — each leecher's download directory per torrent
- `nodes/<id>/.resume/<name>.resume` — libtorrent fast-resume per torrent (lets a
  restarted node restore its torrents + progress)
- `stats/monitor.db` — SQLite time-series (`node_session_stats`,
  `torrent_status`, `peer_info`, `file_replication`)
- `stats/snapshots/*.json` — full combined snapshot per poll
- `stats/*.log` — per-process logs (if you redirect them there)

## Captured stats

- **Session** (`node_session_stats`, long format): all ~296 libtorrent counters
  and gauges via `session_stats_metrics()` (net bytes, peer counts, disk, piece
  picker, …).
- **Torrent** (`torrent_status`, one row per node per torrent, keyed by
  `info_hash`): progress, up/download rates (incl. payload), totals, all-time
  totals, peer/seed/connection counts, pieces, distributed copies,
  seeding/finished flags.
- **Peers** (`peer_info`): per-connection up/down speed, payload speed, progress,
  totals, flags, source, RTT (tagged with the torrent name). The `source` bitmask
  records how each peer was discovered; with no tracker this is dominated by
  `pex` and `incoming`. `report.py` summarises the per-source counts, and the
  live `/stats` exposes decoded `source_flags`.
- **Pieces & files** (in each node's live `/stats` + the JSON snapshots; consumed
  by `piece_map.py`): per-piece ownership bitfield and the static file→piece-range
  map, enabling who-has-what and per-file copy counts.
- **Per-file replication time-series** (`file_replication`, keyed by `info_hash`
  + `torrent_name`): every poll, for each file, `full_copies` (nodes holding the
  whole file), `recon_copies` (reconstructable copies = replication of the file's
  rarest piece) and the holder node-ids — so you can chart how copies grow over
  time. `report.py` summarises it per torrent (final/peak copies, time-to-full).
