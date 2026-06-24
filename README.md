# BitTorrent v2 prototype + stats collection

A BitTorrent swarm for experimenting with how well the protocol's metrics/stats
work — built to run as independent nodes that may be spread across separate
hosts/data centers, not just on one box. You build a **catalog** of one or more
torrents (each from any file or directory tree), start a handful of **roleless
node daemons**, then drive the swarm by hand: tell each node to **seed** or
**leech** specific torrents via a small control API. Each node **pushes** its
stats to a central **collector**, which aggregates them in memory and serves a
live view of the whole swarm.

- **Push-based collection** — nodes POST their `/stats` snapshot to the collector
  (`collector.py`); the collector is the one service that takes inbound
  connections, so nodes only ever dial out (NAT/firewall-friendly). Nodes are
  independent, each identified by a persisted UUID (`node_key`); there's no node
  enumeration — the collector learns nodes as they report.

- **Tracker-based discovery** — peers find each other by announcing to a central
  tracker (`bittorrent_tracker.py`, a tiny stdlib script; no whitelist). Torrents
  are built **private** (announce URL baked in), which makes libtorrent disable
  PEX/DHT/LSD, so discovery is purely tracker-driven.
- **Torrents live on the tracker** — the same tracker also hosts the torrent
  catalog over HTTP (`/catalog`), so a node needs only the tracker URL to obtain
  both peers and the `.torrent` itself; there's no shared catalog directory.
- **BitTorrent v2 only** (SHA-256 merkle, no v1/hybrid).
- **Multiple torrents at once** — a node can seed some and leech others
  simultaneously; all stats are reported per torrent.
- **Manually driven** — there's no catch-all orchestrator; you start each piece
  yourself and issue commands, so you can see exactly what's going on.
- **Restart-safe** — nodes persist their torrents via libtorrent fast-resume, so
  a node that's stopped (Ctrl-C / SIGTERM) and restarted comes back with the same
  torrents and download progress, no re-download and no re-issuing commands.
- **Python stdlib only**, plus the `libtorrent` python binding (tested with
  libtorrent 2.0.13 / Python 3.13). No third-party dependencies. The browser
  dashboard is just as self-contained — a no-build [Tutuca](https://github.com/marianoguerra/tutuca)
  SPA with the framework vendored as a single `webui/tutuca.js`, so nothing is
  installed and nothing is fetched from the internet at runtime.

## Run

Start each piece by hand (e.g. one terminal each). Nodes are long-running and
start **empty**; you assign torrents to them at runtime with `control.py`.

```bash
# 1. Build the torrent catalog (writes data/torrents/<name>.torrent + <name>.json).
#    No args = built-in sample (two torrents: "media" and "documents").
#    Or pass any number of files/directories — each becomes its own torrent.
#    Torrents are private + tracker-based (announce URL baked in).
python make_torrent.py                       # built-in sample (two torrents)
python make_torrent.py ~/photos ~/some/file  # or build your own catalog

# 2. Start the tracker. It serves announces AND hosts the torrent catalog, so it
#    should be up before nodes/control run. Listens on :8000.
python bittorrent_tracker.py

# 3. Start the collector (start any time; nodes buffer nothing, they just push
#    on their next tick). Receives node snapshots and serves a live view (/live,
#    /summary) plus the web UI; in-memory only, nothing is persisted. Listens on
#    :8100 — open http://127.0.0.1:8100/ in a browser for the live dashboard.
python collector.py

# 4. Start some node daemons — one process each, identified only by --id.
#    BitTorrent port = 6881+id, control/stats HTTP port = 8001+id. No role yet.
#    Each node pushes its stats to the collector (--collector, defaults from
#    config.py; pass --collector '' to run a node without reporting).
python node.py --id 0
python node.py --id 1
python node.py --id 2
python node.py --id 3
python node.py --id 4

# 5. Drive the swarm with control.py (see `list` for available torrent names).
#    control is operator-local: you run it on the same host as the node and
#    target it by id. Swarm-wide views come from the collector (see Inspect).
python control.py list                    # what's in the catalog
python control.py add 0 media --role seed     # node 0 seeds "media"
python control.py add 0 documents --role seed # ...and "documents"
python control.py add 1 media --role leech    # node 1 leeches "media"
python control.py add 2 media --role leech
python control.py add 3 documents --role leech
python control.py status 1                 # what node 1 currently holds
python control.py remove 3 documents      # tell a node to drop a torrent
```

The seed serves its content **in place** from where `make_torrent.py` found it
(recorded in the catalog sidecar) — nothing is copied. Leechers reconstruct each
torrent's tree under `nodes/<id>/<name>/`. For a live swarm-wide view, watch the
piece map (it reads the collector):

```
watch -n 2 python piece_map.py
```

Notes:
- Order matters only loosely: collector, tracker and nodes tolerate each other
  starting in any order. A node that can't reach the collector just drops that
  push and tries again next tick; one that announces while the tracker is down
  retries on the next announce (`ANNOUNCE_INTERVAL`). A node silent past
  `NODE_STALE_AFTER` falls out of the collector's live view until it reports again.
- Ports and timing come from `config.py`. There is no fixed node count — start as
  few or as many nodes as you like; the collector learns each by its `node_key`.
- Stop with Ctrl-C (or `kill`/SIGTERM) in each terminal. There's no shared
  shutdown. A node checkpoints fast-resume on the way down and on restart
  re-adds whatever it was holding (with progress intact) — so just relaunch
  `python node.py --id N` and it picks up where it left off, no `control.py`
  needed. To make a node truly forget a torrent, `control.py remove` it (that
  deletes its resume file); deleting `nodes/<id>/.resume/` forgets everything.

## Inspect

The easiest swarm-wide view is the **web UI**: open <http://127.0.0.1:8100/> in a
browser once the collector is running. It's the piece map in the browser — per-node
piece ownership, per-piece availability, the availability histogram, the
"copies of the dataset" summary, and per-file replication — and it refreshes itself
every second. The UI is a zero-build [Tutuca](https://github.com/marianoguerra/tutuca)
SPA served straight from `webui/` (the framework is vendored as a single
`webui/tutuca.js`, so there's nothing to install and no internet needed at runtime).
It reads the collector's `/summary`, which is built by `swarm_stats` — the same
aggregation `piece_map.py` uses — so the browser and the terminal never disagree.

For the terminal, JSON, or scripting:

```bash
# live JSON from a single node (session + per-torrent status + per-peer stats,
# incl. each peer's discovery source — "tracker", "incoming", ...)
curl -s http://127.0.0.1:8001/stats | python -m json.tool

# the collector's view: live swarm state (latest snapshot per fresh node), the
# aggregated per-torrent summary the web UI renders, and its own health (which
# nodes are reporting, last-seen age)
curl -s http://127.0.0.1:8100/live    | python -m json.tool
curl -s http://127.0.0.1:8100/summary | python -m json.tool
curl -s http://127.0.0.1:8100/stats   | python -m json.tool

# the tracker's view of the swarm, and the catalog it hosts
curl -s http://127.0.0.1:8000/stats | python -m json.tool
curl -s http://127.0.0.1:8000/catalog | python -m json.tool

# what one (local) node is doing right now
python control.py status 0

# who has which pieces/files, and how many copies of each file exist (per torrent)
python piece_map.py             # live from the collector, once (incl. copy counts)
watch -n 2 python piece_map.py  # refresh every 2s (use the `watch` CLI tool)
```

## Layout

| File | Role |
|---|---|
| `config.py` | Ports, counts, paths, timing, tracker URL — the single source of truth. |
| `make_torrent.py` | Build v2-only, private, tracker-based `.torrent`s from any files/dirs into the catalog (`data/torrents/`, the tracker's store); generates a two-torrent sample if no args. Also the local catalog-lookup helpers the tracker uses to serve `/catalog`. |
| `bittorrent_tracker.py` | The tiny stdlib tracker: `/announce` + `/scrape` + `/stats`, keyed by info-hash (no whitelist), and `/catalog` endpoints that host the torrents. |
| `catalog.py` | Stdlib client the node/control use to fetch the catalog (list, meta, `.torrent`) from the tracker over HTTP. |
| `node.py` | One roleless node daemon: libtorrent session (announces to the tracker; fetches torrents from its `/catalog`) + HTTP `/stats` (GET) and `/add` `/remove` (POST) control endpoints; pushes its snapshot to the collector (`--collector`). Has a persisted `node_key`. |
| `control.py` | Operator-local CLI to list the catalog (from the tracker), inspect a local node, and tell nodes to seed/leech torrents. |
| `collector.py` | Central push endpoint: keeps the latest snapshot per node in memory (no persistence) and serves `/live` (live swarm state), `/summary` (the per-torrent aggregation the web UI renders), `/stats` (collector health), and the web UI from `webui/`. |
| `piece_map.py` | Per torrent: which peer holds which pieces/files + how many copies of each file exist (reads the collector's `/live`). |
| `swarm_stats.py` | Shared helpers that group node snapshots by torrent and aggregate per-piece/per-file copy stats (used by `collector.py` for `/summary` + `piece_map.py`). |
| `webui/` | The browser dashboard: `index.html` + `app.js` (a [Tutuca](https://github.com/marianoguerra/tutuca) SPA, no build step) polling `/summary`, plus the vendored single-file `tutuca.js` framework. Served by `collector.py`. |

## Ports

- Tracker (`bittorrent_tracker.py`): `8000`
- Collector (`collector.py`): `8100`
- Node *i* BitTorrent: `6881 + i`
- Node *i* control/stats HTTP: `8001 + i`

## Outputs (generated, safe to delete)

- `data/torrents/` — `<name>.torrent` + `<name>.json` per catalog entry (the
  tracker's store, served via `/catalog`)
- `data/sample/` — the built-in sample datasets (when no content is given)
- `nodes/<id>/<name>/` — each leecher's download directory per torrent
- `nodes/<id>/.resume/<name>.resume` — libtorrent fast-resume per torrent (lets a
  restarted node restore its torrents + progress)
- `nodes/<id>/node_key` — the node's persisted UUID identity
- `stats/*.log` — per-process logs (only if you redirect them there)

The collector keeps no on-disk state: the swarm view lives in memory and is
rebuilt from node pushes within one push interval, so there's nothing to clean up
and no historic record. Each metric below is present in every live snapshot
(`/stats` on a node, `/live` on the collector) but is **not** stored over time —
you see current values, not trends.

## Captured stats

Each node's snapshot (and thus the collector's `/live`) carries, per torrent:

- **Session**: all ~296 libtorrent counters and gauges via
  `session_stats_metrics()` (net bytes, peer counts, disk, piece picker, …).
- **Torrent** (keyed by `info_hash`): progress, current up/download rates (incl.
  payload), totals, all-time totals, peer/seed/connection counts, pieces,
  distributed copies, seeding/finished flags.
- **Peers**: per-connection up/down speed, payload speed, progress, totals, flags,
  source, RTT (tagged with the torrent name). The `source` bitmask records how
  each peer was discovered (dominated by `tracker` and `incoming`); the snapshot
  also exposes decoded `source_flags`.
- **Pieces & files** (consumed by `piece_map.py`): per-piece ownership bitfield
  and the static file→piece-range map, enabling who-has-what and, aggregated
  across nodes, current per-file copy counts (`full_copies`, and `recon_copies` =
  replication of each file's rarest piece).
