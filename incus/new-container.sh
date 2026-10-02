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
# Usage:   ./incus/new-container.sh node <name> [data-root] [--policy <file>]
#          ./incus/new-container.sh node <name> [data-root] --rescue <bytes> --collector <host[:port]>
#          ./incus/new-container.sh collector <name>
#   data-root   (node only) host dir whose <name> subdir becomes this node's
#               storage (data, torrents, fast-resume), e.g. a directory on a
#               separate ZFS partition. Omit it and the node stores inside the
#               container as usual.
#   --policy    (node only) a data manager policy file on this host: pushed
#               into the container, and the data manager (collab-cluster-utils)
#               run next to the node, deciding what it holds. Re-run with a
#               changed file to apply it. Omit it and there's no data manager.
#   --rescue    (node only) make it a rescue node instead: the rescuer
#               (collab-cluster-utils) fills up to <bytes> with the swarm's
#               rarest datasets, asking the collector at --collector. Re-run
#               with other values to change them.
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
	echo "usage: $0 node <name> [data-root] [--policy <file>]" >&2
	echo "       $0 node <name> [data-root] --rescue <bytes> --collector <host[:port]>" >&2
	echo "       $0 collector <name>" >&2
	exit 1
}

ROLE=
NAME=
DATA_ROOT=
POLICY=
RESCUE=
COLLECTOR=
npos=0
while [ $# -gt 0 ]; do
	case $1 in
	--policy)
		[ $# -ge 2 ] || usage
		POLICY=$2
		shift 2
		;;
	--rescue)
		[ $# -ge 2 ] || usage
		RESCUE=$2
		shift 2
		;;
	--collector)
		[ $# -ge 2 ] || usage
		COLLECTOR=$2
		shift 2
		;;
	-*)
		usage
		;;
	*)
		npos=$((npos + 1))
		case $npos in
		1) ROLE=$1 ;;
		2) NAME=$1 ;;
		3) DATA_ROOT=$1 ;;
		*) usage ;;
		esac
		shift
		;;
	esac
done
[ "$npos" -ge 2 ] || usage
case $ROLE in
node) ;;
collector)
	[ "$npos" -eq 2 ] || usage
	;;
*)
	usage
	;;
esac
UNIT=collab-cluster-$ROLE
if [ -n "$POLICY" ]; then
	[ "$ROLE" = node ] || {
		echo 'error: --policy only applies to a node' >&2
		exit 1
	}
	[ -f "$POLICY" ] || {
		echo "error: policy file $POLICY not found" >&2
		exit 1
	}
fi
if [ -n "$RESCUE$COLLECTOR" ]; then
	[ "$ROLE" = node ] || {
		echo 'error: --rescue only applies to a node' >&2
		exit 1
	}
	[ -z "$POLICY" ] || {
		echo 'error: a node runs either a data manager (--policy) or a rescuer (--rescue)' >&2
		exit 1
	}
	if [ -z "$RESCUE" ] || [ -z "$COLLECTOR" ]; then
		echo 'error: --rescue and --collector go together' >&2
		exit 1
	fi
	case $RESCUE in
	'' | *[!0-9]*)
		echo "error: --rescue takes a number of bytes, not $RESCUE" >&2
		exit 1
		;;
	esac
fi

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

if [ -n "$POLICY" ]; then
	say "Running the data manager on '$NAME' with policy $POLICY"
	policy_dest=/home/debian/collab-cluster-utils/data-manager-policy.toml
	incus file push "$POLICY" "$NAME$policy_dest"
	incus exec "$NAME" -- chown debian:debian "$policy_dest"
	# restart, not just start: the policy is read once at startup, so a re-run
	# with a changed file takes effect.
	as_debian "$NAME" 'systemctl --user disable --now collab-cluster-rescuer 2>/dev/null; systemctl --user enable collab-cluster-data-manager && systemctl --user restart collab-cluster-data-manager'
fi

if [ -n "$RESCUE" ]; then
	say "Running the rescuer on '$NAME': up to $RESCUE bytes, asking $COLLECTOR"
	env_dest=/home/debian/collab-cluster-utils/rescuer.env
	printf 'RESCUE_BYTES=%s\nCOLLECTOR=%s\n' "$RESCUE" "$COLLECTOR" |
		incus file push - "$NAME$env_dest"
	incus exec "$NAME" -- chown debian:debian "$env_dest"
	# restart, not just start: settings are read once at startup.
	as_debian "$NAME" 'systemctl --user disable --now collab-cluster-data-manager 2>/dev/null; systemctl --user enable collab-cluster-rescuer && systemctl --user restart collab-cluster-rescuer'
fi

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
