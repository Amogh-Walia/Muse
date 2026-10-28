"""Minimal MusicBrainz web-service client (https://musicbrainz.org/doc/MusicBrainz_API).

MusicBrainz allows about one request per second per client and asks for a descriptive
User-Agent; both are enforced here. Set MUSE_MB_CONTACT (a URL or e-mail) to include contact
details in the User-Agent, as MusicBrainz recommends.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config

BASE = "https://musicbrainz.org/ws/2/"
USER_AGENT = f"Muse/0.1 ( {config.MB_CONTACT or 'local music library tool'} )"
MIN_INTERVAL = 1.1  # seconds between requests

_lock = threading.Lock()
_last_request = 0.0


class MusicBrainzError(Exception):
    pass


def _get(path, params):
    global _last_request
    url = BASE + path + "?" + urllib.parse.urlencode({**params, "fmt": "json"})
    for attempt in range(5):
        with _lock:
            wait = MIN_INTERVAL - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504):
                raise MusicBrainzError(f"MusicBrainz said {e.code} for {url}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == 4:
                raise MusicBrainzError(f"could not reach MusicBrainz: {e}") from e
        time.sleep(2 ** attempt)  # back off: rate-limited or temporarily unavailable
    raise MusicBrainzError("MusicBrainz kept refusing requests; try again later")


def _quote(text):
    """Escape Lucene special characters for a phrase query."""
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)


def search_releases(album, artist=None, limit=10, loose=False):
    """Releases by title (an exact phrase, or any of its words with loose=True), optionally by artist."""
    title = f"release:({_quote(album)})" if loose else f'release:"{_quote(album)}"'
    query = title + (f' AND artist:"{_quote(artist)}"' if artist else "")
    return (_get("release/", {"query": query, "limit": limit}) or {}).get("releases", [])


def recordings_by_isrc(isrc):
    """The recordings carrying an ISRC (usually one)."""
    return (_get(f"isrc/{isrc}", {}) or {}).get("recordings", [])


def get_recording(recording_id):
    """A recording with every release it's on, and each release's group (type: album, single, compilation…)."""
    return _get(f"recording/{recording_id}", {"inc": "releases release-groups artist-credits"})


def search_recordings(title, artist=None, limit=10):
    """Recordings by title (and artist); search results include the releases they're on."""
    query = f'recording:"{_quote(title)}"' + (f' AND artist:"{_quote(artist)}"' if artist else "")
    return (_get("recording/", {"query": query, "limit": limit}) or {}).get("recordings", [])


def get_release(release_id):
    """Full release: tracklist with recordings, artist credits, labels and release group."""
    return _get(f"release/{release_id}", {"inc": "recordings artist-credits labels release-groups"})
