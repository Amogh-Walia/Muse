"""Album art operation: find cover candidates for albums whose songs lack embedded art, then embed the chosen ones.

    find    gathers candidates: image files in the album folder, art embedded in a sibling song, and
            (online) the Cover Art Archive via MusicBrainz and optionally iTunes
    upload  adds an image picked from the user's drive as a candidate
    apply   embeds the chosen cover into the album's songs, journaled so the cleanup's Undo reverts it
    set_artwork  puts a cover on one album, or on chosen songs of it (the album page's Edit artwork)
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
        from . import artists
        if key not in _groups(conn) and not artists.is_key(key):
            raise LookupError("no such album")
        sha, mime, width, height = images.store(data)
        crud.artwork.add_candidate(conn, key, "upload", f"From your drive: {os.path.basename(name or 'image')}",
                                   sha, mime, width, height, len(data), None, 0)
        conn.commit()
        return {"sha": sha, "mime": mime, "width": width, "height": height}
    finally:
        conn.close()


class _Journal:
    """One artwork change: its undo entries, the pictures it replaced (kept for undo) and counts."""

    def __init__(self):
        self.stamp = cleanup_apply.journal_stamp()
        self.backup_dir = f"{self.stamp}-artwork-backup"
        self.entries, self.stats, self.albums = [], collections.Counter(), []

    def keep(self, data):
        """Save bytes for undo; returns their name in the journal folder."""
        os.makedirs(os.path.join(cleanup_apply.JOURNAL_DIR, self.backup_dir), exist_ok=True)
        name = os.path.join(self.backup_dir, f"{len(self.entries)}-{self.stats['kept']}.bin")
        self.stats["kept"] += 1
        with open(os.path.join(cleanup_apply.JOURNAL_DIR, name), "wb") as f:
            f.write(data)
        return name

    def write(self, root):
        self.stats.pop("kept", None)
        if not self.entries:
            return None
        os.makedirs(cleanup_apply.JOURNAL_DIR, exist_ok=True)
        path = os.path.join(cleanup_apply.JOURNAL_DIR, f"{self.stamp}-artwork.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"root": root, "created": self.stamp, "kind": "artwork", "albums": self.albums, "backup": self.backup_dir,
                       "stats": self.stats, "entries": self.entries}, f, ensure_ascii=False, indent=1)
        return path


def _embed_songs(conn, root, songs, image, replace, journal, progress, done=0, total=0):
    """Embed `image` (data, mime, width, height) in each song, journaling the pictures it had."""
    data, mime, width, height = image
    for song in songs:
        progress(done, total, song["path"])
        done += 1
        full = os.path.join(root, song["path"])
        if not os.path.exists(full):
            journal.stats["skipped"] += 1
            continue
        size = os.path.getsize(full)
        try:
            before = embed.embed(full, data, mime, width, height, replace=replace)
        except ValueError:  # format without picture support
            journal.stats["unsupported"] += 1
            continue
        saved = [{**{k: v for k, v in p.items() if k != "data"}, "file": journal.keep(p["data"])} for p in before]
        journal.entries.append({"op": "art", "path": song["path"], "pictures": saved, "size": size})
        scan.refresh(conn, root, song["path"])
        conn.commit()
        journal.stats["songs"] += 1
    return done


def _cover_file(root, folder, image, journal, replace):
    """Give the folder a cover file with `image`. With `replace`, cover files already there are set aside
    (kept for undo) first, so players that prefer the folder's file (Navidrome does) show the new one."""
    data, mime = image[0], image[1]
    full = os.path.join(root, folder)
    if not folder or not os.path.isdir(full):
        return
    existing = [n for n in os.listdir(full) if n.lower() in COVER_FILES]
    if existing and not replace:
        return
    for name in existing:
        rel = os.path.join(folder, name)
        with open(os.path.join(root, rel), "rb") as f:
            journal.entries.append({"op": "removed", "path": rel, "backup": journal.keep(f.read())})
        os.remove(os.path.join(root, rel))
    name = "cover.png" if mime == "image/png" else "cover.jpg"
    with open(os.path.join(full, name), "wb") as f:
        f.write(data)
    journal.entries.append({"op": "created", "path": os.path.join(folder, name)})
    journal.stats["cover_files"] += 1


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
    journal, done = _Journal(), 0
    try:
        for key, cand, targets in plan:
            image = images.for_embedding(images.load(cand["sha"]))
            done = _embed_songs(conn, root, targets, image, replace, journal, progress, done, total)
            if save_folder:
                _cover_file(root, groups[key][0]["folder"], image, journal, replace=False)
            crud.artwork.record(conn, key, groups[key][0]["folder"], groups[key][0]["album"], cand, len(targets))
            journal.albums.append(key)
            conn.commit()
            journal.stats["albums"] += 1
    finally:
        conn.commit()
        path = journal.write(root)
        conn.close()
    return {"stats": dict(journal.stats), "journal": path}


# --- editing one album's artwork (the album page's Edit artwork) ------------------------

EMBEDDABLE = (".mp3", ".flac", ".m4a", ".mp4", ".m4b", ".ogg", ".oga", ".opus")  # formats embed.py can write


def _folder_songs(conn, folder):
    songs = [s for (f, _), group in crud.tracks.by_album(conn).items() if f == folder for s in group]
    if not songs:
        raise LookupError("no songs in that folder")
    return songs


def folder_state(conn, root, folder):
    """One album folder's artwork for the Edit artwork dialog: each song's art, the folder's cover file, and
    the candidates. The folder's own images and every different picture in its songs are gathered now
    (local, quick); online ones come from a search."""
    songs = _folder_songs(conn, folder)
    album, artist = mb_service.album_facts(songs)
    key = mb_service.album_key(folder, album)
    crud.artwork.clear_candidates(conn, key, LOCAL_SOURCES)
    _store(conn, key, "folder", sources.folder_images(root, folder))
    _store(conn, key, "embedded", sources.sibling_art(root, songs))
    conn.commit()
    full = os.path.join(root, folder)
    cover_file = next((n for n in sorted(os.listdir(full)) if n.lower() in COVER_FILES), None) if os.path.isdir(full) else None
    return {"key": key, "folder": folder, "album": album, "artist": artist, "cover_file": cover_file,
            "songs": [{"id": s["id"], "path": s["path"], "title": s["title"] or s["filename"], "disc_no": s["disc_no"],
                       "track_no": s["track_no"], "has_cover": bool(s["has_cover"]),
                       "can_embed": os.path.splitext(s["path"])[1].lower() in EMBEDDABLE} for s in songs],
            "candidates": crud.artwork.candidates(conn, key)}


def set_artwork(db_path, folder, sha, paths=None, progress=_no_progress):
    """Put image `sha` on an album folder: every song and the folder's cover file (replacing what's there),
    or, with `paths`, only those songs. Journaled: Undo puts back exactly the pictures and files there were."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    songs = _folder_songs(conn, folder)
    targets = songs if paths is None else [s for s in songs if s["path"] in set(paths)]
    if not targets:
        raise ValueError("pick at least one song")
    try:
        image = images.for_embedding(images.load(sha))
    except (OSError, images.NotAnImage):
        raise ValueError("that image isn't available any more; pick it again") from None
    journal = _Journal()
    try:
        _embed_songs(conn, root, targets, image, True, journal, progress, 0, len(targets))
        if paths is None:  # the whole album: its folder cover too
            _cover_file(root, folder, image, journal, replace=True)
            album, _ = mb_service.album_facts(songs)
            key = mb_service.album_key(folder, album)
            cand = next((c for c in crud.artwork.candidates(conn, key) if c["sha"] == sha), None)
            if cand:
                crud.artwork.record(conn, key, folder, album, cand, len(targets))
                journal.albums.append(key)
        progress(len(targets), len(targets), "Done")
    finally:
        conn.commit()
        path = journal.write(root)
        conn.close()
    return {"stats": dict(journal.stats), "journal": path, "whole_album": paths is None}
