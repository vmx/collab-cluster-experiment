"""Client for the torrent catalog hosted on the tracker.

The catalog is just the .torrent files, hosted on the tracker (see
bittorrent_tracker.py's /catalog endpoints); nodes and control fetch them over
HTTP by name rather than reading a shared data/torrents directory. The tracker
derives each torrent's name + info-hash from the .torrent itself. Stdlib only —
kept free of libtorrent so control.py can use it without that dependency; the
node bdecodes the .torrent bytes itself.
"""
import json
import urllib.error
import urllib.request

import config


def _get(path: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(f"{config.TRACKER_BASE}{path}", timeout=timeout) as r:
        return r.read()


def fetch_list() -> list:
    """All catalog torrents as {"name", "info_hash"} dicts the tracker derives
    from each .torrent."""
    return json.loads(_get("/catalog").decode())


def fetch_stats(timeout: float = 5.0) -> dict:
    """The tracker's live /stats: swarm membership as learned from announces
    ({"uptime", "torrents": [{"info_hash", "announces", "peers": [...]}]}). The
    collector polls this to reconcile who announced against what nodes report."""
    return json.loads(_get("/stats", timeout).decode())


def fetch_torrent_bytes(name: str) -> bytes:
    """The raw .torrent bytes for `name` (bdecode/torrent_info them yourself).
    Raises FileNotFoundError if the catalog has no such torrent, so callers can
    treat it like a local-file lookup."""
    try:
        return _get(f"/catalog/{name}.torrent")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(name)
        raise


def publish(name: str, data: bytes, timeout: float = 5.0) -> dict:
    """Upload the raw .torrent bytes for `name` to the tracker's catalog (POST
    /catalog/<name>.torrent), so a node that built a torrent can register it
    without filesystem access to the tracker. The tracker validates and stores
    it; its watcher then serves it via fetch_torrent_bytes and streams it to
    subscribers. Returns the tracker's {"name", "info_hash"} echo. Raises
    urllib.error.HTTPError on rejection (e.g. 400 if the bytes aren't a valid
    v2 torrent, or `name` doesn't match the torrent's own name)."""
    req = urllib.request.Request(
        f"{config.TRACKER_BASE}/catalog/{name}.torrent", data=data, method="POST",
        headers={"Content-Type": "application/x-bittorrent"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def subscribe(on_torrent, since: int = None) -> None:
    """Stream newly added catalog torrents from the tracker, calling
    `on_torrent(meta)` for each (meta is {"name", "info_hash", "seq"}).

    Blocks, consuming the tracker's /catalog/subscribe Server-Sent Events stream
    until the connection ends (or the caller interrupts). With `since` omitted the
    stream carries only torrents added after this call connects; pass a seq to
    resume from there, or 0 to replay the whole catalog first.
    """
    url = f"{config.TRACKER_BASE}/catalog/subscribe"
    if since is not None:
        url += f"?since={int(since)}"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    # No read timeout: the stream is idle between additions (heartbeats aside).
    with urllib.request.urlopen(req) as resp:
        data_lines: list = []
        for raw in resp:                      # HTTPResponse yields one line at a time
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":                    # blank line terminates an SSE event
                if data_lines:
                    on_torrent(json.loads("\n".join(data_lines)))
                    data_lines = []
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
            # ':' comment lines (heartbeats) and other fields are ignored.
