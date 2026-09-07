# A self-replicating cluster, in one program

Run `node.py` on a few machines on the same network. Hand one of them a file.
Every machine has it.

```bash
python node.py --replicate all      # on each machine. That's the whole setup.
python control.py publish 127.0.0.1:8001 ~/photos    # to the node on this machine
```

Every node runs the same program and is self-sufficient: it discovers its peers,
learns what datasets exist, decides what to store, and serves all of that to
anyone who asks. A node started next week joins and catches up on its own.

`--replicate all` is what makes each node mirror everything, and it is not the
default. Left off, a node still joins and still tracks every dataset — it just
stores nothing until you name one, which is what you want as soon as a disk has
a size. That is the only policy in the system, and all of it is in
[what a node stores](#what-a-node-stores).

Underneath is plain **BitTorrent v2** — content-addressed, hash-verified, and
very good at getting the same bytes onto many machines at once.

## What a node does

Every node runs the same tick, roughly every two seconds:

| | | |
|---|---|---|
| 1 | **beacon** | multicast "here I am, and here's a fingerprint of my catalog" |
| 2 | **peers** | read everyone else's beacons — a peer's address is the datagram's source |
| 3 | **catalog** | for any peer whose fingerprint changed, pull its dataset list and fetch what's new |
| 4 | **want()** | for each dataset we know of but don't hold — do we want it? |
| 5 | **mesh** | hand every known peer to every torrent still missing data, and let BitTorrent move the bytes |

Steps 1, 2, 3 and 5 are the same on every node and are not configurable. Step 4
is the only policy — see [what a node stores](#what-a-node-stores).

A node's peer table, its catalog and its storage decisions are all its own. Two
nodes converge because they see the same beacons, not because anything tells them
to.

## Getting data in

One operation, whatever the node's storage policy:

```bash
python control.py publish <node> <path>
```

That node hashes the path — resolved on its own filesystem, not on the machine
you typed the command from — into a v2 torrent, adds it to its catalog, and
**seeds it in place**: nothing is copied, the data stays where it is. Its beacon
fingerprint changes, peers notice within a tick, and they pull the `.torrent`.
Any node can publish.

Publishing puts a dataset in every node's *catalog*. It does not put the data on
them; that is the next section.

## What a node stores

By default, only what you ask it to. A node with no `--replicate` flag joins the
swarm completely — it hears every beacon, tracks the full catalog, and serves that
catalog and its `.torrent` files to any peer that asks — but it holds no data
until you name a dataset. Everything a node does *except* storing costs nothing;
storing is the one thing it cannot undo cheaply, so it isn't done unasked.

```bash
python node.py                    # joins, tracks everything, stores nothing
python node.py --replicate manual # the same thing, said out loud
```

Such a node knows about every dataset and holds none of them. `list` shows the
whole catalog, with the last column reporting what this particular node has:

```
$ python control.py list 127.0.0.1:8002
name                     v2 info-hash       on this node
documents                78c6f7e55ebbe684   -
media                    66b676791b1a9e20   -

$ python control.py status 127.0.0.1:8002
127.0.0.1:8002: holding nothing yet
```

(Datasets appear there once *some* node has published them — see
[getting data in](#getting-data-in). A swarm where nobody has published anything
has an empty catalog on every node.)

### Taking a dataset

```bash
python control.py add 127.0.0.1:8002 media                # by name...
python control.py add 127.0.0.1:8002 66b676791b1a9e20     # ...or by info-hash
```

An info-hash can be given in full or shortened to any unique leading portion, so
the 16-character forms `list` prints work as-is. Use one when a name is
ambiguous.

The node downloads exactly that dataset and nothing else, into
`nodes/<id>/data/<slug>/`, pulling from every peer that already holds it. Nothing
else about the node changes: it keeps discovering peers, keeps tracking new
datasets as they appear, and keeps ignoring them.

Which nodes hold what is entirely up to you, and nothing has to agree: one
dataset can live on every node, another on two, another on none at all. Nothing
keeps track of that for you: `control.py status <node>` is the per-node answer,
and `control.py map <node>` is the swarm-wide one — every dataset with its copy
count, rarest first. Name a dataset as well and you get its piece map and how
many copies of each *file* exist, which is the number that matters when you
place datasets by hand.

### Dropping a dataset

```bash
python control.py remove 127.0.0.1:8002 media
```

Three things happen, and the third is the one to know about:

1. The node stops serving the dataset's data immediately.
2. Its fast-resume file is deleted, so a restart won't bring it back.
3. **The downloaded files stay on disk.** `remove` frees no space by itself —
   delete `nodes/<id>/data/<slug>/` yourself if that is what you were after.

One upshot of (3) is that re-adding the dataset later costs nothing: libtorrent
rechecks the files already sitting there and comes back complete without pulling
a byte over the network.

The dataset also stays in the node's catalog. It still knows the dataset exists
and still serves its `.torrent` to peers — it just doesn't keep a copy of the
data.

### Mirroring everything

```bash
python node.py --replicate all
```

`want()` becomes unconditionally true — the mode from the example at the top. On
such a node `add` is redundant and `remove` is futile: the dataset is taken again
on the next tick. Mirrors and hand-picked nodes share a swarm with no special
handling.

## Try it on one machine

Several nodes on one host work fine — they share the beacon port and are told
apart by their other ports. These run without `--replicate all`, so you can watch
the storage decision being made rather than have it made for you.

```bash
python make_torrent.py                          # generate some sample content
python node.py --id 0 &
python node.py --id 1 &
python node.py --id 2 &

python control.py peers 127.0.0.1:8001          # they already found each other
python control.py publish 127.0.0.1:8001 data/sample/media

python control.py list   127.0.0.1:8002         # node 1 knows about it...
python control.py add    127.0.0.1:8002 media   # ...and now keeps a copy
python control.py status 127.0.0.1:8002         # arriving
python control.py status 127.0.0.1:8003         # node 2: holding nothing yet
```

Transfers are capped at `UPLOAD_RATE_LIMIT` (1 MiB/s) per node so replication is
slow enough to watch: the 12 MiB sample lands in about 20 seconds from a single
seeder, and quicker once more nodes hold it.

Things worth trying from there:

- **Mirror instead of choosing.** Restart node 2 as `python node.py --id 2
  --replicate all` — it takes `media` a few seconds later without being asked,
  and everything published afterwards too.
- **Drop it again.** `python control.py remove 127.0.0.1:8002 media`, then look at
  `status`, at `list`, and at `nodes/1/data/` — see
  [dropping a dataset](#dropping-a-dataset).
- **Start a fourth node.** It joins and sees the same catalog with nothing
  configured.
- **Publish from a different node.** Every node is equal.
- **Kill a node and restart it.** It resumes with progress intact and re-meshes.
- **Publish two different directories with the same name.** They coexist; see
  *Identity* below.

## Commands

```bash
python control.py publish <node> <path>      # put local data into the swarm
python control.py list    [node]             # datasets a node knows of
python control.py peers   [node]             # nodes a node can see  <- start here when debugging
python control.py status  [node]             # datasets a node actually holds
python control.py add     <node> <dataset>   # tell it to store this one
python control.py remove  <node> <dataset>   # tell it to stop holding it
python control.py map     <node>             # copies of every dataset, rarest first
python control.py map     <node> <dataset>   # ...and that one's pieces, per node
```

Nodes are addressed by their HTTP endpoint, `host[:port]`; the port defaults to
8001, so `127.0.0.1` and `127.0.0.1:8001` name the same node. A dataset argument
is a name or an info-hash, interchangeably. Any node will do for `list` — they
converge on the same catalog. `status` is per node by definition.

## Identity: hashes underneath, names on top

A dataset **is** its BitTorrent v2 info-hash. Names are labels, so two people can
publish different content called `media` and both simply coexist as separate
datasets. On disk each gets a readable slug, `<name>_<first 8 hex of hash>`:

```
nodes/0/catalog/media_3005dbcc.torrent    # what this node knows exists
nodes/0/data/media_3005dbcc/media/…       # a copy it downloaded
nodes/0/.resume/media_3005dbcc.resume     # libtorrent fast-resume
```

The other `media` sits beside it as `media_e3603877`, in the same three places.

Every wire protocol uses the full info-hash, never the filename. Where a name is
ambiguous, commands say so and ask for a hash rather than guessing:

```
$ python control.py add 127.0.0.1:8003 media
127.0.0.1:8003: 'media' is an ambiguous name - 2 datasets match; use one of these info-hashes: 3005dbccd2cc7111, e3603877cb88bd7d

$ python control.py add 127.0.0.1:8003 3005dbccd2cc7111
```

Anywhere a dataset is named you can use its info-hash instead, in full or
shortened to any unique leading portion — the forms printed above and by `list`
are already usable.

## Discovery

The beacon is a ~100-byte UDP datagram to `239.255.42.1:6772` (override with
`SWARM_BEACON_GROUP` / `SWARM_BEACON_PORT`), TTL 1, so it stays on the local
segment. It carries a node's identity, its ports and its catalog fingerprint. A
node never states its own address — the receiver takes it from the packet — so
there is nothing to configure on a multi-homed or NAT'd host. The optional
dashboard joins the same group to find a node to read through, and only ever
listens: it discovers the swarm without the swarm discovering it.

The beacon is the only source of peers. Torrents are built private and
trackerless — the private flag is what keeps libtorrent from using PEX — and DHT,
LSD, UPnP and NAT-PMP are switched off at the session level as well, so a node
talks only to peers whose beacon it heard. `control.py peers` shows you exactly
what a node believes, which is where to look when something isn't replicating.

The flip side is that multicast has to reach between your nodes: same segment,
TTL 1. A node that cannot hear the others does not join.

## The optional dashboard

The swarm replicates with this switched off; run it to *see* what's happening.

```bash
python collector.py    # then open http://127.0.0.1:8100/
```

No address, because it finds a node the way nodes find each other: it listens to
the beacon and reads the swarm through whoever answers. That node is a way in,
not a destination — it asks it who else exists and reads every node itself — so any node will do, and if that one goes away it picks up another by
itself. Nothing is configured on either side: a node has no dashboard setting,
and since the dashboard only ever listens and never beacons back, no node learns
it exists or can tell whether anyone is watching. It shows an overview sorted
rarest-copies-first, a per-dataset piece map, in-flight transfers, and per-node
storage.

Where multicast doesn't reach the machine you are watching from, name any node
instead. The dashboard is a plain HTTP client, so this works from anywhere that
can reach a node, even where the beacon can't:

```bash
python collector.py 127.0.0.1:8001
```

Nodes appear under the address the dashboard reached them at, so what you read
there is what you can paste into `control.py`. A node is never labelled by
itself: it doesn't know its own address, which is the point of the beacon.

The nodes are read at most once a second no matter how many browsers are open,
and not at all while none is.

The same data in the terminal, the same way:

```bash
python control.py map 127.0.0.1:8001          # how many copies of everything exist
python control.py map 127.0.0.1:8001 media   # who has which pieces of one dataset
watch -n 2 python control.py map 127.0.0.1:8001
curl -s http://127.0.0.1:8001/stats | python -m json.tool     # one node, directly
```

## A node's HTTP API

```
GET  /stats                          what the node is: disk, counts, rates, cursor
GET  /holdings[?since=<cursor>]      which datasets it holds — all, or just what changed
GET  /holdings/<info_hash>           one dataset here, with its piece bitfield
GET  /transfers                      what is moving right now: progress and rates
GET  /catalog                        [{"name","info_hash"}] — every dataset it knows of
GET  /catalog/<info_hash>            one dataset's shape: size, pieces, files
GET  /catalog/<info_hash>.torrent    the raw .torrent
GET  /peers                          {"self": …, "peers": […]} — its view of the swarm
POST /publish  {"path": …}           hash a local path in and seed it
POST /add      {"name"|"info_hash"}  take a known dataset
POST /remove   {"name"|"info_hash"}  drop one
```

The split across the first four is the one thing here that decides whether
watching a swarm stays affordable, so it is worth saying why it is drawn where it
is. A node holds far more datasets than it is moving at any moment, and *which*
it holds changes only when one is taken, finishes, or is dropped. So:

- **`/stats` is constant size.** Nothing in it is per dataset. It carries a
  **cursor** — the node's position in its own stream of holding changes.
- **`/holdings` is read by that cursor.** Hand back the one you were given and
  you are told only what has changed since; a settled swarm therefore does no
  holdings traffic at all. The cursor is opaque: store it, return it, never take
  it apart. That is what lets the *node* say "I can't answer from that one"
  (HTTP 409, after a restart) instead of silently returning an empty delta
  forever. Rows are just `{info_hash, state}`, where `state` is `downloading`,
  `complete`, or — only ever in a delta — `gone`, the tombstone that tells a
  reader a dataset was dropped rather than merely not mentioned.
- **`/transfers` holds every per-second number.** Progress and rates are kept out
  of `/holdings` precisely because they would churn the stream continuously.
  It is bounded by what is in flight, never by what is stored.
- **`/holdings/<info_hash>` is the only piece bitfield**, and it is one dataset at
  a time. The swarm-wide piece map costs one request per node holding *that*
  dataset, whatever else is in the catalog.

Counting copies needs none of the piece detail: a node holding a dataset
`complete` holds every file in it by definition, so **copies = holders in state
`complete`**, straight off the holdings stream. The piece-level view refines that
into per-file and rarest-piece figures when you ask for one dataset.

`/catalog/<info_hash>` is there for the same reason: a dataset's size, piece
layout and file list are identical on every node and fixed for its lifetime (they
are what the info-hash hashes), so they belong to the catalog and are fetched
once — not shipped with every node's status, one identical copy per node per
poll.

## Files

| File | Role |
|---|---|
| `node.py` | **The system.** libtorrent session + the sync tick (beacon, peers, catalog, want, mesh) + the HTTP API. Run one per machine. |
| `control.py` | CLI to talk to a node: publish, list, peers, status, add, remove, map. |
| `make_torrent.py` | Builds v2-only, private, trackerless torrents (`build()`), reads a catalog directory (`list_catalog()`). As a script, generates the sample content. |
| `catalog.py` | Stdlib client for another node's HTTP API: `fetch_stats`/`fetch_holdings` (cursor-following, raises `Resync`)/`fetch_transfers`/`fetch_holding`, plus `fetch_swarm()` — every node's `/stats`, gathered through one node's peer table. No libtorrent, so `control.py` doesn't need it. |
| `beacon.py` | The discovery datagram: join the group, send, drain. No libtorrent either, which is how the dashboard finds its way in without running a node. |
| `config.py` | Ports, beacon group, timing, paths. |
| `swarm_stats.py` | The copy-count arithmetic, at both levels: `overview_row()` from holdings alone, `holder_rows()`/`per_file()` from piece bitfields. Shared by `control.py map` and `collector.py`. |
| `collector.py` | *Optional.* Finds a node on the beacon and reads the swarm through it. Keeps one cursor per node so a refresh costs the changes rather than the whole world; serves the `/api/*` dashboard endpoints and the web UI. |
| `webui/` | *Optional.* Zero-build [Tutuca](https://github.com/marianoguerra/tutuca) SPA, framework vendored as one file. Served by `collector.py`. |

Python standard library only, plus the `libtorrent` binding (tested with
libtorrent 2.0.13 / Python 3.13). No third-party packages, nothing fetched at
runtime.

## Ports

| | |
|---|---|
| Beacon (multicast; every node sends, the dashboard only listens) | `239.255.42.1:6772` |
| Node *i* BitTorrent | `6881 + i` |
| Node *i* HTTP API | `8001 + i` |
| Dashboard (optional) | `8100` |

`--id` matters only when several nodes share a host: it picks `nodes/<id>/` and
offsets the ports. One node per machine is the normal case, and `--id` defaults
to 0. A node's real identity is a persisted UUID in `nodes/<id>/node_key`.

## Generated files (safe to delete)

- `nodes/<id>/catalog/` — the .torrent files this node knows about
- `nodes/<id>/data/` — datasets it downloaded (published ones stay in place)
- `nodes/<id>/.resume/` — libtorrent fast-resume, so a restart doesn't re-download
- `nodes/<id>/node_key` — its persisted identity
- `data/sample/` — the built-in sample content

## Limitations

- **The catalog only grows.** A node never drops a dataset on its own, and there
  is no way to retract one once published — it stays in every catalog, and
  `remove` on one node doesn't affect the others.
- **`remove` doesn't delete the files.** It stops serving a dataset and forgets
  it across restarts, but the downloaded copy stays in `nodes/<id>/data/`.
- **Nothing weighs a dataset against free space.** `add` never refuses, and
  `--replicate all` will fill a small disk. Placing datasets is manual, and no
  node warns you that a copy count has dropped to one.
- **Datasets are immutable.** v2 torrents are fixed at publish time. Changing the
  content means publishing it again, which produces a separate dataset.
- **Everything on the segment is trusted.** Any node can publish anything and is
  believed, and a beacon can claim to be any node — nothing is signed or
  authenticated. This suits a private network, which is the assumed environment.
- **Discovery needs working multicast** between nodes: a node that cannot hear
  the beacon cannot join, and there is no address to bootstrap from. (The
  dashboard is the exception — it can be handed a node instead.)
- **A transfer pulls from everyone at once.** A node offers all its known peers
  to every dataset it is still missing, which is deterministic and fast at
  cluster scale but grows as peers x transfers in flight. Nothing is offered for
  a dataset it already holds: in BitTorrent the side that wants the bytes opens
  the connection, so a settled swarm holds no peer connections at all.
