# Scaling

This design targets **25,000 datasets a day, of 100 MiB each, at three copies
apiece**. That is 2.38 TiB of new data a day, or 7.15 TiB stored. After a year
the swarm holds 9.1M datasets and 2.6 PiB.

Each node carries datasets × copies ÷ nodes of that. The per-node figures below
assume **25 nodes**, as a ballpark rather than a constraint. At 25 nodes, each
node takes 3,000 datasets and 293 GiB a day, which is 2.3 MiB/s of transfer.
After a year it holds 1.1M datasets and ~105 TiB. At fifty nodes, all of that
halves.

Everything outside "Where it stops" is implemented and measured, on libtorrent
2.0.14 and Python 3.14. "Where it stops" lists the limits that remain, each with
a proposed fix. Projections to the full load are linear extrapolations of a
measured per-unit cost. The figures are sizes and counts. Timings depend on the
hardware, so they are left out.

## Why it holds

Nothing a node is polled for grows with the number of datasets. Nothing grows
with the number of readers either.

**No node knows every dataset.** A node keeps a `.torrent` for each dataset it
holds, and nothing about the others. There is no central index either.
Publishing seeds the data in place, so a dataset has a holder from the moment it
exists. The list of datasets in the swarm is the union of every node's
holdings. The alternative, every node mirroring every torrent, would cost 9.1M ×
13 KB = ~120 GB of metadata per node per year. Most of it would be for datasets
the node never touches: at 25 nodes, a node holds an eighth of them.

**Holdings are a stream of transitions, not a list.** What a node holds changes
only when a dataset is taken, finishes or is dropped. So `/stats` carries a
cursor, and `/holdings?since=<cursor>` answers with what changed since. At 1.1M
held, `/stats` is 310 B, the same as at one dataset. A five-row delta is 708 B.
A settled swarm does no holdings traffic at all, and a busy one pays only for
the changes.

The log of transitions is bounded by `CHANGE_LOG_LIMIT` (10,000). At ~6,000
transitions per node per day, that is a day and a half. A reader that has been
away longer is told to re-list, rather than handed a delta with holes in it.

**A re-list is paged.** While a listing runs, its cursor remembers the position.
Datasets taken or dropped in the meantime arrive afterwards, as transitions and
tombstones. At 1.1M held, a re-list is 111 requests of 1.8 MB. The state lock is
held for one page at a time, not for the whole listing.

**Per-second numbers stay with what is in flight.** Progress and rates change
every second, so they would defeat the cursor. They live on `/transfers`
instead, which is sized by what is moving. Piece bitfields come one dataset at a
time, from `/holdings/<info_hash>`.

**Nothing walks the held set.** Every transition goes through `note_holding()`.
It keeps the running totals and the set of torrents in flight up to date. So
`/stats`, the session loop and `mesh()` cost what is in flight, not what is
held. `handle.status()` is never called across held torrents. Node throughput
comes from libtorrent's session counters, not from a sum over torrents.

**What a node writes is sharded.** `torrents/`, `.resume/` and `data/` hold one
entry per dataset. Each entry goes under `<dir>/<first byte of the info-hash>/`,
so 1.1M files spread across 256 directories. Every caller already has the
info-hash, so nothing is looked up to place a file. Boot walks the shards
without first listing every path.

### The readers

Nothing has the full list, so whoever wants a swarm-wide view assembles one.

**The dashboard maintains it.** The collector follows each node's cursor and
folds the changes into one entry per dataset. The entry stores the dataset's
identity once, and its holders as node ids. Nothing is rebuilt per request. Two
indexes are kept alongside:

- Datasets by copy count. The rarest-first list is read straight off it, and
  "how many are down to one copy" is just a length.
- Node → datasets. A node dying or re-listing costs only what that node holds.

Views are pages, narrowed on the server, so no response carries every dataset.

**The CLI merges it.** `control.py` keeps nothing between runs. But nodes list
their holdings in info-hash order, so it can merge their streams as it prints.
A dataset's holders arrive together, and the copy count falls out of the merge.
Only one page per node is in memory, and the first line prints before the last
node has answered. `map` walks the same stream and keeps only the rarest. To
resolve a short reference, `control.py` asks each node about its own holdings
(`/holdings?match=`). Each node answers with the matching rows only.

## BitTorrent here is not public BitTorrent

The usual BitTorrent intuitions come from public swarms: peers come from
trackers or the DHT, every torrent holds connections, choking protects the
seeder, and a partial copy is progress. None of them hold here.

**Peers are injected, never discovered.** Torrents are private and trackerless.
DHT, LSD, UPnP and NAT-PMP are off. The only source of peers is the multicast
beacon: ~80 bytes every two seconds, carrying identity and ports and nothing
about holdings. There are no announces. The peer table is O(nodes), the same
size whether a node holds two datasets or a million.

**A seeding torrent needs no connections.** The side that wants bytes opens the
connection, and libtorrent drops a seed-to-seed connection at the handshake.
`mesh()` offers peers only to torrents missing data. So a settled swarm holds no
peer connections at all, however much it stores. Connections scale with
transfers in flight, not with datasets stored.

**An offer is not a request.** `connect_peer()` does not open one connection. It
adds the peer to that torrent's peer list for good, and libtorrent keeps
dialling everyone on the list. A torrent missing bytes wants that. A seed does
not. It dials peers that have nothing to trade with it, each one hangs up, and
libtorrent dials again, indefinitely. Every one of those dials comes out of
`connection_speed`, the session's budget of 30 new connections a second.
Downloads that do need peers wait behind them.

So `mesh()` offers peers only to torrents missing bytes. That is not the same as
torrents not yet complete. A dataset being checked is not complete yet, but it
misses nothing. That covers every freshly published dataset, and everything held
after a restart. `mesh()` skips it until the check is done.

**Choking protects nothing.** Choking exists so that strangers who give nothing
back get nothing. A private segment has no strangers. With the default eight
upload slots, a seed with more interested peers rotates who it answers. A
leecher that is choked with requests outstanding sits snubbed until
`request_timeout` (one minute) drops the connection. The next connection then
finishes at once. A few such stalls add that minute to a whole batch. So
`unchoke_slots_limit` is -1, and nobody is choked.

**A partial copy is worth nothing.** A v2 torrent is verified against its
info-hash. So a holder in state `complete` has every byte, by definition, and
counting copies needs no piece detail. That is what keeps the list view free of
per-dataset lookups. A holder at 90% is not a copy. That is why downloads are
queued (see below) instead of all running at once.

**Metadata is 0.03% of the data.** 100 MiB at 256 KiB pieces is 400 pieces.
That makes a 12.8 KB piece layer and a 13,072-byte `.torrent`. The resume file
is about the same size. At 1.1M held, that is ~30 GB of metadata against ~105
TiB of data.

**A restart is not a re-download or a re-hash.** Fast-resume data is
self-contained (`save_info_dict`), and libtorrent trusts the checkpoint. So a
restart at 1.1M held reads ~15 GB of resume files instead of hashing ~105 TiB
of data.

**RAM is the one linear cost: 34.4 KiB per held torrent.** It was measured at
the real shape (100 MiB, 256 KiB pieces, v2-only, private) and cross-checked on
a live node. It is ~19 KiB fixed, plus 32 B per piece. That comes to ~0.35 GB
per TiB held, or ~38 GB at 1.1M, and it does not change with the node count.

## libtorrent's defaults are desktop defaults

The costs above are the protocol's. What decides whether this works at all is
libtorrent's queue. Its defaults suit a person seeding a handful of torrents
they chose. All of the settings below are in `node.make_session()`.

**`active_seeds` (5) pauses idle seeds, and a paused torrent refuses peers.**
Left idle, a node has all but a handful of its datasets paused. Another node
asking for those datasets gets almost none of them. There are no peer
connections, and nothing on either side says why. With seeding never queued
(`-1`), the same request completes.

**`active_checking` (1) confirms published datasets one at a time.** A published
dataset is complete once libtorrent has checked the files already in place. By
default it checks them one after another, so a batch of publishes is confirmed
one by one. At 64, they are checked in parallel. Until its check is done, a
dataset is not complete, so the swarm counts it as having no copies.

**`active_downloads` wants the opposite of `active_seeds`.** Run all at once,
transfers advance in lockstep and finish together. A large batch runs at nearly
the full rate cap for a long stretch without completing a single copy. Queued
32 at a time, copies complete in step with the bytes moved. The batch as a whole
is no slower, because hundreds of simultaneous connections mostly get in each
other's way. The queue only has to be deep enough to keep the link busy.

**`auto_manage_interval` (30 s) is how long a queued torrent waits paused.** At
the default, the first transfers of a batch can sit still for most of that
time. At 2 s, they start almost at once.

## Where it stops

What is left, in the order it bites, each with a proposed fix.

- **`UPLOAD_RATE_LIMIT` caps the swarm at 2.06 TiB/day.** Three copies need 4.77
  TiB/day. The limit is a demo knob: 1 MiB/s, so a transfer is slow enough to
  watch.
  **Fix:** default it to 0, and let the single-host walkthrough set it from the
  environment. The real need, 2.3 MiB/s per node, needs no cap.
- **A node restart makes every reader re-list it.** A restart mints a new cursor
  epoch. So readers that were up to date re-read everything the node holds:
  ~200 MB at 1.1M.
  **Fix:** persist `{epoch, seq}` across a restart, so an up-to-date reader gets
  an empty delta. The catch is that the restart must then stop *emitting*
  transitions. At 1.1M held that would be 2.2M transitions, which would overrun
  the log anyway. So the torrent state is restored from resume data without
  publishing it. Restored torrents that claim to be complete are then verified
  once. Otherwise a node that lost files while it was down would advertise
  copies it no longer has.
- **The collector keeps its aggregate in memory.** At 9.1M datasets that is 4.9
  GB. It is lost on restart and rebuilt by re-listing 27.5M rows. A name search
  has no index either, so it is a pass over all 9.1M datasets. That is why
  searching is a deliberate action, not something a poll repeats.
  **Fix:** `sqlite3`, which is in the standard library. The aggregate becomes a
  file with indexes. Cursors stored beside it let a restart resume on deltas.
  Names get an index. The two indexes kept by hand today, by copy count and
  node → datasets, become indexes the store keeps consistent. Writes are one
  transaction per poll. The aggregate can be rebuilt from the nodes, so
  durability can be relaxed: a crash costs a re-list.
- **Node RAM grows with holdings**: 34.4 KiB a dataset, ~38 GB at 1.1M. It never
  comes back down.
  **Fix, cheap:** raise `PIECE_SIZE`. The per-piece term is a third of the cost.
  1 MiB pieces cut the piece layer from 12.8 KB to 3.2 KB. 4 MiB pieces bring a
  torrent to ~20 KiB. The price is coarser progress and availability. It only
  helps datasets published after the change, because the piece size is part of
  what the info-hash hashes.
  **Fix, structural:** load a held torrent into libtorrent only while it serves.
  The 34.4 KiB is only needed to move bytes. Answering `/holdings` takes just
  the node's own ~900 B record. A node would add a torrent when a download is
  coming, and drop it once idle. For that, every holder has to hear that a
  download is coming, because a holder without the torrent loaded turns the
  connection away. Today only the holder that serves the `.torrent` hears
  anything. The request proposed under "A download dials every node" below
  would tell them all.

The rest grow with the number of nodes, not the number of datasets. None of them
shows at a couple of dozen nodes.

- **The collector asks every node for `/stats` on every poll.** So each poll
  costs one request per node.
  **Fix:** poll a slice of the nodes each cycle, so the requests per second stay
  the same however many nodes there are. `GONE_AFTER` (15 s) is the silence
  after which a node's copies stop counting. It would then have to cover a full
  round, so that a node that was simply not asked is not declared gone.
- **A download dials every node to find the few that hold it.** Nodes do not
  know who holds what, so `mesh()` offers every peer to every download in
  flight. A node without the dataset hangs up at the handshake, so nothing
  lingers. But every dial comes out of `connection_speed`, the session's 30 new
  connections a second. At a thousand nodes and 32 downloads in flight, that is
  some 32,000 dials before each download has found its holders. Nearly all of
  them go to nodes with nothing to give.
  **Fix:** before a download starts, the downloader asks every node once whether
  it holds the dataset. `mesh()` then offers the download only the nodes that
  say yes. The same request tells each holder that a download is coming, which
  the structural RAM fix above needs. It is one HTTP request per node per
  download, not per tick, and none of it comes out of `connection_speed`.
- **Every node hears every other node's beacon**, and keeps a peer entry for it.
  **Fix:** none needed into the hundreds of nodes. Beyond that, discovery on one
  flat network segment needs a different design, not a setting.

## Re-measuring

Nothing above needs the full load to check.

```sh
python node.py --id 0 &
# publish many datasets, then poll /stats until every one is complete
python control.py publish 127.0.0.1:8001 <path>
```

Per-torrent RAM: clone one 100 MiB v2 torrent into N distinct ones. Patching the
name in the bencoded info dict is enough, and the piece layers stay valid. Add
them to a `node.make_session()` in seed mode, and read `statm`.

The queue: hold N datasets on one session, and let it sit idle past
`auto_manage_interval`. Then ask a second session for an arbitrary one. The
useful number is not throughput, but whether it arrives at all.

Reader costs: run against a synthetic `ns.torrents` of 1.1M entries. The node's
side of them is pure Python.
