# A zero-configuration cluster, in one program

Run `node.py` on a few machines on the same network. Hand one of them a file,
tell another to keep a copy, and the bytes move on their own.

```bash
python node.py                              # on each machine. The whole setup.
python control.py publish 10.0.0.5 ~/photos # to the node on that machine
python control.py add     10.0.0.6 photos   # ...and this one keeps a copy
```

Every node runs the same program: it finds its peers by multicast beacon, holds
what it was given, and serves that to anyone who asks. Nothing is configured and
nothing is central. A node holds what was published to it and what it was told
to take, and nothing else. Underneath is plain **BitTorrent v2** —
content-addressed and hash-verified.

## What a node does

Every node runs the same tick, roughly every two seconds:

1. **beacon** — multicast "here I am, and here's how to reach me".
2. **peers** — read everyone else's beacons; a peer's address is the datagram's
   source.
3. **mesh** — hand every known peer to every torrent still missing data, and let
   BitTorrent move the bytes.

None of it is configurable, and nothing in it decides what to store. A node's
peer table is its own, built only from beacons it heard.

No node has a list of every dataset, and there is no central one either. A node
keeps a `.torrent` for each dataset it holds and nothing at all about the ones
it doesn't, so the set of datasets in the swarm is the union of what its nodes
hold. "Which datasets exist" is a question for something reading every node —
`control.py list`, or the dashboard.

## Getting data in

```bash
python control.py publish <node> <path>
```

The path is resolved on that node's own filesystem, not on the machine you typed
the command from. The node hashes it into a v2 torrent and **seeds it in
place**: nothing is copied, the data stays where it is. Any node can publish,
and publishing puts the data on no other node.

## Taking a dataset

```bash
python control.py add 127.0.0.1:8002 media                # by name...
python control.py add 127.0.0.1:8002 66b676791b1a9e20     # ...or by info-hash
```

The name is resolved across the swarm first, so what the node receives is an
info-hash. It fetches the `.torrent` from a peer that holds the dataset, then
downloads that dataset and nothing else into `nodes/<id>/data/<slug>/`, pulling
from every peer that has it. Nothing else about the node changes.

A node that has been told nothing holds nothing. `list` is read *through* a node
rather than from it — every dataset in the swarm, with the last column saying
what this one has of each:

```
$ python control.py list 127.0.0.1:8002
name                     v2 info-hash       copies  on this node
media                    66b676791b1a9e20        1  -
documents                78c6f7e55ebbe684        1  -

$ python control.py status 127.0.0.1:8002
127.0.0.1:8002: holding nothing yet
```

Which nodes hold what is up to you, and nothing keeps track of it for you.
`control.py status <node>` is the per-node answer and `control.py map <node>`
the swarm-wide one — the least-replicated datasets with their copy counts,
`--all` for every one. Name a dataset as well for its piece map and the number
of copies of each *file*.

`list` prints as the nodes' streams merge, in info-hash order: sorting by name
would mean holding every dataset in the swarm before printing the first line.

## Dropping a dataset

```bash
python control.py remove 127.0.0.1:8002 media
```

1. The node stops serving the dataset immediately.
2. Its fast-resume file and its `.torrent` are deleted, so a restart won't bring
   it back and the node stops answering for it.
3. **The downloaded files stay on disk.** `remove` frees no space — delete
   `nodes/<id>/data/<slug>/` yourself. Re-adding the dataset later costs no
   network traffic: libtorrent rechecks the files sitting there and comes back
   complete.
4. **If that was the last copy, the dataset has left the swarm**, along with any
   record that it existed. There is no confirmation. Re-publishing the same path
   brings it back byte for byte and hash for hash.

## Try it on one machine

Several nodes on one host share the beacon port and are told apart by their
other ports.

```bash
python make_torrent.py                          # generate some sample content
python node.py --id 0 &
python node.py --id 1 &
python node.py --id 2 &

python control.py peers 127.0.0.1:8001          # they already found each other
python control.py publish 127.0.0.1:8001 data/sample/media

python control.py list   127.0.0.1:8002         # the swarm, read through node 1...
python control.py add    127.0.0.1:8002 media   # ...and node 1 keeps a copy
python control.py status 127.0.0.1:8002         # arriving
python control.py status 127.0.0.1:8003         # node 2: holding nothing yet
```

Transfers are capped at `UPLOAD_RATE_LIMIT` (1 MiB/s) per node so replication is
slow enough to watch: the 12 MiB sample lands in about 20 seconds from a single
seeder, quicker once more nodes hold it.

From there: drop it again and look at `status`, `list` and `nodes/1/data/`;
start a fourth node and read `list` through it, which shows everything published
before it existed; publish from a different node, since every node is equal;
kill a node and restart it, and it resumes with progress intact; publish two
different directories under the same name, and they coexist.

## Commands

```bash
python control.py publish <node> <path>      # put local data into the swarm
python control.py list    <node>             # every dataset, in info-hash order
python control.py peers   <node>             # nodes a node can see  <- start here when debugging
python control.py status  <node>             # datasets a node actually holds (--all)
python control.py add     <node> <dataset>   # tell it to store this one
python control.py remove  <node> <dataset>   # tell it to stop holding it
python control.py map     <node>             # the least-replicated (--all, --top N)
python control.py map     <node> <dataset>   # ...and that one's pieces, per node
```

Every command names a node. Nodes are addressed by their HTTP endpoint,
`host[:port]`; the port defaults to 8001, so `127.0.0.1` and `127.0.0.1:8001`
name the same node. A dataset argument is a name or an info-hash,
interchangeably. Any node will do for `list` and `map`: they ask it who else
exists and union what everyone holds. `status` is per node.

## Names and hashes

A dataset **is** its BitTorrent v2 info-hash. Names are labels, so two different
directories published as `media` coexist as separate datasets. On disk each gets
a readable slug, `<name>_<first 8 hex of hash>`:

```
nodes/0/torrents/30/media_3005dbcc.torrent   # the torrent, for a dataset it holds
nodes/0/data/30/media_3005dbcc/media/…       # the copy it downloaded
nodes/0/.resume/30/media_3005dbcc.resume     # libtorrent fast-resume
```

The `30/` is the first byte of the info-hash: a node holding a million datasets
spreads them over 256 directories rather than filling one.

All three exist for exactly the datasets this node holds, and are deleted
together when it drops one. The other `media` sits beside it as
`media_e3603877`.

Every wire protocol uses the full info-hash, never the filename. Where a name is
ambiguous, commands say so and ask for a hash rather than guessing:

```
$ python control.py add 127.0.0.1:8003 media
127.0.0.1:8003: 'media' is an ambiguous name - 2 datasets match; use one of these info-hashes: 3005dbccd2cc7111, e3603877cb88bd7d

$ python control.py add 127.0.0.1:8003 3005dbccd2cc7111
```

Anywhere a dataset is named, an info-hash works too, in full or shortened to any
unique leading portion — the 16-character forms printed above and by `list` are
already usable.

## Discovery

The beacon is a ~80-byte UDP datagram to `239.255.42.1:6772` (override with
`SWARM_BEACON_GROUP` / `SWARM_BEACON_PORT`), TTL 1, so it stays on the local
segment. It carries a node's identity and its ports, and nothing about what it
holds. A node never states its own address — the receiver takes it from the
packet — so there is nothing to configure on a multi-homed or NAT'd host.

The beacon is the only source of peers. Torrents are private and trackerless
(the private flag is what keeps libtorrent from using PEX), and DHT, LSD, UPnP
and NAT-PMP are off at the session level too, so a node talks only to peers
whose beacon it heard. `control.py peers` shows what a node believes, which is
where to look when something isn't replicating.

Multicast has to reach between your nodes: same segment, TTL 1. A node that
cannot hear the others does not join.

## The optional dashboard

The swarm replicates with this switched off; run it to *see* what's happening.

```bash
python collector.py    # then open http://127.0.0.1:8100/
```

No address: it listens to the beacon, reads the swarm through whichever node
answers, and picks up another if that one goes away. It never beacons, so no
node knows it exists. It shows an overview sorted rarest-copies-first, a
per-dataset piece map, in-flight transfers and per-node storage, and reads the
nodes at most once a second however many browsers are open — not at all while
none is.

Where multicast doesn't reach the machine you are watching from, name any node
instead; the dashboard is a plain HTTP client:

```bash
python collector.py 127.0.0.1:8001
```

Nodes appear under the address the dashboard reached them at, so what you read
there is what you can paste into `control.py`.

The same data in the terminal:

```bash
python control.py map 127.0.0.1:8001         # how many copies of everything exist
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
GET  /dataset/<info_hash>            the dataset: name, size, file -> piece map (holders only)
GET  /dataset/<info_hash>.torrent    the same, as the raw .torrent (holders only)
GET  /peers                          {"self": …, "peers": […]} — its view of the swarm
POST /publish  {"path": …}           hash a local path in and seed it
POST /add      {"info_hash": …}      take a dataset from whoever has it
POST /remove   {"name"|"info_hash"}  drop one
```

There is no endpoint for "what datasets exist", because no node knows. The union
of every node's `/holdings` **is** the answer.

`/stats` is constant size — nothing in it is per dataset — and carries a
**cursor**: the node's position in its own stream of holding changes. Hand that
cursor back to `/holdings` and you are told only what changed since, so a
settled swarm does no holdings traffic at all. The cursor is opaque: store it,
return it, never take it apart. A node that cannot answer from one, after a
restart, says so with HTTP 409 rather than an empty delta.

A reader with no cursor gets the held set in pages: `more` says whether to ask
again with the cursor just handed back, and the last page hands back an ordinary
cursor to follow from, so no single response carries everything a node holds.

A holdings row is `{info_hash, state, name, total_size, piece_length}`, where
`state` is `downloading`, `complete`, or — only ever in a delta — `gone`, the
tombstone for a dropped dataset. **Copies = holders in state `complete`**: a
node holding a dataset complete holds every file in it. So a reader lists the
datasets and counts their copies in one pass over the holdings streams, with no
per-dataset lookup.

Per-second numbers live in `/transfers`, bounded by what is in flight rather
than by what is stored. Piece bitfields come one dataset at a time from
`/holdings/<info_hash>`, so a swarm-wide piece map costs one request per node
holding *that* dataset. `/dataset/<info_hash>` carries the file → piece map,
which the piece-level per-file counts need. `/holdings/<info_hash>` is this
node's holding of a dataset and changes as it downloads; `/dataset/<info_hash>`
is the dataset itself, the same on every holder and fixed for its lifetime, so a
reader fetches it once from any holder and keeps it.

## Files

| File | Role |
|---|---|
| `node.py` | **The system.** libtorrent session + the sync tick (beacon, peers, mesh) + the HTTP API. Run one per machine. |
| `control.py` | CLI to talk to a node: publish, list, peers, status, add, remove, map. |
| `make_torrent.py` | Builds v2-only, private, trackerless torrents (`build()`). As a script, generates the sample content. |
| `node_client.py` | Stdlib client for another node's HTTP API: `fetch_stats`/`fetch_holdings` (cursor-following, raises `Resync`)/`fetch_transfers`/`fetch_holding`/`fetch_meta`, plus `fetch_swarm()` — every node's `/stats`, gathered through one node's peer table. No libtorrent. |
| `beacon.py` | The discovery datagram: join the group, send, drain. No libtorrent. |
| `config.py` | Ports, beacon group, timing, paths. |
| `swarm_stats.py` | The copy-count arithmetic: `overview_row()` scores a dataset from holdings alone, `holder_rows()`/`per_file()` work from piece bitfields. Shared by `control.py` and `collector.py`. |
| `collector.py` | *Optional.* Finds a node on the beacon and reads the swarm through it, keeping one cursor per node. Serves the `/api/*` dashboard endpoints and the web UI. |
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

- `nodes/<id>/torrents/` — the .torrent files for the datasets it holds
- `nodes/<id>/data/` — datasets it downloaded (published ones stay in place)
- `nodes/<id>/.resume/` — libtorrent fast-resume, so a restart doesn't re-download
- `nodes/<id>/node_key` — its persisted identity
- `data/sample/` — the built-in sample content

## Limitations

- **A dataset is only as durable as its holders.** One nobody holds is gone,
  along with any record that it existed, and a node that is *down* looks, in a
  single fan-out, like one that never had the data. Watching copy counts over
  time tells the two apart; that is the dashboard's job.
- **A transfer that hits a disk error stays stopped.** The node says so — in its
  log, in `/transfers`, and as a count in `/stats` — but libtorrent will not pick
  that torrent back up, and neither a restart nor fixing the disk restarts it.
  `remove` then `add` does, in seconds.
- **`remove` doesn't delete the files.** It stops serving a dataset and forgets
  it across restarts, but the downloaded copy stays in `nodes/<id>/data/`.
- **Nothing weighs a dataset against free space.** `add` never refuses, however
  little room is left. Placing datasets is manual, and nothing warns you that a
  copy count has dropped to one.
- **Datasets are immutable.** v2 torrents are fixed at publish time. Changed
  content published again is a separate dataset.
- **Everything on the segment is trusted.** Any node can publish anything and is
  believed, and a beacon can claim to be any node — nothing is signed or
  authenticated. This suits a private network, the assumed environment.
- **Discovery needs working multicast** between nodes: a node that cannot hear
  the beacon cannot join, and there is no address to bootstrap from. (The
  dashboard is the exception — it can be handed a node instead.)
- **A transfer pulls from everyone at once.** A node offers all its known peers
  to every dataset it is still missing, which grows as peers x transfers in
  flight. Nothing is offered for a dataset it already holds, so a settled swarm
  holds no peer connections at all.
