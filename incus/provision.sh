#!/bin/sh
# Provision and run the whole swarm on Incus: N nodes and an optional collector
# (web UI), one container each. This is the scripted form of the steps in
# incus/README.md, and it is idempotent — re-running it skips what already
# exists, so it doubles as a "bring everything back up" command.
#
# Usage:   ./incus/provision.sh
# Tunables (environment):
#   IMAGE=images:debian/14/cloud   image to launch (needs cloud-init)
#   NODES=3                        how many node containers
#   PROFILE=collab-cluster         profile name to create/update
#   WEB_PORT=8100                  host port for the collector's web UI
#   EXPOSE_WEB=1                   0 = don't add the public proxy device
#   COLLECTOR=1                    0 = nodes only, no dashboard container
#   DATA_ROOT=                     host dir whose <name> subdir becomes each
#                                   node's storage, e.g. a directory on a
#                                   separate ZFS partition. Unset: nodes store
#                                   inside their containers as usual.
#
# Nothing here is swarm configuration, and there is none to write: every
# container finds what it needs on the bridge. Nodes are told no addresses and
# discover each other by multicast; the collector container, which runs no node
# of its own, finds one to read through the same way. The nodes are not touched
# by it and cannot tell whether anyone is watching.
set -eu

IMAGE=${IMAGE:-images:debian/14/cloud}
NODES=${NODES:-3}
PROFILE=${PROFILE:-collab-cluster}
WEB_PORT=${WEB_PORT:-8100}
EXPOSE_WEB=${EXPOSE_WEB:-1}
COLLECTOR_ENABLED=${COLLECTOR:-1}
DATA_ROOT=${DATA_ROOT:-}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COLLECTOR=collector

say() {
	printf '\n==> %s\n' "$*"
}

command -v incus >/dev/null 2>&1 || {
	echo 'error: incus not found in PATH' >&2
	exit 1
}

node_names() {
	i=0
	while [ "$i" -lt "$NODES" ]; do
		printf 'node%s\n' "$i"
		i=$((i + 1))
	done
}

# Every container: the nodes, plus the collector when the dashboard is wanted.
all_names() {
	if [ "$COLLECTOR_ENABLED" = 1 ]; then
		printf '%s\n' "$COLLECTOR"
	fi
	node_names
}

exists() {
	incus info "$1" >/dev/null 2>&1
}

# Run a command as the `debian` user with a login shell, so `systemctl --user`
# finds its user bus (XDG_RUNTIME_DIR).
as_debian() {
	incus exec "$1" -- su --login debian --command "$2"
}

say "Loading profile '$PROFILE'"
if incus profile show "$PROFILE" >/dev/null 2>&1; then
	echo "profile exists, updating from collab-cluster.yaml"
else
	incus profile create "$PROFILE"
fi
incus profile edit "$PROFILE" <"$SCRIPT_DIR/collab-cluster.yaml"

say "Launching containers ($NODES nodes)"
for name in $(all_names); do
	if exists "$name"; then
		# Already provisioned; just make sure it's running (e.g. after a reboot).
		state=$(incus list "^${name}$" --format csv --columns s)
		if [ "$state" = RUNNING ]; then
			echo "$name: already running"
		else
			echo "$name: starting"
			incus start "$name"
		fi
	else
		echo "$name: launching"
		incus launch "$IMAGE" "$name" --profile default --profile "$PROFILE"
	fi
done

if [ -n "$DATA_ROOT" ]; then
	say "Mounting node storage from $DATA_ROOT"
	for name in $(node_names); do
		data_dir="$DATA_ROOT/$name"
		mkdir -p "$data_dir"
		if incus config device get "$name" data source >/dev/null 2>&1; then
			echo "$name: disk device already present"
		else
			echo "$name: mounting $data_dir at /mnt/collab-data"
			incus config device add "$name" data disk source="$data_dir" path=/mnt/collab-data shift=true
		fi
	done
fi

say 'Waiting for cloud-init to finish provisioning'
for name in $(all_names); do
	printf '%s: ' "$name"
	if incus exec "$name" -- cloud-init status --wait; then
		:
	else
		# Degraded/error still often leaves a usable container, so warn and go on;
		# `incus exec <name> -- cloud-init status --long` has the details.
		echo "warning: $name reported a cloud-init problem, continuing" >&2
	fi
done

say 'Enabling one unit per container'
enable_unit() {
	name=$1
	unit=$2
	echo "$name: $unit"
	as_debian "$name" "systemctl --user enable --now $unit"
	# systemd --user units only come back at boot if the user lingers.
	incus exec "$name" -- loginctl enable-linger debian
}
for name in $(node_names); do
	enable_unit "$name" collab-cluster-node
done
if [ "$COLLECTOR_ENABLED" = 1 ]; then
	# After the nodes, so there is a beacon to hear as soon as it starts.
	enable_unit "$COLLECTOR" collab-cluster-collector
fi

if [ "$COLLECTOR_ENABLED" = 1 ] && [ "$EXPOSE_WEB" = 1 ]; then
	say "Exposing the web UI on tcp:0.0.0.0:$WEB_PORT"
	if incus config device get "$COLLECTOR" web listen >/dev/null 2>&1; then
		echo 'proxy device already present'
	else
		incus config device add "$COLLECTOR" web proxy "listen=tcp:0.0.0.0:$WEB_PORT" connect=tcp:127.0.0.1:8100
	fi
fi

# The bridge IP is how the host reaches a container; control.py takes it directly.
ip_of() {
	incus list "^${1}$" --format csv --columns 4 | head -n 1 | cut -d ' ' -f 1
}

say 'Swarm is up'
for name in $(all_names); do
	printf '  %-12s %s\n' "$name" "$(ip_of "$name")"
done

first_node=$(node_names | sed -n 1p)
second_node=$(node_names | sed -n 2p)

if [ "$COLLECTOR_ENABLED" = 1 ]; then
	cat <<EOF

Dashboard: http://$(ip_of "$COLLECTOR"):8100/  (also http://<this-host>:$WEB_PORT/)
EOF
fi

cat <<EOF

Drive the swarm from here, addressing nodes by the IPs above, e.g.:

  # confirm the nodes found each other
  python control.py peers $(ip_of "$first_node")
  # generate the sample content on $first_node
  incus exec $first_node -- su --login debian --command 'python3 collab-cluster-experiment/make_torrent.py'
  # hash it into a dataset and seed it in place
  python control.py publish $(ip_of "$first_node") /home/debian/collab-cluster-experiment/data/sample/media
  # every node learns about it; have $second_node keep a copy
  python control.py add $(ip_of "$second_node") media
EOF
