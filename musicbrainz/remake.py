"""Remake Various Artists: give the songs of a playlist-style album their own albums.

Albums like Apple Music's "… Essentials" playlists carry the right title and artist on each song but a
made-up album. Each song is looked up on its own: by its ISRC (a code for the recording, in most
store-bought files), or else by title, artist and length. Every release the recording is on becomes an
option, ranked:

  0. the album its title says it's from ('Song (From "Film")'): that's the soundtrack it belongs to
  1. a release (or an album of the same name) already in the library: the song joins that album
  2. not a compilation, live album or remix
  3. official, and an album before an EP or single
  4. the earliest: where the song first came out, not a later re-release
  5. shared with the other songs looked up together, so songs from one film land on one soundtrack

A song can also become a single of its own (cleanup/singles.py): the default for songs MusicBrainz doesn't
have, and a choice for every song. A song whose best release is itself a single gets that release's tags, which
say so.

The Bollywood degreaser (mode "degrease") runs the same lookup on a different set of songs: those whose title
or file name says what album they're from ('Song (From "Film")'), wherever they are. Each goes to that film's
album when MusicBrainz has it, or else just has the "(From …)" taken out of its title ("tidy"), which Clean up's
renaming then takes out of the file name. Either way it stops showing up.

Applying writes the release's tags (an album already in the library lends its own album name, album
artist and dates, so the song files alongside it), takes out the songs' artwork (the playlist's cover, not
their album's: without art they show up in Album art to get the real one), moves the songs into place like
Clean up, and moves what's left of an emptied playlist folder to the trash. One journal: one Undo reverses it,
pictures included.
"""
import collections
import json
import os
import re

import crud
import scan
from artwork import embed
from cleanup import apply as applier, merge, rules, singles, tags

from . import client, match

PLAYLIST_ID = re.compile(r"^pl\.", re.I)  # Apple Music's id for a playlist, where an album id should be
PLAYLIST_NAME = re.compile(r"\b(essentials|playlist|radio hits)\b", re.I)
NOT_ORIGINAL = {"Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street", "Karaoke", "Demo"}
TYPE_ORDER = {"Album": 0, "EP": 1, "Single": 2}
MAX_RECORDINGS = 3  # recordings per ISRC to follow (normally one)
MAX_OPTIONS = 8  # release groups offered per song


def _no_progress(done, total, message):
    pass


def album_key(folder, album):
    return f"{folder}\x00{album or ''}"


def _norm_album(name):
    return match.simplify_album(rules.clean_album(name or "")).lower()


def _norm_title(name):
    return rules.clean_title(name or "")[0].lower()


def from_hint(song):
    """The album a song's title (or else its file name) says it's from: 'Song (From "Film")'."""
    return rules.from_album(song["title"] or "")[1] or rules.from_album(os.path.splitext(song["filename"] or "")[0])[1]


def _names(o, hint):
    """Whether release option `o` is the album `hint` names."""
    return bool(hint) and match.similarity(_norm_album(o["title"]), _norm_album(hint)) >= 0.85


def degrease_albums(conn, groups=None):
    """Albums holding songs whose title or file name says what album they're from. All come pre-selected, and
    (like playlists) they aren't albums for other songs to join."""
    groups = groups or crud.tracks.by_album(conn)
    out = []
    for (folder, _), songs in groups.items():
        named = [s["id"] for s in songs if from_hint(s)]
        if named:
            album = songs[0]["album"] or ""
            out.append({"key": album_key(folder, album), "folder": folder, "album": album, "songs": len(songs), "playlist": True,
                        "reason": "1 of its songs says what it's from" if len(named) == 1 else f"{len(named)} of its songs say what they're from",
                        "only": named})
    return sorted(out, key=lambda a: a["folder"].lower())


def albums_for(conn, mode, groups=None):
    return degrease_albums(conn, groups) if mode == "degrease" else candidates(conn, groups)


def candidates(conn, groups=None):
    """Albums that can be remade: anything filed as "Various Artists" that isn't linked to MusicBrainz, and
    anything that looks like a playlist. Playlists come pre-selected."""
    groups = groups or crud.tracks.by_album(conn)
    itunes = crud.tracks.tag_lookup(conn, ("itunesalbumid",))
    out = []
    for (folder, _), songs in groups.items():
        album = songs[0]["album"] or ""
        artist = collections.Counter(s["album_artist"] or "" for s in songs).most_common(1)[0][0]
        # a song linked to its MusicBrainz release has been sorted out, whatever playlist id it still carries
        playlist_id = any(PLAYLIST_ID.match(itunes.get(s["id"], {}).get("itunesalbumid", "")) and not s["mb_album_id"] for s in songs)
        playlist = playlist_id or bool(PLAYLIST_NAME.search(album))
        linked = all(s["mb_album_id"] for s in songs)
        if not playlist and not (rules.is_various(artist) and not linked):
            continue
        out.append({"key": album_key(folder, album), "folder": folder, "album": album, "songs": len(songs),
                    "playlist": playlist, "reason": "Apple Music playlist" if playlist_id else
                    "named like a playlist" if playlist else "Various Artists, not on MusicBrainz"})
    return sorted(out, key=lambda a: (not a["playlist"], a["folder"].lower()))


# --- looking songs up -------------------------------------------------------------------

def _release_options(releases, recording_id):
    out = []
    for rel in releases:
        group = rel.get("release-group") or {}
        out.append({"release_id": rel["id"], "title": rel.get("title") or "", "date": rel.get("date") or "",
                    "status": rel.get("status"), "type": group.get("primary-type"), "secondary": group.get("secondary-types") or [],
                    "group": group.get("id"), "recording": recording_id})
    return out


def _options_for(song, isrc):
    """Every release the song's recording is on, and how it was found. With no ISRC match, the title search
    includes the album the song says it's from."""
    if isrc:
        options = []
        for rec in client.recordings_by_isrc(isrc)[:MAX_RECORDINGS]:
            options += _release_options((client.get_recording(rec["id"]) or {}).get("releases", []), rec["id"])
        if options:
            return options, "ISRC"
    # No ISRC, or MusicBrainz doesn't know it: search by title and artist (and the album the title says it's
    # from, if it says), and check the length.
    title, from_album = rules.from_album(rules.clean_title(song["title"] or "")[0])
    from_album = from_album or from_hint(song)
    options = []
    found = client.search_recordings(title, song["artist"], release=from_album) if from_album else []
    for rec in found or client.search_recordings(title, song["artist"]):
        if match.similarity(title, rec.get("title") or "") < 0.85:
            continue
        if song["duration"] and rec.get("length") and abs(rec["length"] / 1000 - song["duration"]) > 8:
            continue
        options += _release_options(rec.get("releases", []), rec["id"])
    return options, "title, artist and length"


def lookup(db_path, keys, refresh=False, skip=(), finished=None, progress=_no_progress, mode="remake"):
    """Look up every song of the albums `keys` (stored as it goes; stoppable). Songs already looked up are
    skipped unless `refresh`. Degreasing looks up only the songs that say where they're from."""
    conn = crud.connect(db_path)
    groups = crud.tracks.by_album(conn)
    only = None
    if mode == "degrease":
        only = {t for a in degrease_albums(conn, groups) for t in a["only"]}
    wanted = set()
    for k in keys:
        folder, sep, album = k.partition("\x00")
        if not sep:
            raise ValueError("an album was picked from an out-of-date page: reload it and pick again")
        wanted.add((folder, album.lower()))
    codes = crud.tracks.tag_lookup(conn, ("isrc", "tsrc"))
    known = crud.remake.songs(conn)
    todo = [s for g in wanted for s in groups.get(g, []) if (refresh or s["id"] not in known) and s["id"] not in skip
            and (only is None or s["id"] in only)]
    counts = collections.Counter()
    try:
        for i, song in enumerate(todo):
            progress(i, len(todo), f"{song['artist'] or ''} - {song['title'] or ''}")
            isrc = (codes.get(song["id"], {}).get("isrc") or codes.get(song["id"], {}).get("tsrc") or "").strip().upper()
            try:
                options, how = _options_for(song, isrc)
                status = "found" if options else "none"
            except client.MusicBrainzError as e:
                options, how, status = [], str(e), "error"
            crud.remake.save_song(conn, song["id"], isrc or None, status, how, options)
            conn.commit()
            counts[status] += 1
            if finished is not None:
                finished.append(song["id"])
    finally:
        conn.close()
    progress(len(todo), len(todo), "Done")
    return {"songs": len(todo), **counts}


# --- reviewing: where each song would go ------------------------------------------------

def _option_label(o):
    kind = ", ".join(t for t in [o["type"]] + o["secondary"] if t) or "release"
    return f"{o['title']} ({o['date'][:4] or 'no date'}, {kind.lower()})"


def state(conn, root=None, mode="remake"):
    """The albums that can be remade (or degreased) and, for songs looked up, their options, the chosen release,
    and what choosing it means: joining an album already in the library, a duplicate there, or two songs remade
    into the same place."""
    groups = crud.tracks.by_album(conn)
    looked = crud.remake.songs(conn)
    albums = albums_for(conn, mode, groups)
    sources = {a["folder"] for a in albums if a["playlist"]}  # playlists aren't albums to join; real ones listed here are
    by_folder = collections.defaultdict(list)
    for (folder, _), songs in groups.items():
        by_folder[folder] += songs
    # the library outside the albums being remade: release groups it has, and album names
    in_library = {group: (folder, release) for folder, found in crud.releases.release_groups_by_folder(conn).items()
                  if folder not in sources for group, release in found.items()}
    names = collections.defaultdict(list)
    for folder, songs in by_folder.items():
        if folder not in sources:
            names[_norm_album(songs[0]["album"])].append(folder)
    shared = collections.Counter(g for r in looked.values() for g in {o["group"] for o in r["options"] if o["group"]})

    def joins(o):
        if o["group"] in in_library:
            return in_library[o["group"]][0], "the same release"
        folders = names.get(_norm_album(o["title"]), [])
        year = o["date"][:4]
        same_year = [f for f in folders if not year or any(str(s["year"] or "") == year for s in by_folder[f])]
        return (same_year or folders or [None])[0], "the same album name" if folders else None

    def rank(o, from_album=None):
        library_release = in_library.get(o["group"], (None, None))[1]
        return (not _names(o, from_album), joins(o)[0] is None, bool(NOT_ORIGINAL & set(o["secondary"])), o["status"] != "Official",
                TYPE_ORDER.get(o["type"], 3), bool(library_release) and o["release_id"] != library_release,
                o["date"] or "9999", -shared[o["group"]], o["title"])

    claims = collections.defaultdict(list)
    out = []
    for album in albums:
        rows = []
        for song in by_folder[album["folder"]]:
            if (song["album"] or "") != album["album"] or ("only" in album and song["id"] not in album["only"]):
                continue
            r = looked.get(song["id"])
            hint = from_hint(song)  # 'Song (From "Film")': the film's album first
            row = {"id": song["id"], "path": song["path"], "title": song["title"], "artist": song["artist"],
                   "status": r["status"] if r else None, "how": r["how"] if r else None, "options": [], "chosen": None, "target": None,
                   "from": hint}
            best_per_group = []
            if r and r["options"]:
                seen = set()
                for o in sorted(r["options"], key=lambda o: rank(o, hint)):  # one edition per release group: the best one
                    if (o["group"] or o["release_id"]) not in seen:
                        seen.add(o["group"] or o["release_id"])
                        best_per_group.append(o)
                row["options"] = [{"release_id": o["release_id"], "label": _option_label(o)} for o in best_per_group[:MAX_OPTIONS]]
            choice = r["chosen"] if r else None
            if r and choice == "keep":  # left as it is, found or not
                row.update(chosen="keep", auto=False)
            elif hint and (choice == "tidy" or (choice is None and mode == "degrease"
                                                and not (best_per_group and _names(best_per_group[0], hint)))):
                # keep its album, take the "(From …)" out: chosen, or degreasing's default when the film isn't found
                row.update(chosen="tidy", auto=choice is None,
                           target={"tidy": True, "album": song["album"], "label": "its album as it is, title tidied",
                                   "title": rules.from_album(song["title"] or "")[0], "from": hint,
                                   "joins": None, "joins_how": None, "duplicate": None, "clashes_with": []})
            elif r and (choice == "single" or (choice is None and r["status"] == "none")):
                # a single of its own: chosen, or the default for a song MusicBrainz doesn't have
                row.update(chosen="single", auto=choice is None,
                           target={"single": True, "album": rules.clean_title(song["title"] or "")[0], "label": "a single of its own",
                                   "joins": None, "joins_how": None, "duplicate": None, "clashes_with": []})
            elif r and r["options"]:
                picked = None if choice == "keep" else next((o for o in r["options"] if o["release_id"] == choice), best_per_group[0])
                row["chosen"] = "keep" if choice == "keep" else picked["release_id"]
                row["auto"] = not choice
                if picked:
                    folder, how = joins(picked)
                    duplicate = next((s["path"] for s in by_folder.get(folder, []) if _norm_title(s["title"]) == _norm_title(song["title"])), None)
                    row["target"] = {"release_id": picked["release_id"], "recording": picked["recording"], "album": picked["title"],
                                     "year": picked["date"][:4], "label": _option_label(picked), "joins": folder, "joins_how": how,
                                     "duplicate": duplicate, "type": picked["type"]}
                    claims[(folder or picked["group"], _norm_title(song["title"]))].append(row)
            rows.append(row)
        if rows:
            out.append({**album, "looked_up": sum(1 for x in rows if x["status"]), "songs": rows})
    for rows in claims.values():  # two songs remade into the same album under the same title
        for row in rows:
            if row["target"].get("single") or row["target"].get("tidy"):
                continue
            row["target"]["clashes_with"] = [x["title"] for x in rows if x is not row] if len(rows) > 1 else []
    return {"albums": out}


def choose(conn, track_id, choice):
    crud.remake.choose(conn, track_id, None if choice in (None, "", "auto") else choice)


# --- applying -----------------------------------------------------------------------------

def _strip_art(root, path, backup, entries):
    """Take a remade song's pictures out, keeping them (under the journal's `backup` folder) for undo.
    Returns whether there were any."""
    full = os.path.join(root, path)
    try:
        before = embed.snapshot(full)
    except ValueError:  # a format without pictures
        return False
    if not before:
        return False
    os.makedirs(os.path.join(applier.JOURNAL_DIR, backup), exist_ok=True)
    saved = []
    for n, picture in enumerate(before):
        name = os.path.join(backup, f"{len(entries)}-{n}.bin")
        with open(os.path.join(applier.JOURNAL_DIR, name), "wb") as f:
            f.write(picture["data"])
        saved.append({**{k: v for k, v in picture.items() if k != "data"}, "file": name})
    size = os.path.getsize(full)
    embed.restore(full, [])
    entries.append({"op": "art", "path": path, "pictures": saved, "size": size})
    return True


def _summary(conn, release_id):
    """A release's full details (track list included), from the cache or MusicBrainz."""
    summary = crud.remake.release(conn, release_id)
    if not summary:
        release = client.get_release(release_id)
        if not release:
            raise LookupError(f"MusicBrainz no longer has release {release_id}")
        summary = match.release_summary(release)
        crud.remake.save_release(conn, release_id, summary)
        conn.commit()
    return summary


def apply(db_path, track_ids, progress=_no_progress, mode="remake"):
    """Remake (or degrease) the songs `track_ids` as chosen, then move them into place."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    rows = {row["id"]: (album, row) for album in state(conn, root, mode)["albums"] for row in album["songs"]}
    work = [rows[t] for t in track_ids if t in rows and rows[t][1]["target"]]
    by_folder = collections.defaultdict(list)
    for (folder, _), songs in crud.tracks.by_album(conn).items():
        by_folder[folder] += songs
    entries, stats, used, done = [], collections.Counter(), {}, []
    # An artist already in the library keeps its spelling, found by MusicBrainz id or by name, so remade songs
    # don't open a second folder for it ("Vishal & Shekhar" next to "Vishal-Shekhar").
    by_id, by_name = {}, {}
    for name, artist_id in crud.tracks.album_artists(conn):
        if artist_id and ";" not in artist_id:  # one artist, not a joint credit
            by_id.setdefault(artist_id.lower(), name)
        by_name.setdefault(rules.normalize(name).lower(), name)

    def album_artist(summary):
        """The release's main (first) artist, as the library already spells it if it's there."""
        ids, names = summary.get("artist_ids") or [], summary.get("artist_names") or []
        if ids and ids[0].lower() in by_id:
            return by_id[ids[0].lower()]
        name = rules.normalize(names[0] if names else summary["artist"])
        name = by_name.setdefault(name.lower(), name)
        if ids:
            by_id[ids[0].lower()] = name  # later releases crediting this artist differently use the same spelling
        return name
    released = crud.tracks.tag_lookup(conn, ("releasetime",))
    stamp = applier.journal_stamp()
    backup = f"{stamp}-{mode}-backup"  # the pictures taken out of remade songs, for undo
    try:
        for i, (album, row) in enumerate(work):
            target = row["target"]
            progress(i, len(work) + 1, f"{row['title']} → {target['album']}")
            if target.get("tidy"):  # keep its album; "(From …)" out of the title (and, moving into place, the file name)
                full = os.path.join(root, row["path"])
                if target["title"] and target["title"] != row["title"]:
                    size = os.path.getsize(full)
                    entries.append({"op": "tags", "path": row["path"], "before": tags.write(full, {"title": [target["title"]]}), "size": size})
                    scan.refresh(conn, root, row["path"])
                    conn.commit()
                done.append(row)
                stats["tidied"] += 1
                continue
            if target.get("single"):  # its own single: one-song release by its main artist (cleanup/singles.py)
                song = next(s for s in by_folder[album["folder"]] if s["id"] == row["id"])
                values = singles.single_values(song, released.get(song["id"], {}).get("releasetime"))
                if values.get("albumartist"):
                    name = rules.normalize(values["albumartist"][0])
                    values["albumartist"] = [by_name.setdefault(name.lower(), name)]
                full = os.path.join(root, row["path"])
                size = os.path.getsize(full)
                entries.append({"op": "tags", "path": row["path"], "before": tags.write(full, values), "size": size})
                stats["art_removed"] += _strip_art(root, row["path"], backup, entries)
                scan.refresh(conn, root, row["path"])
                conn.commit()
                done.append(row)
                stats["singles"] += 1
                continue
            summary = used.get(target["release_id"]) or _summary(conn, target["release_id"])
            used[target["release_id"]] = summary
            track = next((t for t in summary["tracks"] if t["recording"] == target["recording"]), None)
            if track is None:  # found by search: pair by title instead
                song = next(s for s in by_folder[album["folder"]] if s["id"] == row["id"])
                pairs = match.pair_tracks([song], summary["tracks"])[0]
                track = pairs[0][1] if pairs else None
            if track is None:
                stats["not_on_release"] += 1
                continue
            values = match.tag_values(summary, track)
            # on the album it says it's from, the title doesn't need to say so any more
            values["title"] = [rules.from_album(v)[0] for v in values.get("title", [])]
            if summary["artist"]:
                values["albumartist"] = [album_artist(summary)]
            if target["joins"]:
                # an album already in the library: file alongside it under its own name, artist and dates, even
                # when it's the same release (its tags may say "Parvana EP" where MusicBrainz says "Parvana")
                existing = merge.album_values(root, by_folder[target["joins"]])
                for field in ("album", "albumartist", "date", "originaldate"):
                    if existing[field]:
                        values[field] = existing[field]
            full = os.path.join(root, row["path"])
            size = os.path.getsize(full)
            before = tags.write(full, values)
            entries.append({"op": "tags", "path": row["path"], "before": before, "size": size})
            # the playlist's picture goes: without art, the song's real album shows up in Album art
            stats["art_removed"] += _strip_art(root, row["path"], backup, entries)
            scan.refresh(conn, root, row["path"])
            conn.commit()
            done.append(row)
            stats["remade"] += 1
            stats["joined"] += bool(target["joins"])
        for summary in used.values():
            crud.releases.upsert(conn, summary)
        crud.releases.recompute(conn)
        conn.commit()
        progress(len(work), len(work) + 1, "Moving the songs into place…")
        stats["moved"] = applier.move_into_place(conn, root, [row["path"] for row in done], entries)
        # playlist folders left without songs: what's left (the playlist's cover and so on) goes to the trash
        for folder in {album["folder"] for album, _ in work}:
            stats["folders_emptied"] += applier.trash_leftovers(root, folder, stamp, entries)
    finally:
        conn.commit()
        journal = None
        if entries:
            os.makedirs(applier.JOURNAL_DIR, exist_ok=True)
            journal = os.path.join(applier.JOURNAL_DIR, f"{stamp}-{mode}.json")
            with open(journal, "w", encoding="utf-8") as f:
                json.dump({"root": root, "created": stamp, "kind": mode, "stats": stats, "entries": entries,
                           **({"backup": backup} if stats["art_removed"] else {})}, f, ensure_ascii=False, indent=1)
        conn.close()
    return {"stats": dict(stats), "journal": journal,
            "clashes": sum(1 for _, row in work if row["target"]["duplicate"] or row["target"].get("clashes_with"))}
