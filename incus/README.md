# Deploying on Incus

Launch N identical containers on one host, each running `node.py`. They find each
other over the managed bridge with no addresses configured anywhere — which is
the whole point, and what makes Incus a good place to watch it work.

Optionally add a `collector` container for the web UI. Only that one is ever
exposed to the internet (via an Incus proxy device); everything else lives on the
bridge, reachable by SSHing to the host.

[`provision.sh`](provision.sh) does everything below in one idempotent command,
if you'd rather not run the steps by hand.

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
place, and writes the env file pointing nodes at `collector.incus`. The same
content works in every container; a node that never reaches a collector simply
reports to nothing.

```sh
incus profile create collab-cluster
incus profile edit collab-cluster < incus/collab-cluster.yaml
```

Edit the profile first if your dashboard container isn't named `collector`, or to
point the clone at your own fork/mirror of the repo.

## 2. Launch the containers

Node names don't matter — nodes are told no addresses and announce none, so you
can add and remove them freely. Only `collector` has to match the `.incus` name
in the env file.

```sh
incus launch images:debian/14/cloud collector --profile default --profile collab-cluster
for name in node0 node1 node2; do
  incus launch images:debian/14/cloud "$name" --profile default --profile collab-cluster
done
```

The collector is optional — skip it and the swarm replicates exactly the same,
you just don't get the web UI.

## 3. Enable the right unit per container

The profile already copied both units (listed in
[`../systemd/README.md`](../systemd/README.md)) into
`/home/debian/.config/systemd/user/`, so here you just enable the one that
container plays. First make sure provisioning has finished:

```sh
incus exec collector -- cloud-init status --wait
for name in node0 node1 node2; do
  incus exec "$name" -- cloud-init status --wait
done
```

Then enable each container's unit. Run these as the `debian` user — `su --login
debian --command` opens a login session so `systemctl --user` finds its user bus
(`XDG_RUNTIME_DIR`):

```sh
incus exec collector -- su --login debian --command 'systemctl --user enable --now collab-cluster-collector'
for name in node0 node1 node2; do
  incus exec "$name" -- su --login debian --command 'systemctl --user enable --now collab-cluster-node'
done
```

`systemd --user` units only start at boot if lingering is on, so enable it once
per container (as root, hence no `su`):

```sh
for name in collector node0 node1 node2; do
  incus exec "$name" -- loginctl enable-linger debian
done
```

## 4. Use it

`control.py` addresses nodes by their HTTP endpoint. From the host, use the
container bridge IPs — the host is on the bridge, so it reaches them directly:

```sh
# find the IPs
incus list
# each node should see the others
python control.py peers   10.x.x.5
python control.py publish 10.x.x.5 /home/debian/some-data
# every node knows about it
python control.py list    10.x.x.6
# ...and this one keeps a copy
python control.py add     10.x.x.6 some-data
python control.py status  10.x.x.6
```

The path given to `publish` is a path *inside* that node, not one on your host.
The sample content isn't shipped, so generate it on the node first if you want
something to publish:

```sh
incus exec node0 -- su --login debian --command 'python3 collab-cluster-experiment/make_torrent.py'
python control.py publish 10.x.x.5 /home/debian/collab-cluster-experiment/data/sample/media
```

Nodes store only what they're told to unless their unit passes `--replicate all`,
in which case the `add` step is unnecessary and the data arrives on its own.

Optional: to type `.incus` names on the host instead of IPs
(`control.py peers node0.incus`), teach the host resolver about the bridge — see
[integrate with systemd-resolved](https://linuxcontainers.org/incus/docs/main/howto/network_bridge_resolved/):

```sh
resolvectl dns    incusbr0 "$(incus network get incusbr0 ipv4.address | cut -d/ -f1)"
resolvectl domain incusbr0 '~incus'
```

This isn't persistent across reboots / Incus restarts on its own — the howto
shows a small unit to reapply it. Not needed if you address nodes by IP.

## 5. Optional: expose the dashboard

The collector serves the web UI on `8100`, reachable on the bridge already. To
publish just that one port off the host:

```sh
incus config device add collector web proxy listen=tcp:0.0.0.0:8100 connect=tcp:127.0.0.1:8100
```

See the [proxy device docs](https://linuxcontainers.org/incus/docs/main/reference/devices_proxy/).

## If nodes don't see each other

Check `python control.py peers <node>` first. Discovery is multicast on
`239.255.42.1:6772` with TTL 1; a Linux bridge normally floods that, but if
IGMP snooping is enabled without a querier it can be dropped. The fallback needs
one address, not a fix to the network — add `--peer node0.incus` to the node
unit's `ExecStart=` and gossip does the rest, in both directions.
