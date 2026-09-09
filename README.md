# A zero-configuration cluster, in one program

Run `node.py` on a few machines on the same network. Hand one of them a file,
tell another to keep a copy, and the bytes move on their own.

```bash
python node.py                              # on each machine. The whole setup.
python control.py publish 10.0.0.5 ~/photos # to the node on that machine
python control.py add     10.0.0.6 photos   # ...and this one keeps a copy
```

Every node runs the same program and is self-sufficient: it discovers its peers,
holds what it was given, and serves all of that to anyone who asks. A node
started next week joins with nothing configured and is usable immediately.

A node stores what it was told to store and nothing else. Storing is the one
thing a node cannot undo cheaply — everything else it does costs nothing — so
nothing ever lands on a machine unasked. See
[what a node stores](#what-a-node-stores).

Underneath is plain **BitTorrent v2** — content-addressed, hash-verified, and
very good at getting the same bytes onto many machines at once.

## What a node does

Every node runs the same tick, roughly every two seconds:

| | | |
|---|---|---|
| 1 | **beacon** | multicast "here I am, and here's how to reach me" |
| 2 | **peers** | read everyone else's beacons — a peer's address is the datagram's source |
| 3 | **mesh** | hand every known peer to every torrent still missing data, and let BitTorrent move the bytes |

All three are the same on every node, and none of them is configurable. Nothing
in the tick decides what to store: a node holds what was published to it and what
it was told to take — see [what a node stores](#what-a-node-stores).

A node's peer table is its own, and built only from beacons it heard. Nothing
tells a node who exists, and nothing tells it what to do.

There is no catalog anywhere — not centrally, and not on a node. Publishing
seeds the data in place, so a dataset has a holder from the moment it exists, and
**the set of datasets in the swarm is just the union of what its nodes hold.** A
node knows what it holds and nothing at all about the datasets it doesn't; it
keeps a `.torrent` only for what it holds. So nothing a node stores grows with
the swarm's catalog, only with its own disk — and "which datasets exist" is a
question for something looking at every node (`control.py list`, the dashboard),
not for any one of them.

## Getting data in

One operation, on any node:

```bash
python control.py publish <node> <path>
```

That node hashes the path — resolved on its own filesystem, not on the machine
you typed the command from — into a v2 torrent and **seeds it in place**:
nothing is copied, the data stays where it is. Publishing *is* starting to hold
it, which is what makes a dataset exist — from that moment it is in the swarm's
catalog, because that catalog is the union of what the nodes hold. Any node can
publish.

Publishing does not put the data on other nodes; that is the next section.

## What a node stores

Only what you ask it to. A node joins the swarm completely — it hears every
beacon, answers for everything it has, and serves it to any peer that asks — but
it holds no data until you name a dataset, and it keeps nothing at all about the
datasets it doesn't hold. Everything a node does *except* storing costs nothing;
storing is the one thing it cannot undo cheaply, so it isn't done unasked.

```bash
python node.py                    # joins the swarm, stores nothing
```

Such a node holds nothing. `list` is read *through* it rather than *from* it —
every dataset in the swarm, with the last column reporting what this particular
node has:

```
$ python control.py list 127.0.0.1:8002
name                     v2 info-hash       copies  on this node
documents                78c6f7e55ebbe684        1  -
media                    66b676791b1a9e20        1  -

$ python control.py status 127.0.0.1:8002
127.0.0.1:8002: holding nothing yet
```

(Datasets appear there once *some* node has published them — see
[getting data in](#getting-data-in). A swarm where nobody has published anything
lists nothing, because there is nothing anywhere for it to list.)

### Taking a dataset

```bash
python control.py add 127.0.0.1:8002 media                # by name...
python control.py add 127.0.0.1:8002 66b676791b1a9e20     # ...or by info-hash
```

An info-hash can be given in full or shortened to any unique leading portion, so
the 16-character forms `list` prints work as-is. Use one when a name is
ambiguous.

The name is resolved across the swarm before the node is asked — a node has no
catalog to look one up in — so what it receives is an info-hash. It fetches the
`.torrent` from a peer that holds the dataset, then downloads exactly that
dataset and nothing else, into `nodes/<id>/data/<slug>/`, pulling from every peer
that already holds it. Nothing else about the node changes: it keeps discovering
peers and serving what it has, and takes nothing else.

Which nodes hold what is entirely up to you, and nothing has to agree: one
dataset can live on every node and another on two. Nothing
keeps track of that for you: `control.py status <node>` is the per-node answer,
and `control.py map <node>` is the swarm-wide one — every dataset with its copy
count, rarest first. Name a dataset as well and you get its piece map and how
many copies of each *file* exist, which is the number that matters when you
place datasets by hand.

### Dropping a dataset

```bash
python control.py remove 127.0.0.1:8002 media
```

Four things happen, and the last two are the ones to know about:

1. The node stops serving the dataset's data immediately.
2. Its fast-resume file and its `.torrent` are deleted, so a restart won't bring
   it back and the node stops answering for a dataset it no longer has.
3. **The downloaded files stay on disk.** `remove` frees no space by itself —
   delete `nodes/<id>/data/<slug>/` yourself if that is what you were after.
4. **If that was the last copy, the dataset has left the swarm.** A dataset
   exists because someone holds it, so nothing keeps a record of one nobody
   holds. This is how a dataset is retracted; there is no other way, and no
   confirmation.

One upshot of (3) is that re-adding the dataset later costs nothing: libtorrent
rechecks the files already sitting there and comes back complete without pulling
a byte over the network. Another is that (4) is recoverable — re-publishing the
same path reproduces the same dataset, byte for byte and hash for hash, because
the dataset *is* its content.

## Try it on one machine

Several nodes on one host work fine — they share the beacon port and are told
apart by their other ports.

```bash
python make_torrent.py                          # generate some sample content
python node.py --id 0 &
python node.py --id 1 &
python node.py --id 2 &

python control.py peers 127.0.0.1:8001          # they already found each other
python control.py publish 127.0.0.1:8001 data/sample/media

python control.py list   127.0.0.1:8002         # the catalog, read through node 1...
python control.py add    127.0.0.1:8002 media   # ...and node 1 keeps a copy
python control.py status 127.0.0.1:8002         # arriving
python control.py status 127.0.0.1:8003         # node 2: holding nothing yet
```

Transfers are capped at `UPLOAD_RATE_LIMIT` (1 MiB/s) per node so replication is
slow enough to watch: the 12 MiB sample lands in about 20 seconds from a single
seeder, and quicker once more nodes hold it.

Things worth trying from there:

- **Drop it again.** `python control.py remove 127.0.0.1:8002 media`, then look at
  `status`, at `list`, and at `nodes/1/data/` — see
  [dropping a dataset](#dropping-a-dataset).
- **Start a fourth node.** It joins with nothing configured, and `list` read
  through it shows the whole swarm — including everything published before it
  existed.
- **Publish from a different node.** Every node is equal.
- **Kill a node and restart it.** It resumes with progress intact and re-meshes.
- **Publish two different directories with the same name.** They coexist; see
  *Identity* below.

## Commands

```bash
python control.py publish <node> <path>      # put local data into the swarm
python control.py list    <node>             # datasets a node knows of
python control.py peers   <node>             # nodes a node can see  <- start here when debugging
python control.py status  <node>             # datasets a node actually holds
python control.py add     <node> <dataset>   # tell it to store this one
python control.py remove  <node> <dataset>   # tell it to stop holding it
python control.py map     <node>             # copies of every dataset, rarest first
python control.py map     <node> <dataset>   # ...and that one's pieces, per node
```

Nodes are addressed by their HTTP endpoint, `host[:port]`; the port defaults to
8001, so `127.0.0.1` and `127.0.0.1:8001` name the same node. A dataset argument
is a name or an info-hash, interchangeably. Any node will do for `list` and
`map`, but they are read *through* it and not *from* it: no node has a catalog,
so both ask it who else exists and union what everyone holds. `status` is per
node by definition.

## Identity: hashes underneath, names on top

A dataset **is** its BitTorrent v2 info-hash. Names are labels, so two people can
publish different content called `media` and both simply coexist as separate
datasets. On disk each gets a readable slug, `<name>_<first 8 hex of hash>`:

```
nodes/0/catalog/media_3005dbcc.torrent    # the torrent, for a dataset it holds
nodes/0/data/media_3005dbcc/media/…       # the copy it downloaded
nodes/0/.resume/media_3005dbcc.resume     # libtorrent fast-resume
```

All three exist for exactly the datasets this node holds, and are deleted
together when it drops one.

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

The beacon is a ~80-byte UDP datagram to `239.255.42.1:6772` (override with
`SWARM_BEACON_GROUP` / `SWARM_BEACON_PORT`), TTL 1, so it stays on the local
segment. It carries a node's identity and its ports, and nothing about what it
holds — that is an HTTP question, and one for whoever is reading the whole swarm
rather than for a datagram sent every tick. A node never states its own address —
the receiver takes it from the packet — so there is nothing to configure on a
multi-homed or NAT'd host. The optional dashboard joins the same group to find a
node to read through, and only ever listens: it discovers the swarm without the
swarm discovering it.

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
GET  /catalog/<info_hash>            one dataset's file -> piece map (holders only)
GET  /catalog/<info_hash>.torrent    the raw .torrent (holders only)
GET  /peers                          {"self": …, "peers": […]} — its view of the swarm
POST /publish  {"path": …}           hash a local path in and seed it
POST /add      {"info_hash": …}      take a dataset from whoever has it
POST /remove   {"name"|"info_hash"}  drop one
```

There is no endpoint for "what datasets exist", because no node knows. That is
the deliberate half of this: publishing seeds the data in place, so a dataset has
a holder from the instant it exists, and the union of every node's `/holdings`
**is** the catalog. A node keeps a `.torrent` only for what it holds (~14 KB
against ~100 MiB of data, so it costs nothing next to holding the data at all),
and nothing whatsoever about a dataset it doesn't hold. Nothing a node stores
grows with the swarm — only with its own disk.

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
  forever. A row is `{info_hash, state, name, total_size, piece_length}`, where
  `state` is `downloading`, `complete`, or — only ever in a delta — `gone`, the
  tombstone that tells a reader a dataset was dropped rather than merely not
  mentioned. The three immutable fields are there so that unioning these streams
  lists the datasets *and* counts their copies in one pass, with no per-dataset
  lookup; they are exempt from the rule above because they never change, which
  was the whole reason to keep rates out.
- **`/transfers` holds every per-second number.** Progress and rates are kept out
  of `/holdings` precisely because they would churn the stream continuously.
  It is bounded by what is in flight, never by what is stored.
- **`/holdings/<info_hash>` is the only piece bitfield**, and it is one dataset at
  a time. The swarm-wide piece map costs one request per node holding *that*
  dataset, however much the swarm holds.

Counting copies needs none of the piece detail: a node holding a dataset
`complete` holds every file in it by definition, so **copies = holders in state
`complete`**, straight off the holdings stream. The piece-level view refines that
into per-file and rarest-piece figures when you ask for one dataset.

`/catalog/<info_hash>` carries the one thing a row can't: the file → piece map,
which is the large part and is needed only for per-file copy counts. It is fixed
for the dataset's lifetime — it is what the info-hash hashes — so a reader fetches
it once and keeps it, from any node holding the dataset. Which is the only kind
of node that has it, and, since holding is what makes a dataset exist, the only
kind there is.

## Files

| File | Role |
|---|---|
| `node.py` | **The system.** libtorrent session + the sync tick (beacon, peers, mesh) + the HTTP API. Run one per machine. |
| `control.py` | CLI to talk to a node: publish, list, peers, status, add, remove, map. |
| `make_torrent.py` | Builds v2-only, private, trackerless torrents (`build()`). As a script, generates the sample content. |
| `catalog.py` | Stdlib client for another node's HTTP API: `fetch_stats`/`fetch_holdings` (cursor-following, raises `Resync`)/`fetch_transfers`/`fetch_holding`/`fetch_meta`, plus `fetch_swarm()` — every node's `/stats`, gathered through one node's peer table. Named for the thing it assembles rather than fetches: no node has a catalog. No libtorrent, so `control.py` doesn't need it. |
| `beacon.py` | The discovery datagram: join the group, send, drain. No libtorrent either, which is how the dashboard finds its way in without running a node. |
| `config.py` | Ports, beacon group, timing, paths. |
| `swarm_stats.py` | The catalog and the copy-count arithmetic: `catalog_from()` unions the holdings streams, `overview_row()` scores a dataset from holdings alone, `holder_rows()`/`per_file()` work from piece bitfields. Shared by `control.py` and `collector.py`. |
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

- `nodes/<id>/catalog/` — the .torrent files for the datasets it holds
- `nodes/<id>/data/` — datasets it downloaded (published ones stay in place)
- `nodes/<id>/.resume/` — libtorrent fast-resume, so a restart doesn't re-download
- `nodes/<id>/node_key` — its persisted identity
- `data/sample/` — the built-in sample content

## Limitations

- **A dataset is only as durable as its holders.** There is no catalog, so a
  dataset nobody holds is not merely unavailable — it is gone, along with any
  record that it existed. Dropping the last copy retracts it silently, and a node
  that is *down* is indistinguishable, in a single fan-out, from one that never
  had the data. Watching copy counts over time is the answer to both, and that is
  the dashboard's job rather than a node's.
- **`remove` doesn't delete the files.** It stops serving a dataset and forgets
  it across restarts, but the downloaded copy stays in `nodes/<id>/data/`.
- **Nothing weighs a dataset against free space.** `add` never refuses, however
  little room is left. Placing datasets is manual, and no node warns you that a
  copy count has dropped to one.
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
