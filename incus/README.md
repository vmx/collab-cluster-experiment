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
incus config device add collector web proxy listen=tcp:0.0.0.0:8100 connect=tcp:127.0.0.1:8100
```

See the [proxy device docs](https://linuxcontainers.org/incus/docs/main/reference/devices_proxy/).

## 5. Drive the swarm from the host

`control.py` runs on the host and addresses each node by its bridge IP (the host
is on the bridge, so it reaches them directly):

```sh
# find node IPs
incus list
# what that node holds
python control.py status <node0-ip>
```

To add content, tell a node to `serve` a file/dir it has on disk. If that torrent
isn't in the catalog yet, the node builds it and publishes it to the tracker, so
registering and seeding are one step. The sample content isn't shipped, so
generate it on the node first:

```sh
# on node0: generate the sample content onto its disk
incus exec node0 -- su --login debian --command 'python3 collab-cluster-experiment/make_torrent.py'
# from the host: have node0 serve it (builds + publishes the torrent)
python control.py add <node0-ip> media --mode serve --path /home/debian/collab-cluster-experiment/data/sample/media
```

`--path` is a path *inside node0* — hence the container's absolute path — not one
on your host.

Once it's published, any other node can download it:

```sh
# lands in nodes/0/media/ on that node
python control.py add <node1-ip> media --mode download
```
