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
resolved relative to that dir. On a machine running a node, `python3` must have
the `libtorrent` binding importable; the dashboard and the one-shot tools are
plain stdlib clients and don't need it.

## Install

```sh
mkdir -p ~/.config/systemd/user
cp systemd/collab-cluster-*.service ~/.config/systemd/user/
systemctl --user daemon-reload

systemctl --user enable --now collab-cluster-node
```

That is the whole install on every machine. A node needs no configuration and no
addresses at all — it discovers its peers on the local network, and nothing,
including the optional dashboard, has to be pointed at it.

As shipped, the unit stores nothing it wasn't asked for: the node joins, tracks
the whole catalog and serves what it holds, and you name the datasets it should
keep. To have a machine mirror everything instead, add the flag:

```ini
ExecStart=python3 node.py --replicate all
```

Editing a unit after it is running takes a `systemctl --user daemon-reload` and a
`restart` of it to take effect.

## Use

```sh
systemctl --user status collab-cluster-node
# follow its log
journalctl --user -u collab-cluster-node -f
# resumes with progress intact
systemctl --user restart collab-cluster-node
```

Then put some data in, from wherever you can reach a node:

```sh
# confirm they found each other
python control.py peers   <node-address>
python control.py publish <node-address> /path/to/data
# every node now knows about it
python control.py list    <other-node>
# ...and this one keeps a copy
python control.py add     <other-node> <name>
```

The last step is only needed on nodes that aren't running `--replicate all`.

## The optional dashboard

Only if you want the web UI. Like a node it takes no configuration and no
addresses — it listens to the same beacon and reads the swarm through whichever
node answers — so this is the whole of it, on any machine on the segment,
with or without a node of its own:

```sh
# serves :8100
systemctl --user enable --now collab-cluster-collector
```

Any node will do: it is a way in, not a destination, and the dashboard moves to
another one by itself if that node goes away. Nothing is configured on the nodes
either — they have no dashboard setting, and because it only ever listens and
never beacons back, they cannot tell whether anyone is watching.

Only where multicast doesn't reach does it need naming a node in the unit's
`ExecStart=`, the same escape hatch a node has:

```ini
ExecStart=python3 collector.py node0.example:8001
```

## Spread across hosts

Nothing to do — that's the point. Nodes advertise nothing and are told nothing;
each learns its peers' addresses from the beacons it receives.

The one prerequisite is that multicast reaches between them (same segment,
TTL 1). Where it doesn't, give a node one address to start from and it learns the
rest by gossip — add `--peer <a-known-node>` to the unit's `ExecStart=`.
