"""Subsonic API client for Navidrome (https://www.navidrome.org/docs/developers/subsonic-api/).

Requests use Subsonic token auth (md5(password + salt), a fresh salt each time). They're made from
the Muse server only: the password and tokens never reach the browser.
"""
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

import config

CLIENT = "muse"          # the "player" name Navidrome records for Muse
API_VERSION = "1.16.1"
BROWSER_CODECS = {"flac", "mp3", "vorbis", "opus", "aac", "wav"}  # play as-is; anything else (ALAC…) is transcoded


class NavidromeError(Exception):
    pass


def configured():
    return bool(config.NAVIDROME_URL and config.NAVIDROME_USER and config.NAVIDROME_PASSWORD)


def _url(endpoint, **params):
    salt = secrets.token_hex(8)
    auth = {"u": config.NAVIDROME_USER, "t": hashlib.md5((config.NAVIDROME_PASSWORD + salt).encode()).hexdigest(),
            "s": salt, "v": API_VERSION, "c": CLIENT}
    return f"{config.NAVIDROME_URL}/rest/{endpoint}?" + urllib.parse.urlencode({**auth, **params})


def call(endpoint, timeout=15, **params):
    """A JSON Subsonic call. Raises NavidromeError if Navidrome is down or refuses."""
    if not configured():
        raise NavidromeError("Navidrome isn't configured (set NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASSWORD in .env)")
    try:
        with urllib.request.urlopen(_url(endpoint, f="json", **params), timeout=timeout) as r:
            body = json.load(r)["subsonic-response"]
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise NavidromeError(f"can't reach Navidrome at {config.NAVIDROME_URL}: {getattr(e, 'reason', e)}") from e
    except (ValueError, KeyError) as e:
        raise NavidromeError(f"{config.NAVIDROME_URL} didn't answer like Navidrome") from e
    if body.get("status") != "ok":
        error = body.get("error", {})
        raise NavidromeError(f"Navidrome refused {endpoint}: {error.get('message', 'unknown error')}")
    return body


def ping():
    return call("ping", timeout=3)


def start_scan(full=False):
    return call("startScan", fullScan=str(full).lower())["scanStatus"]


def scan_status():
    return call("getScanStatus")["scanStatus"]


def all_songs(page=500):
    """Every song Navidrome knows (an empty search3 query lists them all), page by page."""
    offset = 0
    while True:
        songs = call("search3", query="", songCount=page, songOffset=offset, albumCount=0, artistCount=0,
                     timeout=60)["searchResult3"].get("song", [])
        yield from songs
        if len(songs) < page:
            return
        offset += page


def stream(song_id, transcode=False, range_header=None):
    """Open a stream for proxying: the file as-is (seekable, Range honoured) or transcoded to MP3 320."""
    params = {"id": song_id, "format": "mp3", "maxBitRate": 320} if transcode else {"id": song_id, "format": "raw"}
    request = urllib.request.Request(_url("stream", **params))
    if range_header and not transcode:
        request.add_header("Range", range_header)
    try:
        return urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return e  # unsatisfiable range: relayed as-is
        raise NavidromeError(f"Navidrome couldn't stream that song ({e.code})") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise NavidromeError(f"can't reach Navidrome: {getattr(e, 'reason', e)}") from e
