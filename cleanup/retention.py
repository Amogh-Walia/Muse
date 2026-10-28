"""Keep undo history from growing forever.

A change stays undoable for config.UNDO_DAYS (MUSE_UNDO_DAYS). After that its journal expires together
with everything only that journal could restore: picture backups from the artwork operation, and the
duplicates it discarded into .muse-trash/<stamp>/, which are then deleted for good. Undo always reverses
the newest change first, so expiring the oldest ones never breaks it.

Also removed: journals already undone (there's no redo), backup folders whose journal is gone, cached
cover images nothing refers to, and artwork candidates and MusicBrainz proposals for albums that no
longer exist (renamed or moved by a cleanup).
"""
import collections
import datetime
import json
import os
import re
import shutil

import config
import crud
from scan import TRASH_DIR

from .apply import JOURNAL_DIR

STAMP = re.compile(r"^(\d{8}-\d{6})")


def _created(name, path):
    """When a journal, backup or trash folder was made: from its timestamped name, else its mtime."""
    m = STAMP.match(name)
    if m:
        return datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
    return datetime.datetime.fromtimestamp(os.path.getmtime(path))


def _size(path):
    """(bytes, files) of a file or a folder tree."""
    if os.path.isfile(path):
        return os.path.getsize(path), 1
    total = files = 0
    for dirpath, _, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
                files += 1
            except OSError:
                pass
    return total, files


def _expiry(created, days, now):
    if not days:
        return None, False
    expires = created + datetime.timedelta(days=days)
    return expires, now >= expires


def inventory(root, days=None, now=None):
    """Everything undo keeps: journals, picture backups, discarded duplicates and the cover cache."""
    days = config.UNDO_DAYS if days is None else days
    now = now or datetime.datetime.now()
    journals, backups_in_use = [], set()
    names = sorted(os.listdir(JOURNAL_DIR)) if os.path.isdir(JOURNAL_DIR) else []
    for name in names:
        path = os.path.join(JOURNAL_DIR, name)
        if not name.endswith(".json"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            j = {}
        backup = j.get("backup")
        size = os.path.getsize(path) + (_size(os.path.join(JOURNAL_DIR, backup))[0] if backup else 0)
        if backup:
            backups_in_use.add(backup)
        created = _created(name, path)
        expires, expired = _expiry(created, days, now)
        journals.append({"name": name, "kind": j.get("kind") or "cleanup", "created": created.isoformat(timespec="seconds"),
                         "entries": len(j.get("entries", [])), "bytes": size, "backup": backup,
                         "for_library": j.get("root") == root, "undone": name.endswith(".undone.json"),
                         "expires": expires.isoformat(timespec="seconds") if expires else None, "expired": expired})
    orphan_backups = [n for n in names if os.path.isdir(os.path.join(JOURNAL_DIR, n)) and n not in backups_in_use]

    trash, trash_root = [], os.path.join(root, TRASH_DIR)
    for name in sorted(os.listdir(trash_root)) if os.path.isdir(trash_root) else []:
        path = os.path.join(trash_root, name)
        if not os.path.isdir(path):  # .ndignore
            continue
        size, files = _size(path)
        created = _created(name, path)
        expires, expired = _expiry(created, days, now)
        trash.append({"name": name, "bytes": size, "files": files, "created": created.isoformat(timespec="seconds"),
                      "expires": expires.isoformat(timespec="seconds") if expires else None, "expired": expired})

    cache_bytes, cache_files = _size(config.ART_CACHE) if os.path.isdir(config.ART_CACHE) else (0, 0)
    live = [j for j in journals if not j["undone"]]
    return {
        "days": days,
        "journals": journals,
        "orphan_backups": orphan_backups,
        "trash": trash,
        "totals": {
            "journals": len(live), "journal_bytes": sum(j["bytes"] for j in journals),
            "trash_bytes": sum(t["bytes"] for t in trash), "trash_files": sum(t["files"] for t in trash),
            "cache_bytes": cache_bytes, "cache_files": cache_files,
            "next_expiry": min([j["expires"] for j in live if j["expires"]] + [t["expires"] for t in trash if t["expires"]],
                               default=None),
        },
    }


def prune(conn, root, days=None, now=None):
    """Delete what has expired or can't be used any more. Returns what was removed."""
    inv = inventory(root, days, now)
    report = collections.Counter()

    for j in inv["journals"]:
        if not (j["undone"] or j["expired"]):
            continue
        if j["backup"]:
            shutil.rmtree(os.path.join(JOURNAL_DIR, j["backup"]), ignore_errors=True)
        os.remove(os.path.join(JOURNAL_DIR, j["name"]))
        report["undone_journals" if j["undone"] else "expired_journals"] += 1
        report["bytes"] += j["bytes"]
    for name in inv["orphan_backups"]:
        path = os.path.join(JOURNAL_DIR, name)
        report["bytes"] += _size(path)[0]
        shutil.rmtree(path, ignore_errors=True)
        report["orphan_backups"] += 1

    for t in inv["trash"]:
        if t["expired"]:
            shutil.rmtree(os.path.join(root, TRASH_DIR, t["name"]), ignore_errors=True)
            report["trash_folders"] += 1
            report["trash_files"] += t["files"]
            report["bytes"] += t["bytes"]
    trash_root = os.path.join(root, TRASH_DIR)
    if os.path.isdir(trash_root) and not [n for n in os.listdir(trash_root) if n != ".ndignore"]:
        shutil.rmtree(trash_root, ignore_errors=True)

    # Artwork candidates and MusicBrainz proposals of albums that no longer exist, then unreferenced covers.
    live = {f"{folder}\x00{songs[0]['album'] or ''}" for (folder, _), songs in crud.tracks.by_album(conn).items()}
    report["stale_candidates"] += crud.artwork.prune_candidates(conn, live)
    report["stale_proposals"] += crud.releases.prune_proposals(conn, live)
    conn.commit()
    if os.path.isdir(config.ART_CACHE):
        keep = crud.artwork.referenced_images(conn)
        for name in os.listdir(config.ART_CACHE):
            path = os.path.join(config.ART_CACHE, name)
            if name not in keep and os.path.isfile(path):
                report["bytes"] += os.path.getsize(path)
                os.remove(path)
                report["cached_covers"] += 1
    return dict(report)
