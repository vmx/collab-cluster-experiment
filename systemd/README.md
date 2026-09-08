# systemd services

One `systemd --user` unit per long-running component. Both are standalone (no
dependency on each other) so either can run alone in a container; `control.py`
and `make_torrent.py` are one-shot tools, not services.

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

The unit stores nothing it wasn't asked for: the node joins, serves what it
holds, and you name the datasets it should keep. Nothing arrives on a machine
unasked.

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
# the swarm's catalog, read through any node
python control.py list    <other-node>
# ...and this one keeps a copy
python control.py add     <other-node> <name>
```

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
`ExecStart=` — the dashboard's one escape hatch, and one a node has no equivalent
of:

```ini
ExecStart=python3 collector.py node0.example:8001
```

## Spread across hosts

Nothing to do — that's the point. Nodes advertise nothing and are told nothing;
each learns its peers' addresses from the beacons it receives.

The one prerequisite is that multicast reaches between them (same segment,
TTL 1). A node that cannot hear the beacon cannot join.
