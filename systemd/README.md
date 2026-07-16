# systemd services

One `systemd --user` unit per long-running server component. Each is standalone
(no dependency on the others) so it can run on its own in a container; the
one-shot tools (`control.py`, `make_torrent.py`, `piece_map.py`) are not
services — you still run those by hand.

| Unit | Component | Listens on |
| --- | --- | --- |
| `bt-tracker.service`   | `bittorrent_tracker.py` — announce + catalog | `6969` |
| `bt-collector.service` | `collector.py` — stats collector + web UI    | `8100` |
| `bt-node.service`      | `node.py --id 0` — one node daemon           | BT `6881`, control `8001` |

No paths are hardcoded: the units run `python3` from `PATH` and, being user
units, default their working directory to the user's home. So put the repo at
the home directory of the user that runs the service (the natural container
layout) — or add a `WorkingDirectory=` line if it lives elsewhere. `python3`
must have the `libtorrent` binding importable.

## Install

```sh
mkdir -p ~/.config/systemd/user
cp systemd/bt-*.service ~/.config/systemd/user/
systemctl --user daemon-reload

# Build the catalog once (the tracker serves it; its :6969 announce URL is baked
# into the torrents).
python make_torrent.py

systemctl --user enable --now bt-tracker bt-collector bt-node
```

## Use

```sh
systemctl --user status bt-tracker bt-collector bt-node
journalctl --user -u bt-node -f      # follow one component's logs
systemctl --user restart bt-node
```

Then drive the swarm as usual, e.g. `python control.py add 0 media --mode serve
--path data/sample/media`, and open the dashboard at <http://127.0.0.1:8100/>.

Running each in its own container instead of one host? They reach each other by
address, so point the node at the others' hosts (the tracker URL baked into the
torrents and the node's `--collector` / `SWARM_ADVERTISE_IP`, all in
`config.py`) rather than the `127.0.0.1` defaults.
