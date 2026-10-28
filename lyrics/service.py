"""Lyrics operation: keep each song's lyrics status, fill in missing lyrics from LRCLIB, tidy orphaned .lrc files.

Lyrics are saved next to the song: synced ones as `<song>.lrc`, plain ones as `<song>.txt`. The audio file
isn't touched, and Navidrome, Subsonic apps and most players read both. Everything is journaled, so the
cleanup's Undo reverses it (a file that was replaced gets its old contents back).
"""
import collections
import json
import os

import crud
from cleanup import apply as cleanup_apply
from scan import TRASH_DIR

from . import files, lrclib


def _no_progress(done, total, message):
    pass


def refresh_status(conn, root):
    """Work out every song's lyrics kind and source (sidecars on disk can change without the audio changing)."""
    crud.lyrics.set_status(conn, files.status_for_all(conn, root))
    conn.commit()


def overview(conn, root):
    found = crud.lyrics.candidates(conn)
    return {"coverage": crud.lyrics.coverage(conn),
            "candidates": [{k: c[k] for k in ("track_id", "kind", "how", "path", "folder", "album", "title", "artist",
                                              "disc_no", "track_no", "current", "duration", "song_duration")}
                           | {"preview": (c["synced"] or c["plain"] or "")[:4000]} for c in found],
            "orphans": files.orphans(conn, root)}


def find(db_path, upgrade=False, refresh=False, skip=(), finished=None, progress=_no_progress):
    """Look up songs without lyrics on LRCLIB (and songs with only plain lyrics, with upgrade).
    Songs in `skip` are left out; the id of each song looked up is appended to `finished`, if given."""
    conn = crud.connect(db_path)
    asked = set() if refresh else crud.lyrics.asked(conn)
    wanted = ("lyrics IS NULL OR lyrics = 'plain'") if upgrade else "lyrics IS NULL"
    targets = [t for t in conn.execute(f"""SELECT id, title, artist, album_artist, album, duration, lyrics FROM tracks
                                           WHERE ({wanted}) AND title IS NOT NULL ORDER BY folder, disc_no, track_no""")
               if t["id"] not in asked and t["id"] not in skip]
    counts, failures = collections.Counter(), 0
    try:
        for i, t in enumerate(targets):
            progress(i, len(targets), f"{t['artist'] or t['album_artist'] or ''} - {t['title']}")
            try:
                found = lrclib.find(t["artist"] or t["album_artist"], t["title"], t["album"], t["duration"])
                failures = 0
            except lrclib.LrclibError:
                failures += 1
                counts["errors"] += 1
                if failures >= 5:
                    raise
                continue
            if found and t["lyrics"] == "plain" and found["kind"] != "synced":
                found = None  # nothing better than what's there
            crud.lyrics.save_candidate(conn, t["id"], found)
            conn.commit()
            counts[found["kind"] if found else "none"] += 1
            if finished is not None:
                finished.append(t["id"])
    finally:
        conn.close()
    progress(len(targets), len(targets), "Done")
    return {"songs": len(targets), **counts}


def _journal(root, kind, entries, stats, stamp):
    if not entries:
        return None
    path = os.path.join(cleanup_apply.JOURNAL_DIR, f"{stamp}-{kind}.json")
    os.makedirs(cleanup_apply.JOURNAL_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"root": root, "created": stamp, "kind": kind, "stats": stats, "entries": entries}, f,
                  ensure_ascii=False, indent=1)
    return path


def write_sidecar(root, path, text, synced, entries):
    """Save lyrics next to a song (.lrc if synced, else .txt), journaling what was there before."""
    rel = os.path.splitext(path)[0] + (".lrc" if synced else ".txt")
    full = os.path.join(root, rel)
    before = files._read(full) if os.path.exists(full) else None
    if before == text:
        return False
    with open(full, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    entries.append({"op": "file", "path": rel, "before": before})
    return True


def apply(db_path, track_ids, progress=_no_progress):
    """Save the chosen LRCLIB lyrics next to their songs."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    chosen = [c for c in crud.lyrics.candidates(conn, set(track_ids)) if c["kind"] in ("synced", "plain")]
    entries, stats = [], collections.Counter()
    stamp = cleanup_apply.journal_stamp()
    try:
        for i, c in enumerate(chosen):
            progress(i, len(chosen), c["path"])
            if not os.path.exists(os.path.join(root, c["path"])):
                stats["skipped"] += 1  # moved or removed since the lookup
                continue
            if c["current"] == "synced" or (c["current"] == "plain" and c["kind"] != "synced"):
                stats["skipped"] += 1  # already as good
                continue
            text = c["synced"] if c["kind"] == "synced" else c["plain"]
            if write_sidecar(root, c["path"], text, c["kind"] == "synced", entries):
                stats[c["kind"]] += 1
        crud.lyrics.forget_candidates(conn, [c["track_id"] for c in chosen])
        refresh_status(conn, root)
    finally:
        conn.commit()
        journal = _journal(root, "lyrics", entries, stats, stamp)
        conn.close()
    return {"stats": dict(stats), "journal": journal}


def fix_orphans(db_path, decisions, progress=_no_progress):
    """Reattach or trash orphaned .lrc files. `decisions`: {path: "reattach" | "trash"} (anything else: leave).

    Re-checked against the files as they are now: only orphans still orphaned are touched, and a
    reattach goes to the song Muse suggests, never over an existing file.
    """
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    current = {o["path"]: o for o in files.orphans(conn, root)}
    entries, stats = [], collections.Counter()
    stamp = cleanup_apply.journal_stamp()
    try:
        for path, action in decisions.items():
            orphan = current.get(path)
            if not orphan or action not in ("reattach", "trash"):
                continue
            if action == "reattach" and orphan["action"] == "reattach" and not os.path.exists(os.path.join(root, orphan["target"])):
                cleanup_apply._move(root, path, orphan["target"], entries, "extra")
                stats["reattached"] += 1
            else:
                ignore = os.path.join(root, TRASH_DIR, ".ndignore")
                os.makedirs(os.path.dirname(ignore), exist_ok=True)
                if not os.path.exists(ignore):
                    open(ignore, "w").close()
                cleanup_apply._move(root, path, os.path.join(TRASH_DIR, stamp, path), entries, "extra")
                stats["trashed"] += 1
            cleanup_apply._remove_empty_dirs(root, os.path.dirname(path))
        refresh_status(conn, root)
    finally:
        conn.commit()
        journal = _journal(root, "lyrics", entries, stats, stamp)
        conn.close()
    return {"stats": dict(stats), "journal": journal}
