"""Album art operation: find cover candidates for albums whose songs lack embedded art, then embed the chosen ones.

    find    gathers candidates: image files in the album folder, art embedded in a sibling song, and
            (online) the Cover Art Archive via MusicBrainz and optionally iTunes
    upload  adds an image picked from the user's drive as a candidate
    apply   embeds the chosen cover into the album's songs, journaled so the cleanup's Undo reverts it
"""
import collections
import json
import os

import crud
import scan
from cleanup import apply as cleanup_apply
from musicbrainz import client as mb_client, match, service as mb_service

from . import embed, images, sources

LOCAL_SOURCES = ("folder", "embedded")
ONLINE_SOURCES = ("coverartarchive", "itunes")
COVER_FILES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png", "front.jpg")


def _no_progress(done, total, message):
    pass


def _groups(conn):
    return {mb_service.album_key(folder, songs[0]["album"] or ""): songs
            for (folder, _), songs in crud.tracks.by_album(conn).items()}


def albums(conn, include_covered=False):
    """Albums with songs lacking embedded art (all albums with include_covered), with their candidates."""
    candidates = collections.defaultdict(list)
    for c in crud.artwork.candidates(conn):
        candidates[c["album_key"]].append(c)
    applied = crud.artwork.applied(conn)
    out = []
    for key, songs in _groups(conn).items():
        lacking = [s for s in songs if not s["has_cover"]]
        if not lacking and not include_covered:
            continue
        album, artist = mb_service.album_facts(songs)
        out.append({
            "key": key, "folder": songs[0]["folder"], "album": album, "artist": artist,
            "songs": len(songs), "lacking": len(lacking),
            "cover_id": next((s["id"] for s in songs if s["has_cover"]), None),
            "candidates": candidates.get(key, []), "applied": applied.get(key),
        })
    return sorted(out, key=lambda a: ((a["artist"] or "").lower(), (a["album"] or "").lower()))


def _store(conn, key, source, found):
    for c in found:
        try:
            sha, mime, width, height = images.store(c["data"])
        except images.NotAnImage:
            continue
        crud.artwork.add_candidate(conn, key, source, c["label"], sha, mime, width, height, len(c["data"]),
                                   c["url"], c["rank"])


def find(db_path, online=True, use_itunes=False, include_covered=False, keys=None, skip=(), finished=None,
         progress=_no_progress):
    """Look for cover candidates for every album lacking art (or just `keys`). Albums in `skip` are left out;
    the key of each album searched is appended to `finished`, if given."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    groups = _groups(conn)
    targets = [a for a in albums(conn, include_covered) if (keys is None or a["key"] in keys) and a["key"] not in skip]
    errors, with_candidates = [], 0
    try:
        for i, a in enumerate(targets):
            progress(i, len(targets), f"{a['artist']} - {a['album']}")
            songs = groups[a["key"]]
            crud.artwork.clear_candidates(conn, a["key"], LOCAL_SOURCES)
            _store(conn, a["key"], "folder", sources.folder_images(root, a["folder"]))
            _store(conn, a["key"], "embedded", sources.sibling_art(root, songs))
            if online:
                crud.artwork.clear_candidates(conn, a["key"], ONLINE_SOURCES)
                try:
                    release_id, best, _ = mb_service.best_match(songs)
                    # don't take art from a release by some other artist
                    if release_id and (best is None or match.artist_similarity(best, a["artist"]) >= 0.6):
                        group = ((best or {}).get("release-group") or {}).get("id")
                        _store(conn, a["key"], "coverartarchive", sources.cover_art_archive(release_id, group))
                    if use_itunes:
                        _store(conn, a["key"], "itunes", sources.itunes(a["album"], a["artist"]))
                except mb_client.MusicBrainzError as e:
                    errors.append(f"{a['album']}: {e}")
            conn.commit()
            with_candidates += bool(crud.artwork.candidates(conn, a["key"]))
            if finished is not None:
                finished.append(a["key"])
    finally:
        conn.close()
    progress(len(targets), len(targets), "Done")
    return {"albums": len(targets), "with_candidates": with_candidates, "errors": errors}


def add_upload(db_path, key, name, data):
    """Add an image picked from the user's drive as a candidate for album `key`."""
    conn = crud.connect(db_path)
    try:
        if key not in _groups(conn):
            raise LookupError("no such album")
        sha, mime, width, height = images.store(data)
        crud.artwork.add_candidate(conn, key, "upload", f"From your drive: {os.path.basename(name or 'image')}",
                                   sha, mime, width, height, len(data), None, 0)
        conn.commit()
        return {"sha": sha, "mime": mime, "width": width, "height": height}
    finally:
        conn.close()


def apply(db_path, choices, replace=False, save_folder=True, progress=_no_progress):
    """Embed the chosen cover ({album key: image sha}) into each album's songs that lack art (all, with replace)."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    groups = _groups(conn)
    plan = []
    for key, sha in choices.items():
        cand = next((c for c in crud.artwork.candidates(conn, key) if c["sha"] == sha), None)
        if key not in groups or cand is None:
            raise ValueError("that image isn't a candidate for that album; look for artwork again")
        targets = [s for s in groups[key] if replace or not s["has_cover"]]
        plan.append((key, cand, targets))
    total = sum(len(t) for _, _, t in plan)

    stamp = cleanup_apply.journal_stamp()
    journal_path = os.path.join(cleanup_apply.JOURNAL_DIR, f"{stamp}-artwork.json")
    backup_dir = f"{stamp}-artwork-backup"  # pictures replaced or kept, for undo
    entries, stats, done = [], collections.Counter(), 0
    try:
        for key, cand, targets in plan:
            data, mime, width, height = images.for_embedding(images.load(cand["sha"]))
            for song in targets:
                progress(done, total, song["path"])
                done += 1
                full = os.path.join(root, song["path"])
                if not os.path.exists(full):
                    stats["skipped"] += 1
                    continue
                size = os.path.getsize(full)
                try:
                    before = embed.embed(full, data, mime, width, height, replace=replace)
                except ValueError:  # format without picture support
                    stats["unsupported"] += 1
                    continue
                saved = []
                for p in before:
                    os.makedirs(os.path.join(cleanup_apply.JOURNAL_DIR, backup_dir), exist_ok=True)
                    name = os.path.join(backup_dir, f"{len(entries)}-{len(saved)}.bin")
                    with open(os.path.join(cleanup_apply.JOURNAL_DIR, name), "wb") as f:
                        f.write(p["data"])
                    saved.append({**{k: v for k, v in p.items() if k != "data"}, "file": name})
                entries.append({"op": "art", "path": song["path"], "pictures": saved, "size": size})
                scan.refresh(conn, root, song["path"])
                conn.commit()
                stats["songs"] += 1
            folder = os.path.join(root, groups[key][0]["folder"])
            existing = {n.lower() for n in os.listdir(folder)} if os.path.isdir(folder) else set()
            if save_folder and groups[key][0]["folder"] and not existing & set(COVER_FILES):
                name = "cover.png" if mime == "image/png" else "cover.jpg"
                with open(os.path.join(folder, name), "wb") as f:
                    f.write(data)
                entries.append({"op": "created", "path": os.path.join(groups[key][0]["folder"], name)})
                stats["cover_files"] += 1
            crud.artwork.record(conn, key, groups[key][0]["folder"], groups[key][0]["album"], cand, len(targets))
            conn.commit()
            stats["albums"] += 1
    finally:
        conn.commit()
        if entries:
            os.makedirs(cleanup_apply.JOURNAL_DIR, exist_ok=True)
            with open(journal_path, "w", encoding="utf-8") as f:
                json.dump({"root": root, "created": stamp, "kind": "artwork", "albums": [k for k, _, _ in plan],
                           "backup": backup_dir, "stats": stats, "entries": entries}, f, ensure_ascii=False, indent=1)
        conn.close()
    return {"stats": dict(stats), "journal": journal_path if entries else None}
