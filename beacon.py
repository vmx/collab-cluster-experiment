"""Peer discovery: the multicast beacon, on the wire.

Every node multicasts a tiny datagram to the local segment every tick — who it
is, its ports, and a digest of its catalog — and reads everyone else's. That one
mechanism replaces the tracker: it answers both "which nodes exist" and "has
anyone published something new", and a peer's address comes free as the
datagram's source, so no node ever has to work out (or be told) its own routable
address.

The message is JSON, and this is the whole of it:

    {"v": 1, "node": <node_key>, "bt": <port>, "http": <port>, "cat": <digest>}

Deliberately no address field — see above. `v` is the only thing a receiver
insists on; anything else is ignored, so the datagram can grow.

This lives apart from node.py because it needs nothing but the stdlib: anything
that wants a way into the swarm can listen without libtorrent. The dashboard
does exactly that, and *only* that — it never sends a beacon, so no node ever
learns it exists.
"""
import errno
import json
import socket
import struct

import config

# Receivers ignore anything else, so this only moves if the datagram's meaning
# changes — new fields don't need it.
VERSION = 1


def make_socket() -> "socket.socket":
    """A UDP socket joined to the beacon group, ready to send and to be drained.

    SO_REUSEADDR/SO_REUSEPORT so several nodes (and a dashboard) can share the
    port on one host, multicast loopback left on so those co-located processes
    actually hear each other, and TTL 1 so the beacon never leaves the local
    segment. Non-blocking: everyone drains on their own schedule rather than
    parking a thread in recvfrom().
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", config.BEACON_PORT))
    mreq = struct.pack("4s4s", socket.inet_aton(config.BEACON_GROUP),
                       socket.inet_aton("0.0.0.0"))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    s.setblocking(False)
    return s


def send(sock, message: dict) -> None:
    """Announce `message` to the local segment."""
    try:
        sock.sendto(json.dumps(message).encode(),
                    (config.BEACON_GROUP, config.BEACON_PORT))
    except OSError as exc:
        # No multicast route (offline host, restricted container). Not fatal in
        # itself — a lone node runs fine — but it will not meet anyone.
        if exc.errno not in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPERM):
            raise


def drain(sock) -> list:
    """Every beacon waiting on the socket, as (message, sender_ip) pairs.

    Takes in one tick's worth without blocking (hence no listener thread). At
    ~120 bytes every couple of seconds the socket buffer holds far more than a
    tick's worth, so nothing is missed between drains. Garbage on a well-known
    port is somebody else's traffic, not an error: it is skipped silently.
    """
    out = []
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except BlockingIOError:
            return out
        except OSError:
            return out
        try:
            msg = json.loads(data)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("v") == VERSION:
            out.append((msg, addr[0]))
