#!/bin/sh
# Add a single container — a node or a collector — to a collab-cluster swarm on
# Incus.
#
# provision.sh stands up a whole swarm (N nodes + collector) in one call; this
# is the one-container building block, for growing a swarm incrementally or
# spreading it across several hosts — run it once per container, on whichever
# server that container should live on. Idempotent: re-running it with the same
# arguments changes nothing once the container is up and enabled.
#
# Usage:   ./incus/new-container.sh node <name> [data-root]
#          ./incus/new-container.sh collector <name>
#   data-root   (node only) host dir whose <name> subdir becomes this node's
#               storage (data, torrents, fast-resume), e.g. a directory on a
#               separate ZFS partition. Omit it and the node stores inside the
#               container as usual.
# Tunables (environment):
#   IMAGE=images:debian/14/cloud   image to launch (needs cloud-init)
#   PROFILE=collab-cluster         profile name to create/update
#   WEB_PORT=8100                  (collector) host port for the web UI; give
#                                   each collector on a host its own
#   EXPOSE_WEB=1                   (collector) 0 = don't add the public proxy
#                                   device
set -eu

IMAGE=${IMAGE:-images:debian/14/cloud}
PROFILE=${PROFILE:-collab-cluster}
WEB_PORT=${WEB_PORT:-8100}
EXPOSE_WEB=${EXPOSE_WEB:-1}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage() {
	echo "usage: $0 node <name> [data-root]" >&2
	echo "       $0 collector <name>" >&2
	exit 1
}

[ $# -ge 2 ] || usage
ROLE=$1
NAME=$2
DATA_ROOT=${3:-}
case $ROLE in
node)
	[ $# -le 3 ] || usage
	;;
collector)
	[ $# -eq 2 ] || usage
	;;
*)
	usage
	;;
esac
UNIT=collab-cluster-$ROLE

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

say "Enabling $UNIT on '$NAME'"
as_debian "$NAME" "systemctl --user enable --now $UNIT"
# systemd --user units only come back at boot if the user lingers.
incus exec "$NAME" -- loginctl enable-linger debian

if [ "$ROLE" = collector ] && [ "$EXPOSE_WEB" = 1 ]; then
	say "Exposing the web UI on tcp:0.0.0.0:$WEB_PORT"
	if incus config device get "$NAME" web listen >/dev/null 2>&1; then
		echo 'proxy device already present'
	else
		incus config device add "$NAME" web proxy "listen=tcp:0.0.0.0:$WEB_PORT" connect=tcp:127.0.0.1:8100
	fi
fi

# The bridge IP is how the host reaches a container; control.py takes it directly.
ip=$(incus list "^${NAME}$" --format csv --columns 4 | head -n 1 | cut -d ' ' -f 1)

say "$ROLE is up"
printf '  %-12s %s\n' "$NAME" "$ip"

if [ "$ROLE" = collector ]; then
	cat <<EOF

Dashboard: http://$ip:8100/
EOF
	if [ "$EXPOSE_WEB" = 1 ]; then
		echo "  (also http://<this-host>:$WEB_PORT/)"
	fi
else
	cat <<EOF

Drive it from here, e.g.:

  python control.py peers $ip
EOF
fi
