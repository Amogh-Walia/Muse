"""LRCLIB client (https://lrclib.net/docs): a free, community lyrics database with synced (LRC) lyrics.

No account is needed. Only artist, title, album and duration are sent. Requests are kept to about one
a second and retried when LRCLIB is busy (it answers 503 under load).
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from musicbrainz import match

BASE = "https://lrclib.net/api/"
USER_AGENT = f"Muse/0.1 ( {config.MB_CONTACT or 'local music library tool'} )"
MIN_INTERVAL = 1.0
DURATION_SLACK = 2  # seconds: a longer or shorter take is a different version (live, radio edit…)

_lock = threading.Lock()
_last = 0.0


class LrclibError(Exception):
    pass


def _get(endpoint, params):
    global _last
    url = BASE + endpoint + "?" + urllib.parse.urlencode(params)
    for attempt in range(5):
        with _lock:
            wait = MIN_INTERVAL - (time.monotonic() - _last)
            if wait > 0:
                time.sleep(wait)
            _last = time.monotonic()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504):
                raise LrclibError(f"LRCLIB said {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if attempt == 4:
                raise LrclibError(f"could not reach LRCLIB: {e}") from e
        time.sleep(2 ** attempt)
    raise LrclibError("LRCLIB stayed busy; try again later")


def _result(record, how):
    kind = ("instrumental" if record.get("instrumental") else "synced" if record.get("syncedLyrics")
            else "plain" if record.get("plainLyrics") else None)
    return kind and {"kind": kind, "how": how, "lrclib_id": record.get("id"), "synced": record.get("syncedLyrics"),
                     "plain": record.get("plainLyrics"), "duration": record.get("duration"),
                     "title": record.get("trackName"), "artist": record.get("artistName"), "album": record.get("albumName")}


def find(artist, title, album, duration):
    """Lyrics for one song, or None. Exact lookup first (artist, title, album, length), then a search
    keeping only results whose title and artist really match and whose length is within 2 seconds."""
    if duration:
        record = _get("get", {"artist_name": artist or "", "track_name": title, "album_name": album or "",
                              "duration": round(duration)})
        if record:
            found = _result(record, "exact match")
            if found:
                return found
    results = _get("search", {"track_name": title, "artist_name": artist or ""}) or []
    good = [r for r in results
            if match.similarity(r.get("trackName"), title) >= 0.85
            and (not artist or match.similarity(r.get("artistName"), artist) >= 0.6)
            and (not duration or abs((r.get("duration") or 0) - duration) <= DURATION_SLACK)]
    good.sort(key=lambda r: (not r.get("syncedLyrics"), abs((r.get("duration") or 0) - (duration or 0))))
    return _result(good[0], "search") if good else None
