"""Client for the torrent catalog hosted on the tracker.

The torrents live on the tracker (see bittorrent_tracker.py's /catalog endpoints),
so nodes and control fetch them over HTTP by name rather than reading a shared
data/torrents directory. Stdlib only — kept free of libtorrent so control.py can
use it without that dependency; the node bdecodes the .torrent bytes itself.
"""
import json
import urllib.error
import urllib.request

import config


def _get(path: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(f"{config.TRACKER_BASE}{path}", timeout=timeout) as r:
        return r.read()


def fetch_list() -> list:
    """All catalog torrents (sidecar meta dicts), as the tracker reports them."""
    return json.loads(_get("/catalog").decode())


def fetch_meta(name: str) -> dict:
    """One torrent's sidecar meta. Raises FileNotFoundError if the tracker has no
    such torrent (so callers can treat it like the old local-file lookup)."""
    try:
        return json.loads(_get(f"/catalog/{name}.json").decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(name)
        raise


def fetch_torrent_bytes(name: str) -> bytes:
    """The raw .torrent bytes for `name` (bdecode/torrent_info them yourself)."""
    return _get(f"/catalog/{name}.torrent")
