"""Work out every tag fix and rename for a library without changing anything."""
import collections
import os
from concurrent.futures import ThreadPoolExecutor

from scan import AUDIO_EXTS, TRASH_DIR

from . import rules, tags

LABELS = {"title": "Title", "artist": "Artist", "artists": "All artists", "album": "Album",
          "albumartist": "Album artist", "date": "Date", "path": "File"}


def _first(values):
    return values[0] if values else ""


def _num(values):
    head = _first(values).partition("/")[0].strip()
    return int(head) if head.isdigit() else None


def _total(values):
    tail = _first(values).partition("/")[2].strip()
    return int(tail) if tail.isdigit() else None


def _read(path):
    try:
        return tags.read(path)
    except Exception as e:  # unreadable/corrupt file: report it, don't abort the plan
        return e


def _canonical_case(names):
    """Most common spelling per lowercase name: 'System of a Down' vs 'System Of A Down'."""
    best = {}
    for name, _ in collections.Counter(n for n in names if n).most_common():
        best.setdefault(name.lower(), name)
    return lambda name: best.get(name.lower(), name) if name else name


def _album_artist(values):
    """One person for the album-artist tag, or 'Various Artists' for compilations."""
    people = rules.split_artists(values)
    if not people:
        return None
    if len(people) > 2 or rules.is_various(people[0]):
        return "Various Artists"
    return people[0]


def build(root):
    root = os.path.abspath(root)
    paths, busy = [], set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != TRASH_DIR)
        rel_dir = os.path.relpath(dirpath, root)
        if any(n.endswith(".part") for n in filenames):
            busy.add("" if rel_dir == "." else rel_dir)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                paths.append(os.path.relpath(os.path.join(dirpath, name), root))

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        raw = list(pool.map(lambda p: _read(os.path.join(root, p)), paths))

    errors, tracks = [], []
    for path, t in zip(paths, raw):
        if isinstance(t, Exception):
            errors.append({"path": path, "error": str(t)})
            continue
        title, featured = rules.clean_title(_first(t["title"]) or os.path.splitext(os.path.basename(path))[0])
        credited = rules.split_artists(t["artist"])
        everyone = rules.dedupe(credited + featured + rules.split_artists(t["artists"]))
        tracks.append({
            "path": path, "folder": os.path.dirname(path), "old": t,
            "title": title, "main": everyone[0] if everyone else None, "everyone": everyone,
            "album": rules.clean_album(_first(t["album"])) if t["album"] else None,
            "albumartist": _album_artist(t["albumartist"]),
            "date": rules.clean_date(_first(t["date"])) if t["date"] else None,
            "year": rules.year_of(_first(t["originaldate"])) or rules.year_of(_first(t["date"])),
            "track": _num(t["tracknumber"]),
            "disc": _num(t["discnumber"]),
            "discs": _total(t["discnumber"]) or _num(t["disctotal"]) or _num(t["totaldiscs"]),
            "release": (_first(t["musicbrainz_albumid"]) or "").lower() or None,
        })

    # Songs on one MusicBrainz release are one album, whatever their album tags say: a store's "Parvana EP"
    # and MusicBrainz's "Parvana" would otherwise become two folders. They take the name, album artist and
    # year of the bigger album they're spread over (counting its songs without MusicBrainz ids too).
    by_release = collections.defaultdict(list)
    for t in tracks:
        if t["release"]:
            by_release[t["release"]].append(t)
    album_size = collections.Counter((t["folder"], t["album"]) for t in tracks)
    for members in by_release.values():
        parts = {(t["folder"], t["album"]) for t in members}
        if len(parts) < 2:
            continue
        name = max(parts, key=lambda part: (album_size[part], part[1] or ""))[1]
        artists = collections.Counter(t["albumartist"] for t in members if t["albumartist"])
        years = [t["year"] for t in members if t["year"]]
        for t in members:
            if name:
                t["album"] = name
            if artists:
                t["albumartist"] = artists.most_common(1)[0][0]
            if years:
                t["year"] = min(years)

    case = _canonical_case([t["main"] for t in tracks] + [t["albumartist"] for t in tracks])

    # Album-level decisions, so one release never ends up split across artists or years.
    groups = collections.defaultdict(list)
    for t in tracks:
        groups[(t["folder"], (t["album"] or "").lower())].append(t)

    albums = []
    for (folder, _), members in groups.items():
        votes = collections.Counter(case(t["albumartist"]) for t in members if t["albumartist"])
        if votes:
            album_artist = votes.most_common(1)[0][0]
        else:
            mains = {case(t["main"]) for t in members if t["main"]}
            album_artist = mains.pop() if len(mains) == 1 else ("Various Artists" if mains else None)
        years = [t["year"] for t in members if t["year"]]
        year = min(years) if years else None
        multi_disc = max((t["discs"] or t["disc"] or 1) for t in members) > 1
        album_name = members[0]["album"]

        entries = []
        for t in members:
            old = t["old"]
            new = {
                "title": [t["title"]] if old["title"] else [],
                "artist": [case(t["main"])] if t["main"] else old["artist"],
                "artists": [case(a) for a in t["everyone"]] if len(t["everyone"]) > 1 else old["artists"],
                "album": [album_name] if old["album"] else [],
                "albumartist": [album_artist] if album_artist else old["albumartist"],
                "date": [t["date"]] if t["date"] else old["date"],
            }
            changes = [
                {"field": f, "label": LABELS[f], "old": "; ".join(old[f]), "new": "; ".join(v), "values": v}
                for f, v in new.items() if v != old[f]
            ]

            number = f"{t['track']:02d}" if t["track"] else ""
            if number and multi_disc:
                number = f"{t['disc'] or 1}-{number}"
            stem = f"{number} - {rules.safe_name(t['title'])}" if number else rules.safe_name(t["title"])
            album_dir = rules.safe_name(album_name or "Unknown Album")
            if year:
                album_dir = f"{year} - {album_dir}"
            artist_dir = rules.safe_name(album_artist or t["main"] or "Unknown Artist")
            ext = os.path.splitext(t["path"])[1].lower()
            new_path = os.path.join(artist_dir, album_dir, stem + ext)

            if changes or new_path != t["path"]:
                entries.append({"path": t["path"], "new_path": new_path, "title": t["title"], "changes": changes})

        if entries:
            new_dirs = {os.path.dirname(e["new_path"]) for e in entries}
            albums.append({
                "key": f"{folder}\x00{album_name}", "folder": folder, "album": album_name, "artist": album_artist,
                "year": year, "new_folder": new_dirs.pop() if len(new_dirs) == 1 else None,
                "busy": folder in busy, "tracks": entries,
            })

    # A clash is two files ending up with the same name in any audio format ("01 - Song.flac" and
    # "01 - Song.mp3" are the same song twice), or a file already sitting at that name.
    def stem(path):
        return os.path.splitext(path)[0].lower()

    listing = {}

    def occupants(new_path, own_path):
        folder = os.path.dirname(new_path)
        if folder not in listing:
            full = os.path.join(root, folder)
            listing[folder] = [os.path.join(folder, n) for n in os.listdir(full)
                               if os.path.splitext(n)[1].lower() in AUDIO_EXTS] if os.path.isdir(full) else []
        return [p for p in listing[folder] if stem(p) == stem(new_path) and p != own_path]

    moving = [e for a in albums for e in a["tracks"] if e["new_path"] != e["path"]]
    targets = collections.Counter(stem(e["new_path"]) for e in moving)
    for e in moving:
        e["clash"] = targets[stem(e["new_path"])] > 1 or bool(occupants(e["new_path"], e["path"]))
    for a in albums:
        for e in a["tracks"]:
            e.setdefault("clash", False)

    # Group clashing files by destination name so they can be compared and one kept. Files already at the
    # destination join the group, unless one is itself moving elsewhere (then there's nothing to choose).
    sources = {e["path"] for e in moving}
    by_target = collections.defaultdict(list)
    for e in moving:
        if e["clash"]:
            by_target[stem(e["new_path"])].append(e)
    clash_groups = []
    for members in by_target.values():
        target = os.path.splitext(members[0]["new_path"])[0]  # the shared name, without extension
        in_place = occupants(members[0]["new_path"], None)
        if any(p in sources for p in in_place):
            continue
        group = [{"path": p, "role": "in place"} for p in in_place] + [{"path": e["path"], "role": "moving"} for e in members]
        for e in members:
            e["clash_target"] = target
        clash_groups.append({"target": target, "kind": "incoming", "members": group})

    # Duplicates already sitting side by side: the same name in one folder, in different formats
    # ("09 - V. 3005.flac" and "09 - V. 3005.m4a"). Nothing is moving, so the checks above never see them.
    grouped = {stem(g["target"]) for g in clash_groups}
    side_by_side = collections.defaultdict(list)
    for path in paths:
        side_by_side[stem(path)].append(path)
    for key, files in side_by_side.items():
        if len(files) < 2 or key in grouped or any(p in sources for p in files):
            continue
        clash_groups.append({"target": os.path.splitext(files[0])[0], "kind": "duplicates",
                             "members": [{"path": p, "role": "in place"} for p in sorted(files)]})

    albums.sort(key=lambda a: (a["new_folder"] or a["folder"]).lower())
    return {
        "root": root,
        "albums": albums,
        "clash_groups": clash_groups,
        "errors": errors,
        "summary": {
            "albums": len(albums),
            "tracks": sum(len(a["tracks"]) for a in albums),
            "tag_fixes": sum(1 for a in albums for e in a["tracks"] if e["changes"]),
            "renames": len(moving),
            "clashes": sum(e["clash"] for e in moving),
            "duplicates": sum(1 for g in clash_groups if g["kind"] == "duplicates"),
            "busy": sum(a["busy"] for a in albums),
        },
    }
