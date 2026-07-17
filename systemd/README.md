# systemd services

One `systemd --user` unit per long-running server component. Each is standalone
(no dependency on the others) so it can run on its own in a container; the
one-shot tools (`control.py`, `make_torrent.py`, `piece_map.py`) are not
services — you still run those by hand.

| Unit | Component | Listens on |
| --- | --- | --- |
| `collab-cluster-tracker.service`   | `bittorrent_tracker.py` — announce + catalog | `6969` |
| `collab-cluster-collector.service` | `collector.py` — stats collector + web UI    | `8100` |
| `collab-cluster-node.service`      | `node.py` — one node daemon                  | BT `6881`, control `8001` |

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

# Build the catalog once (the tracker serves it; its :6969 announce URL is baked
# into the torrents).
python make_torrent.py

systemctl --user enable --now collab-cluster-tracker collab-cluster-collector collab-cluster-node
```

## Use

```sh
systemctl --user status collab-cluster-tracker collab-cluster-collector collab-cluster-node
journalctl --user -u collab-cluster-node -f      # follow one component's logs
systemctl --user restart collab-cluster-node
```

Then drive the swarm as usual, e.g. `python control.py add 127.0.0.1:8001 media
--mode serve --path data/sample/media` (nodes are addressed by their control
endpoint `host[:port]`), and open the dashboard at <http://127.0.0.1:8100/>.

## Spread across hosts / containers

The units carry no addresses. On a single host they need nothing — `config.py`
defaults every service to loopback. When the tracker/collector run elsewhere,
`collab-cluster-node` and `collab-cluster-collector` read an optional
`~/.config/collab-cluster-experiment/env` (the tracker needs none — it only gets
dialed):

```sh
# ~/.config/collab-cluster-experiment/env  — same content works in every container
SWARM_TRACKER=<tracker-address>      # e.g. tracker.incus, or a host/IP
SWARM_COLLECTOR=<collector-address>  # e.g. collector.incus
```

Build torrents with the matching announce URL so it's baked in correctly:
`python make_torrent.py --tracker http://<tracker-address>:6969/announce …`
(or export `SWARM_TRACKER` before running it). The peer layer needs nothing —
each node auto-detects its own routable address (`SWARM_ADVERTISE_IP` overrides).
