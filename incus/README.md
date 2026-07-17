# Deploying on Incus

Run each component in its own Incus container on one host: a `tracker`, a
`collector` (which serves the web UI), and one or more `nodeN`. They find each
other by **Incus DNS name** (`<container>.incus`) on the managed bridge, so no
IP is ever hardcoded — the only Incus-specific glue is here; the `systemd/` units
themselves stay deployment-agnostic.

Only the web UI is exposed to the internet (via an Incus proxy device);
everything else is reachable only on the bridge, i.e. by SSHing to the host.

## How the pieces resolve

- **Container → container** (`SWARM_TRACKER=tracker.incus`, node → collector,
  collector → tracker): resolved automatically by the bridge's DNS. Nothing to
  configure.
- **Baked announce URL** (`http://tracker.incus:6969/announce`): resolved by the
  node containers at announce time — also on the bridge, so automatic.
- **Host → container** (running `control.py` on the host): *not* automatic. Use
  bridge IPs (below), or optionally teach the host to resolve `.incus`.

## Prerequisites

- Incus with a managed bridge (default `incusbr0`) providing DNS.
- A cloud-init-enabled image for the profile, e.g. `images:debian/14/cloud`.
- Containers can reach the git URL in the profile (clone happens at first boot).

The profile provisions the rest per container: it clones this repo into
`/home/debian/collab-cluster-experiment` (owned by the image's default `debian`
user, which runs the services), installs `python3-libtorrent`, and copies the
units into `~/.config/systemd/user/`. The units set `WorkingDirectory` to that
checkout, so their scripts and data (`data/`, `nodes/`) all resolve inside it.

## 1. Load the profile

[`collab-cluster.yaml`](collab-cluster.yaml) provisions each container at first
boot — clones the repo, installs `python3-libtorrent`, copies the units into
place, and writes the env file pointing the units at `tracker.incus` /
`collector.incus`. Same content for every container (the tracker just ignores the
env vars).

```sh
incus profile create collab-cluster
incus profile edit collab-cluster < incus/collab-cluster.yaml
```

Edit the profile first if your service containers aren't named `tracker` and
`collector`, or to point the clone at your own fork/mirror of the repo.

## 2. Launch the containers

Names matter — `tracker` and `collector` must match the `.incus` names in the env
file.

```sh
incus launch images:debian/14/cloud tracker --profile default --profile collab-cluster
incus launch images:debian/14/cloud collector --profile default --profile collab-cluster
incus launch images:debian/14/cloud node0 --profile default --profile collab-cluster
incus launch images:debian/14/cloud node1 --profile default --profile collab-cluster
```

All nodes run the default `--id 0`; they stay distinct because each has its own
IP and its own persisted `node_key` (and the dashboard labels them by their
`ip:port` address).

## 3. Enable the right unit per container

The profile already copied all three units (listed in
[`../systemd/README.md`](../systemd/README.md)) into
`/home/debian/.config/systemd/user/`, so here you just enable the one that
container plays. First make sure provisioning has finished:

```sh
incus exec tracker -- cloud-init status --wait
incus exec collector -- cloud-init status --wait
incus exec node0 -- cloud-init status --wait
incus exec node1 -- cloud-init status --wait
```

Then enable each container's unit. Run these as the `debian` user — `su --login
debian --command` opens a login session so `systemctl --user` finds its user bus
(`XDG_RUNTIME_DIR`):

```sh
incus exec tracker -- su --login debian --command 'systemctl --user enable --now collab-cluster-tracker'
incus exec collector -- su --login debian --command 'systemctl --user enable --now collab-cluster-collector'
incus exec node0 -- su --login debian --command 'systemctl --user enable --now collab-cluster-node'
incus exec node1 -- su --login debian --command 'systemctl --user enable --now collab-cluster-node'
```

`systemd --user` units only start at boot if lingering is on, so enable it once
per container (run as root, hence no `su`):

```sh
incus exec tracker -- loginctl enable-linger debian
incus exec collector -- loginctl enable-linger debian
incus exec node0 -- loginctl enable-linger debian
incus exec node1 -- loginctl enable-linger debian
```

## 4. Expose the web UI to the internet

An Incus proxy device on the `collector` container — the only thing on the public
interface:

```sh
# non-NAT (wildcard listen ok), fine for a web UI:
incus config device add collector web proxy \
  listen=tcp:0.0.0.0:8100 connect=tcp:127.0.0.1:8100

# or NAT mode (kernel-forwarded, faster) — listen must be a concrete host IP:
incus config device add collector web proxy \
  listen=tcp:<host-ip>:8100 connect=tcp:0.0.0.0:8100 nat=true
```

See the [proxy device docs](https://linuxcontainers.org/incus/docs/main/reference/devices_proxy/).

## 5. Build the catalog

The tracker serves the catalog from **its own** container's
`collab-cluster-experiment/data/torrents`, so build the torrents there (running
inside the container also means `tracker.incus` resolves for the baked announce
URL):

```sh
incus exec tracker -- su --login debian --command 'python3 collab-cluster-experiment/make_torrent.py --tracker http://tracker.incus:6969/announce'
```

(If you'd rather build elsewhere, push the resulting `.torrent` files into the
tracker container's `collab-cluster-experiment/data/torrents`, or share that dir
via an Incus disk device.)

## 6. Drive the swarm from the host

`control.py` addresses nodes by their control endpoint. From the host, use the
container bridge IPs (the host is on the bridge, so it reaches them directly):

```sh
incus list                                   # find node IPs
python control.py status 10.x.x.5            # what that node holds
python control.py add    10.x.x.5 media --mode serve --path data/sample/media
```

(`--path` is resolved on the node relative to the node service's
`WorkingDirectory` — the repo checkout — so it's just `data/sample/media`.
`control.py` itself runs from your host checkout.)

Optional: to type `.incus` names on the host instead of IPs
(`control.py status node0.incus`), teach the host resolver about the bridge —
see [integrate with systemd-resolved](https://linuxcontainers.org/incus/docs/main/howto/network_bridge_resolved/):

```sh
resolvectl dns incusbr0 "$(incus network get incusbr0 ipv4.address | cut --delimiter=/ --fields=1)"
resolvectl domain incusbr0 '~incus'
```

This isn't persistent across reboots / Incus restarts on its own — the howto
shows a small unit to reapply it. Not needed if you address nodes by IP.
