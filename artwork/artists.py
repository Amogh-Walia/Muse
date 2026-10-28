"""Artist pictures: find one (Deezer, Wikimedia Commons via MusicBrainz and Wikidata, or the user's drive) and
save it as artist.jpg in the artist's folder (Artist/, above their albums), where Navidrome looks for it.

Candidates are kept with the album covers' (crud.artwork), under a key no album can have, so uploads, the
image cache and /art/<sha> work the same. Saving is journaled: Undo puts back the picture that was there.
"""
import collections
import os
import urllib.parse

import config
import crud
from musicbrainz import client as mb_client, match

from . import images, sources
from .service import _Journal

PREFIX = "\x01artist\x00"  # candidate keys: album keys are "folder\x00album", so these never clash
PICTURE_FILES = ("artist.jpg", "artist.jpeg", "artist.png", "artist.webp")
ONLINE_SOURCES = ("deezer", "wikimedia")


def key(name):
    return PREFIX + name


def is_key(candidate_key):
    return candidate_key.startswith(PREFIX)


def folder(conn, name):
    """The artist's own folder (where their albums are), or None when they have no albums of their own."""
    tops = collections.Counter(f.split("/")[0] for f in crud.tracks.album_folders(conn, name)
                               if "/" in f and not (config.INBOX and f.startswith(config.INBOX + "/")))
    return tops.most_common(1)[0][0] if tops else None


def picture_file(root, artist_folder):
    """The artist picture already in their folder (file name), if any."""
    full = os.path.join(root, artist_folder or "")
    if not artist_folder or not os.path.isdir(full):
        return None
    return next((n for n in sorted(os.listdir(full)) if n.lower() in PICTURE_FILES), None)


def _store(conn, k, source, label, data, rank):
    try:
        sha, mime, width, height = images.store(data)
    except images.NotAnImage:
        return
    crud.artwork.add_candidate(conn, k, source, label, sha, mime, width, height, len(data), None, rank)


def state(conn, root, name):
    """An artist's picture: their folder, the picture there now (also offered as a candidate) and candidates."""
    artist_folder = folder(conn, name)
    current = picture_file(root, artist_folder)
    if current:
        with open(os.path.join(root, artist_folder, current), "rb") as f:
            _store(conn, key(name), "folder", f"Now: {current}", f.read(), 0)
        conn.commit()
    return {"artist": name, "folder": artist_folder, "current": current, "candidates": crud.artwork.candidates(conn, key(name))}


def _mbid(conn, name):
    """The artist's MusicBrainz id: from the library's tags, else a confident search."""
    for spelled, artist_id in crud.tracks.album_artists(conn):
        if artist_id and ";" not in artist_id and spelled.lower() == name.lower():
            return artist_id
    found = (mb_client._get("artist/", {"query": f'artist:"{mb_client._quote(name)}"', "limit": 3}) or {}).get("artists", [])
    best = next((a for a in found if int(a.get("score", 0)) >= 95 and match.similarity(a["name"], name) >= 0.9), None)
    return best and best["id"]


def find(db_path, name, progress=None):
    """Look online for pictures of `name`: Deezer's artist photos, and the artist's image on Wikimedia Commons."""
    progress = progress or (lambda done, total, message: None)
    conn = crud.connect(db_path)
    k, errors = key(name), []
    try:
        crud.artwork.clear_candidates(conn, k, ONLINE_SOURCES)
        progress(0, 2, "Deezer…")
        try:
            found = sources._download("https://api.deezer.com/search/artist?" + urllib.parse.urlencode({"q": name, "limit": 5}),
                                      as_json=True) or {}
            for a in found.get("data", []):
                url = a.get("picture_xl") or ""
                # Deezer's stand-in picture for artists without one has no image id in its address
                if match.similarity(a.get("name") or "", name) < 0.85 or not url or "/artist//" in url:
                    continue
                data = sources._download(url)
                if data:
                    _store(conn, k, "deezer", f"Deezer: {a['name']} ({a.get('nb_fan', 0):,} fans)", data,
                           0 if (a.get("name") or "").lower() == name.lower() else 10)
        except mb_client.MusicBrainzError as e:
            errors.append(f"Deezer: {e}")
        progress(1, 2, "Wikimedia Commons…")
        try:
            artist_id = _mbid(conn, name)
            artist = artist_id and mb_client._get(f"artist/{artist_id}", {"inc": "url-rels"})
            links = [r["url"]["resource"] for r in (artist or {}).get("relations", []) if r.get("type") == "wikidata"]
            if links:
                item = links[0].rstrip("/").split("/")[-1]
                entity = sources._download(f"https://www.wikidata.org/wiki/Special:EntityData/{item}.json", as_json=True) or {}
                claims = entity.get("entities", {}).get(item, {}).get("claims", {}).get("P18", [])
                if claims:
                    file = claims[0]["mainsnak"]["datavalue"]["value"]
                    data = sources._download("https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(file) + "?width=1200")
                    if data:
                        _store(conn, k, "wikimedia", f"Wikimedia Commons: {file}", data, 5)
        except (mb_client.MusicBrainzError, KeyError, IndexError) as e:
            errors.append(f"Wikimedia: {e}")
        conn.commit()
        progress(2, 2, "Done")
        return {"artist": name, "found": len([c for c in crud.artwork.candidates(conn, k) if c["source"] in ONLINE_SOURCES]),
                "errors": errors}
    finally:
        conn.close()


def set_picture(db_path, name, sha):
    """Save image `sha` as the artist's picture (artist.jpg/png in their folder), setting aside what was there."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    try:
        artist_folder = folder(conn, name)
        if not artist_folder:
            raise ValueError(f"{name} has no albums of their own, so no folder to keep a picture in")
        try:
            data = images.load(sha)
            mime = images.probe(data)[0]
        except (OSError, images.NotAnImage):
            raise ValueError("that picture isn't available any more; pick it again") from None
    finally:
        conn.close()
    journal = _Journal()
    try:
        full = os.path.join(root, artist_folder)
        for existing in [n for n in os.listdir(full) if n.lower() in PICTURE_FILES]:
            rel = os.path.join(artist_folder, existing)
            with open(os.path.join(root, rel), "rb") as f:
                journal.entries.append({"op": "removed", "path": rel, "backup": journal.keep(f.read())})
            os.remove(os.path.join(root, rel))
        rel = os.path.join(artist_folder, "artist.png" if mime == "image/png" else "artist.jpg")
        with open(os.path.join(root, rel), "wb") as f:
            f.write(data)
        journal.entries.append({"op": "created", "path": rel})
        journal.stats["artist_pictures"] += 1
    finally:
        path = journal.write(root)
    return {"artist": name, "file": rel, "journal": path}
