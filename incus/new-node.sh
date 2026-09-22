#!/bin/sh
# Add a single node container to a collab-cluster swarm on Incus.
#
# provision.sh stands up a whole swarm (N nodes + collector) in one call; this
# is the one-node building block, for growing a swarm incrementally or
# spreading it across several hosts — run it once per node, on whichever
# server that node should live on. Idempotent: re-running it with the same
# name changes nothing once the node is up and enabled.
#
# Usage:   ./incus/new-node.sh <name> [data-root]
#   data-root   host dir whose <name> subdir becomes this node's storage
#               (data, torrents, fast-resume), e.g. a directory on a separate
#               ZFS partition. Omit it and the node stores inside the
#               container as usual.
# Tunables (environment):
#   IMAGE=images:debian/14/cloud   image to launch (needs cloud-init)
#   PROFILE=collab-cluster         profile name to create/update
set -eu

IMAGE=${IMAGE:-images:debian/14/cloud}
PROFILE=${PROFILE:-collab-cluster}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
	echo "usage: $0 <name> [data-root]" >&2
	exit 1
fi
NAME=$1
DATA_ROOT=${2:-}

say() {
	printf '\n==> %s\n' "$*"
}

command -v incus >/dev/null 2>&1 || {
	echo 'error: incus not found in PATH' >&2
	exit 1
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

say "Launching container '$NAME'"
if exists "$NAME"; then
	# Already provisioned; just make sure it's running (e.g. after a reboot).
	state=$(incus list "^${NAME}$" --format csv --columns s)
	if [ "$state" = RUNNING ]; then
		echo "$NAME: already running"
	else
		echo "$NAME: starting"
		incus start "$NAME"
	fi
else
	echo "$NAME: launching"
	incus launch "$IMAGE" "$NAME" --profile default --profile "$PROFILE"
fi

if [ -n "$DATA_ROOT" ]; then
	data_dir="$DATA_ROOT/$NAME"
	say "Mounting $data_dir at /mnt/collab-data"
	mkdir -p "$data_dir"
	if incus config device get "$NAME" data source >/dev/null 2>&1; then
		echo 'disk device already present'
	else
		incus config device add "$NAME" data disk source="$data_dir" path=/mnt/collab-data shift=true
	fi
fi

say 'Waiting for cloud-init to finish provisioning'
if incus exec "$NAME" -- cloud-init status --wait; then
	:
else
	# Degraded/error still often leaves a usable container, so warn and go on;
	# `incus exec <name> -- cloud-init status --long` has the details.
	echo "warning: $NAME reported a cloud-init problem, continuing" >&2
fi

say "Enabling collab-cluster-node on '$NAME'"
as_debian "$NAME" "systemctl --user enable --now collab-cluster-node"
# systemd --user units only come back at boot if the user lingers.
incus exec "$NAME" -- loginctl enable-linger debian

# The bridge IP is how the host reaches a container; control.py takes it directly.
ip=$(incus list "^${NAME}$" --format csv --columns 4 | head -n 1 | cut -d ' ' -f 1)

say 'Node is up'
printf '  %-12s %s\n' "$NAME" "$ip"

cat <<EOF

Drive it from here, e.g.:

  python control.py peers $ip
EOF
