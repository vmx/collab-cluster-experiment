# Deploying on Incus

Launch N identical containers on one host, each running `node.py`. They find each
other over the managed bridge with no addresses configured anywhere — which is
the whole point, and what makes Incus a good place to watch it work.

Optionally add a `collector` container for the web UI. Only that one is ever
exposed to the internet (via an Incus proxy device); everything else lives on the
bridge, reachable by SSHing to the host. The node containers know nothing about
it — the dashboard reads them, not the other way round.

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
boot — installs `python3-libtorrent`, clones the repo, and copies the units into
place. It sets nothing swarm-specific, so the identical profile applies to every
container and names no other container.

The only line worth changing is the `git clone`: it decides which repo and branch
every container ends up with.

```sh
incus profile create collab-cluster
incus profile edit collab-cluster < incus/collab-cluster.yaml
```

## 2. Launch the containers

Names don't matter to the swarm — nodes are told no addresses and announce none,
so you can add and remove them freely. They're only how *you* refer to the
containers (and what `.incus` DNS name you'd type below).

```sh
for name in node0 node1 node2; do
  incus launch images:debian/14/cloud "$name" --profile default --profile collab-cluster
done
```

The profile only provisions each container — it configures nothing, because
there is nothing swarm-wide to configure. That is the whole swarm; the dashboard
in step 5 is optional and separate.

## 3. Start the nodes

The profile already copied both units (listed in
[`../systemd/README.md`](../systemd/README.md)) into
`/home/debian/.config/systemd/user/`, so here you just enable the one this
container plays — `collab-cluster-node`. First make sure provisioning has
finished:

```sh
for name in node0 node1 node2; do
  incus exec "$name" -- cloud-init status --wait
done
```

Then enable the node unit. Run these as the `debian` user — `su --login debian
--command` opens a login session so `systemctl --user` finds its user bus
(`XDG_RUNTIME_DIR`):

```sh
for name in node0 node1 node2; do
  incus exec "$name" -- su --login debian --command 'systemctl --user enable --now collab-cluster-node'
done
```

`systemd --user` units only start at boot if lingering is on, so enable it once
per container (as root, hence no `su`):

```sh
for name in node0 node1 node2; do
  incus exec "$name" -- loginctl enable-linger debian
done
```

## 4. Use it

`control.py` addresses nodes by their HTTP endpoint. Run it from your checkout on
the host — it only speaks HTTP, so unlike the containers the host needs no
`libtorrent`. Use the container bridge IPs; the host is on the bridge, so it
reaches them directly:

```sh
# find the IPs
incus list
# each node should see the others
python control.py peers 10.x.x.5
```

Now give the swarm something to replicate. `publish` reads the path on the node
itself, not on your host, so the data has to be there first — the sample content
isn't shipped, so generate it inside `node0`:

```sh
# writes data/sample/ into node0's checkout
incus exec node0 -- su --login debian --command 'python3 collab-cluster-experiment/make_torrent.py'
python control.py publish 10.x.x.5 /home/debian/collab-cluster-experiment/data/sample/media
```

The dataset now exists because `node0` holds it. Putting it on another node is
a separate decision, made per node:

```sh
# the swarm's catalog, read through node1...
python control.py list   10.x.x.6
# ...and now node1 keeps a copy
python control.py add    10.x.x.6 media
python control.py status 10.x.x.6
```

Nodes store only what they're told to store; nothing arrives on one unasked.

## 5. Optional: the dashboard

Launch one more container the same way and enable the other unit — that is all
of it. This container runs no node and doesn't need one: the dashboard hears the
nodes' beacons on the same bridge and reads the swarm through whichever answers,
so like them it is told nothing and configured with nothing.

```sh
incus launch images:debian/14/cloud collector --profile default --profile collab-cluster
incus exec collector -- cloud-init status --wait
incus exec collector -- su --login debian --command 'systemctl --user enable --now collab-cluster-collector'
incus exec collector -- loginctl enable-linger debian
```

Any node will do — it's a way in, not a destination — and if that one goes away
the dashboard picks up another by itself. The node containers are not touched
either: they are read, have no setting for this, and never hear from it.

Then expose just the UI:

```sh
incus config device add collector web proxy listen=tcp:0.0.0.0:8100 connect=tcp:127.0.0.1:8100
```

See the [proxy device docs](https://linuxcontainers.org/incus/docs/main/reference/devices_proxy/).

## Clean up

Nothing lives outside the containers, so deleting them is the whole teardown —
the proxy device and the datasets go with them.

```sh
# --force stops them first
incus delete --force node0 node1 node2 collector
```

The profile keeps no state; drop it too if you're done with the setup:

```sh
incus profile delete collab-cluster
```
