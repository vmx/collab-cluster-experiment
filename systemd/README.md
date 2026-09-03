# systemd services

One `systemd --user` unit per long-running component. Both are standalone (no
dependency on each other) so either can run alone in a container; `control.py`,
`make_torrent.py` and `piece_map.py` are one-shot tools, not services.

| Unit | Component | Listens on |
| --- | --- | --- |
| `collab-cluster-node.service`      | `node.py` — a node        | BT `6881`, HTTP `8001`, beacon `6772/udp` |
| `collab-cluster-collector.service` | `collector.py` — the optional dashboard | `8100` |

The units run `python3` from `PATH` and set
`WorkingDirectory=%h/collab-cluster-experiment`, so check the repo out at
`~/collab-cluster-experiment` (the natural container layout) — or change that
`WorkingDirectory=` if it lives elsewhere. Scripts, `data/`, and `nodes/` are all
resolved relative to that dir. `python3` must have the `libtorrent` binding
importable.

## Install

```sh
mkdir -p ~/.config/systemd/user
cp systemd/collab-cluster-*.service ~/.config/systemd/user/
systemctl --user daemon-reload

systemctl --user enable --now collab-cluster-node
```

That is the whole install on every machine. A node needs no configuration and no
addresses — it discovers its peers on the local network.

As shipped, the unit stores nothing it wasn't asked for: the node joins, tracks
the whole catalog and serves what it holds, and you name the datasets it should
keep. To have a machine mirror everything instead, add the flag:

```ini
ExecStart=python3 node.py --replicate all
```

## Use

```sh
systemctl --user status collab-cluster-node
journalctl --user -u collab-cluster-node -f      # follow its log
systemctl --user restart collab-cluster-node     # resumes with progress intact
```

Then put some data in, from wherever you can reach a node:

```sh
python control.py peers   <node-address>       # confirm they found each other
python control.py publish <node-address> /path/to/data
python control.py list    <other-node>         # every node now knows about it
python control.py add     <other-node> <name>  # ...and this one keeps a copy
```

The last step is only needed on nodes that aren't running `--replicate all`.

## The optional dashboard

Only if you want the web UI. Run the collector somewhere:

```sh
systemctl --user enable --now collab-cluster-collector    # serves :8100
```

and point nodes at it by writing one line to the env file the node unit reads
(`-` on the `EnvironmentFile=` line means it's fine for this to be absent, which
is the normal case):

```sh
# ~/.config/collab-cluster-experiment/env
SWARM_COLLECTOR=<collector-address>      # e.g. collector.incus, or a host/IP
```

Nothing else needs it. Nodes that never hear of a collector replicate exactly the
same.

## Spread across hosts

Nothing to do — that's the point. Nodes advertise nothing and are told nothing;
each learns its peers' addresses from the beacons it receives.

The one prerequisite is that multicast reaches between them (same segment,
TTL 1). Where it doesn't, give a node one address to start from and it learns the
rest by gossip — add `--peer <a-known-node>` to the unit's `ExecStart=`.
