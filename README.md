# BitTorrent v2 prototype + stats collection

A BitTorrent swarm for experimenting with how well the protocol's metrics/stats
work — built to run as independent nodes that may be spread across separate
hosts/data centers, not just on one box. You build a **catalog** of one or more
torrents (each from any file or directory tree), start a handful of **empty
node daemons**, then drive the swarm by hand: tell each node to **serve** or
**download** specific torrents via a small control API. Each node **pushes** its
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

## BitTorrent basics

If you're new to BitTorrent, here's just enough of the vocabulary to follow the
rest of this README. The terms in **bold** are the ones that show up in the
commands, stats, and web UI.

- **Torrent file** (`.torrent`) — a small metadata file that describes *one*
  dataset (a single file, or a whole directory tree shared as a group). It is
  **not** the content; it's the recipe for fetching it. It lists the files and
  their sizes, the **piece** size, a cryptographic hash for every piece, and one
  or more **tracker** URLs. In this prototype `make_torrent.py` builds these into
  the catalog, and the tracker hands them out over `/catalog`.

- **Pieces** — the content is sliced into fixed-size chunks called pieces (the
  torrent file records a hash of each). Peers exchange pieces, not whole files,
  so a download arrives out of order and from many peers at once. Because every
  piece is verified against its hash, corrupt or malicious data can't slip in.
  Which peer holds which pieces is exactly what `piece_map.py` and the web UI's
  drill-down visualise.

- **Info-hash** — the hash of the torrent's "info" section, used as the dataset's
  unique ID. Peers and the tracker refer to a dataset solely by its info-hash;
  you'll see it in URLs like `/api/torrent/<info_hash>`.

- **Seed / leech** — a **seeder** has the complete dataset and only uploads; a
  **leecher** is still downloading (and uploads the pieces it already has to
  others). It's a live status, not a fixed role: a leecher that finishes becomes
  a seeder. A **swarm** is everyone — seeders and leechers — trading a given
  dataset. `control.py add … --mode serve|download` only chooses how a node
  starts: `serve` hosts a copy the node already has on disk (point `--path` at
  the local file/dir), `download` fetches a copy.

- **Tracker** — a lightweight server that lets peers find each other. A peer
  **announces** ("I want dataset X, here's my address") and gets back a list of
  other peers in that swarm; the tracker never sees the content itself. Normally
  BitTorrent can *also* find peers without a tracker (via **DHT**, **PEX**, and
  **LSD** — decentralised peer-gossip mechanisms), but this prototype builds its
  torrents **private**, which disables those and forces discovery through the one
  tracker (`bittorrent_tracker.py`) so the swarm stays observable.

- **BitTorrent v2** — the newer version of the format (BEP 52). It uses SHA-256
  merkle trees for hashing instead of v1's SHA-1, giving per-file (not just
  per-torrent) integrity and cleaner handling of many-file torrents. This
  prototype is **v2-only** — no v1 or hybrid torrents.

## Run

Start each piece by hand (e.g. one terminal each). Nodes are long-running and
start **empty**; you assign torrents to them at runtime with `control.py`.

```bash
# 1. Build the torrent catalog (writes data/torrents/<name>.torrent).
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
#    BitTorrent port = 6881+id, control/stats HTTP port = 8001+id. No torrents yet.
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
python control.py add 0 media --mode serve --path data/sample/media         # node 0 serves "media"
python control.py add 0 documents --mode serve --path data/sample/documents # ...and "documents"
python control.py add 1 media --mode download     # node 1 downloads "media"
python control.py add 2 media --mode download
python control.py add 3 documents --mode download
python control.py status 1                 # what node 1 currently holds
python control.py remove 3 documents      # tell a node to drop a torrent
```

To watch the catalog grow, subscribe to newly added torrents. The tracker
watches its catalog dir and streams each new torrent (Server-Sent Events at
`/catalog/subscribe`), so anything `make_torrent.py` adds shows up live with no
polling:

```
python control.py subscribe               # tail -f the catalog; Ctrl-C to stop
# then, in another terminal, `python make_torrent.py <path>` and watch it appear.
```

By default a subscriber sees only torrents added after it connects; pass
`--since <seq>` to resume (each event carries a `seq`), or `--since 0` to replay
the whole catalog first. The web dashboard also toasts new datasets as they land
(the collector relays the stream, exposed as `/api/catalog/recent`).

A node in `serve` mode hosts its content **in place** from the `--path` you give
it (the local `<name>` file/dir itself, i.e. what you passed to `make_torrent.py`)
— nothing is copied, and that path stays local to the node, never entering the
catalog. Downloaders reconstruct each
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
browser once the collector is running. It has a few screens, reachable from the top
nav — real routes via the History API (URLPattern matching, not hashes), each a
shareable, reloadable URL — and it refreshes itself every second:

- **Overview** (`/`) — one row per dataset: size, copy counts (the weakest-link
  "durable" copies up front, with full copies and the rarest piece as context), a
  per-node *spread* strip showing how much of the dataset each node holds, and the
  live activity (replicating vs complete, with throughput). A **search box** filters
  by dataset name and **status chips** (All / Replicating / Incomplete) narrow the
  list — both applied client-side, instantly, with a "showing X of Y" count. It
  reads `/api/overview`, which carries **no per-piece bitfields**, so it stays cheap
  to poll no matter how many datasets/nodes there are. Datasets are sorted
  rarest-copies-first so the least-replicated float to the top.
- **Drill-down** (`/dataset/<info_hash>`, click any row) — the full piece map for
  one dataset: per-node piece ownership, per-piece availability, the availability
  histogram, the copies summary, and per-file replication. It reads the dataset's
  detail from `/api/torrent/<info_hash>` only while that dataset is open. Each
  per-node row links to that node's page (see Nodes), so you can pivot from "which
  nodes hold this dataset" to "what else those nodes hold".
- **Transfers** (`/transfers`) — what's moving right now: one row per incomplete
  (node, dataset) with a progress bar, live rate and ETA (stalled transfers show
  too, without an ETA), soonest-done first. Reads `/api/transfers`.
- **Nodes** (`/nodes`) — the infrastructure side of "where is the data": per node,
  how much it stores, how much disk it has free, how many datasets it
  holds/completes, and its throughput.
  Reads `/api/nodes`. **Click any node** (`/node/<label>`) to drill into the
  datasets it holds — each with its seed/leech status, completion and rate, and each
  linking back to that dataset's drill-down. Reads `/api/node/<label>`.

The machine endpoints are namespaced under `/api/` so they never collide with those
client-side page paths; the collector serves the app shell (`index.html`) for any
non-`/api/` GET, so every route deep-links and reloads (with `<base href="/">`
keeping assets resolving from the root).

The UI is a zero-build [Tutuca](https://github.com/marianoguerra/tutuca) SPA served
straight from `webui/` (the framework is vendored as a single `webui/tutuca.js`, so
there's nothing to install and no internet needed at runtime). The endpoints are
built by `swarm_stats` — the same aggregation `piece_map.py` uses — so the browser
and the terminal never disagree, and they fan out to many viewers cheaply: results
are cached for one push interval (`SUMMARY_TTL`) so a crowd shares one recompute per
tick, the heavy detail's piece bitfields are bucketed server-side into the
≤`WEBUI_MAX_COLS` columns the UI actually draws, and each carries an `ETag` so
unchanged state comes back as a `304`.

For the terminal, JSON, or scripting:

```bash
# live JSON from a single node (session + per-torrent status + per-peer stats,
# incl. each peer's discovery source — "tracker", "incoming", ...)
curl -s http://127.0.0.1:8001/stats | python -m json.tool

# the collector's view. Operator endpoints live at the root (live swarm state +
# its own health); the dashboard's data API is namespaced under /api/ so it never
# collides with the SPA's page routes (/, /dataset/<hash>, /transfers, /nodes):
#   /api/overview            one light row per dataset (size, copy counts, live
#                            throughput, per-node held-fraction; NO piece bitfields)
#   /api/torrent/<info_hash> full per-dataset detail, fetched only on drill-down
#   /api/transfers           in-flight transfers (per (node,dataset): progress/rate/ETA)
#   /api/nodes               per-node storage + activity (stored, free disk, held, throughput)
#   /api/node/<label>        one node's held datasets (drill-down from /nodes)
#   /api/summary             the original all-torrents-at-once payload
curl -s http://127.0.0.1:8100/live          | python -m json.tool
curl -s http://127.0.0.1:8100/stats         | python -m json.tool
curl -s http://127.0.0.1:8100/api/overview  | python -m json.tool
curl -s http://127.0.0.1:8100/api/transfers | python -m json.tool
curl -s http://127.0.0.1:8100/api/nodes     | python -m json.tool
curl -s http://127.0.0.1:8100/api/node/0    | python -m json.tool
curl -s "http://127.0.0.1:8100/api/torrent/$(curl -s http://127.0.0.1:8100/api/overview | python -c 'import sys,json;print(json.load(sys.stdin)["datasets"][0]["info_hash"])')" | python -m json.tool

# the tracker's view of the swarm, and the catalog it hosts
curl -s http://127.0.0.1:8000/stats | python -m json.tool
curl -s http://127.0.0.1:8000/catalog | python -m json.tool
curl -sN http://127.0.0.1:8000/catalog/subscribe   # live stream of new torrents (SSE)

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
| `make_torrent.py` | Build v2-only, private, tracker-based `.torrent`s from any files/dirs into the catalog (`data/torrents/`, the tracker's store); generates a two-torrent sample if no args. Also `list_catalog`, which derives `{name, info_hash}` by parsing the `.torrent`s for the tracker to serve `/catalog`. |
| `bittorrent_tracker.py` | The tiny stdlib tracker: `/announce` + `/scrape` + `/stats`, keyed by info-hash (no whitelist), and `/catalog` endpoints that host the torrents (incl. `/catalog/subscribe`, an SSE stream of newly added torrents). |
| `catalog.py` | Stdlib client the node/control use to fetch the catalog (list, `.torrent` bytes) from the tracker over HTTP, and to `subscribe` to newly added torrents. |
| `node.py` | One node daemon (starts empty): libtorrent session (announces to the tracker; fetches torrents from its `/catalog`) + HTTP `/stats` (GET) and `/add` `/remove` (POST) control endpoints; pushes its snapshot to the collector (`--collector`). Has a persisted `node_key`. |
| `control.py` | Operator-local CLI to list the catalog (from the tracker), subscribe to newly added torrents, inspect a local node, and tell nodes to serve/download torrents. |
| `collector.py` | Central push endpoint: keeps the latest snapshot per node in memory (no persistence) and serves `/live` + `/stats` (operator views), the dashboard API under `/api/` (`overview` light per-dataset list, `torrent/<info_hash>` full drill-down detail, `transfers` in-flight transfers, `nodes` per-node storage, `node/<label>` one node's datasets, `swarm` tracker membership reconciled against node reports, `summary` legacy all-torrents), and the web UI from `webui/` (app shell served for any non-`/api/` path). Also polls the tracker's `/stats` in the background to feed `/api/swarm`. |
| `piece_map.py` | Per torrent: which peer holds which pieces/files + how many copies of each file exist (reads the collector's `/live`). |
| `swarm_stats.py` | Shared helpers that group node snapshots by torrent and aggregate per-piece/per-file copy stats (used by `collector.py` for `/api/overview` + `/api/torrent/<info_hash>` + `/api/summary`, and by `piece_map.py`). |
| `webui/` | The browser dashboard: `index.html` + `app.js` (a [Tutuca](https://github.com/marianoguerra/tutuca) SPA, no build step) — History-API-routed screens Overview (`/`, with name search + status filter), per-dataset piece-map drill-down (`/dataset/<info_hash>`), Transfers (`/transfers`), Nodes (`/nodes`) and per-node drill-down (`/node/<label>`), and Swarm (`/swarm`, the tracker's announce-based membership reconciled against what nodes report), reading the `/api/*` endpoints — plus the vendored single-file `tutuca.js` framework. Served by `collector.py`. |

## Ports

- Tracker (`bittorrent_tracker.py`): `8000`
- Collector (`collector.py`): `8100`
- Node *i* BitTorrent: `6881 + i`
- Node *i* control/stats HTTP: `8001 + i`

## Outputs (generated, safe to delete)

- `data/torrents/` — `<name>.torrent` per catalog entry (the tracker's store,
  served via `/catalog`)
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
  `session_stats_metrics()` (net bytes, peer counts, disk, piece picker, …), plus
  `disk` — free/total bytes of the filesystem holding the node's data directory.
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
