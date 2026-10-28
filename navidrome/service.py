"""Keep Navidrome in step with Muse: make sure it runs, rescan after changes, link songs to Navidrome ids.

Navidrome's song ids follow file paths, so after a cleanup moves files they're re-linked once
Navidrome has rescanned. Songs are matched by real path when Navidrome reports it (see
documentation/navidrome-setup.md), else by MusicBrainz recording id, else by album/disc/track/title.
"""
import datetime
import re
import subprocess
import time

import config
import crud

from . import client
from .client import NavidromeError


def _no_progress(done, total, message):
    pass


def status():
    """Is Navidrome configured and answering, and what's it doing?"""
    out = {"configured": client.configured(), "reachable": False, "public_url": config.NAVIDROME_PUBLIC_URL or None}
    if not out["configured"]:
        return out
    try:
        pong = client.ping()
        out.update(reachable=True, version=pong.get("serverVersion"), scan=client.scan_status())
    except NavidromeError as e:
        out["error"] = str(e)
    return out


def ensure_running(wait=20):
    """Ping Navidrome; if it's down, start its systemd user service (NAVIDROME_SERVICE) and wait for it."""
    try:
        client.ping()
        return "running"
    except NavidromeError:
        if not config.NAVIDROME_SERVICE:
            raise
    started = subprocess.run(["systemctl", "--user", "start", config.NAVIDROME_SERVICE], capture_output=True, text=True)
    if started.returncode != 0:
        raise NavidromeError(f"Navidrome is down and `systemctl --user start {config.NAVIDROME_SERVICE}` failed: "
                             f"{started.stderr.strip() or started.returncode}")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            client.ping()
            return "started"
        except NavidromeError:
            pass
    raise NavidromeError(f"started the {config.NAVIDROME_SERVICE} service, but Navidrome still isn't answering")


WATCHER_GRACE = 15  # seconds: Navidrome's folder watcher waits a few seconds after changes, then scans


def rescan(wait=True, timeout=1800, progress=_no_progress):
    """Have Navidrome pick up changes, optionally waiting until its scan has finished.

    Only Navidrome admins may start a scan. Muse's own (non-admin) user instead relies on Navidrome's
    folder watcher, which notices changes by itself: Muse gives it a moment, then waits for that scan.
    """
    try:
        client.start_scan()
        first_wait = 1
    except NavidromeError as e:
        if "not authorized" not in str(e).lower():
            raise
        first_wait = WATCHER_GRACE
    if not wait:
        return
    progress(0, 0, "Waiting for Navidrome to notice the changes…")
    deadline = time.monotonic() + timeout
    time.sleep(first_wait)
    while time.monotonic() < deadline:
        st = client.scan_status()
        if not st.get("scanning"):
            return st
        progress(0, 0, f"Navidrome is scanning… {st.get('count', 0)} files so far")
        time.sleep(2)
    raise NavidromeError("Navidrome's scan is taking very long; links will be updated next time")


def _norm(text):
    return re.sub(r"[\W_]+", "", (text or "").lower())


def _relative(path, root, known):
    """A Navidrome path as a library-relative path: under our root, or the longest tail we know."""
    path = (path or "").replace("\\", "/")
    if root and path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1:]
    parts = path.strip("/").split("/")
    for i in range(len(parts)):
        candidate = "/".join(parts[i:])
        if candidate in known:
            return candidate
    return None


def link(conn, root):
    """Store each song's Navidrome song and album id. Returns counts."""
    tracks = conn.execute("SELECT id, path, album, disc_no, track_no, title FROM tracks").fetchall()
    by_path = {t["path"]: t["id"] for t in tracks}
    by_key = {(_norm(t["album"]), t["disc_no"] or 1, t["track_no"], _norm(t["title"])): t["id"] for t in tracks}
    by_recording = crud.tracks.recording_ids(conn)
    links, how = {}, {"path": 0, "musicbrainz": 0, "tags": 0}
    unmatched = 0
    for song in client.all_songs():
        tid = by_path.get(_relative(song.get("path"), root, by_path))
        kind = "path"
        if tid is None and song.get("musicBrainzId"):
            tid, kind = by_recording.get(song["musicBrainzId"]), "musicbrainz"
        if tid is None:
            tid, kind = by_key.get((_norm(song.get("album")), song.get("discNumber") or 1, song.get("track"),
                                    _norm(song.get("title")))), "tags"
        if tid is None or tid in links:
            unmatched += 1
            continue
        links[tid] = (song["id"], song.get("albumId"))
        how[kind] += 1
    crud.tracks.set_navidrome_ids(conn, links)
    crud.set_meta(conn, "navidrome_linked", datetime.datetime.now().isoformat(timespec="seconds"))
    conn.commit()
    return {"linked": len(links), "songs_in_library": len(tracks), "navidrome_only": unmatched, "matched_by": how}


def sync(db_path, progress=_no_progress):
    """Make sure Navidrome runs, rescan it, wait, and re-link every song."""
    progress(0, 0, "Checking Navidrome is running…")
    started = ensure_running()
    progress(0, 0, "Navidrome is scanning…")
    rescan(wait=True, progress=progress)
    progress(0, 0, "Linking songs to Navidrome…")
    conn = crud.connect(db_path)
    try:
        return {"navidrome": started, **link(conn, crud.get_root(conn))}
    finally:
        conn.close()
