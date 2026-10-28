"""Carry out a plan (or part of it), journaling every change so it can be undone.

Every file operation is mirrored in the catalog straight away (and committed), so the
database and the disk agree even if a run stops halfway.
"""
import collections
import datetime
import glob
import json
import os
import shutil

import config
import crud
import scan
import config
from scan import AUDIO_EXTS, TRASH_DIR

from . import tags

def journal_stamp():
    """Unique, time-ordered name for a journal (and its trash/backup folders). Microseconds keep two runs
    in the same second from overwriting each other's journal."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


JOURNAL_DIR = config.JOURNAL_DIR
LYRICS_SIDECARS = (".lrc", ".txt")  # lyrics files named after their song; they move and go to the trash with it


def _move(root, src, dst, entries, kind):
    full_dst = os.path.join(root, dst)
    if os.path.exists(full_dst):
        raise FileExistsError(f"refusing to overwrite {dst}")
    os.makedirs(os.path.dirname(full_dst), exist_ok=True)
    os.rename(os.path.join(root, src), full_dst)
    entries.append({"op": "move", "kind": kind, "from": src, "to": dst})


def _move_track(conn, root, src, dst, entries):
    """Move an audio file and its catalog row together (the row keeps its id)."""
    _move(root, src, dst, entries, "audio")
    crud.tracks.delete(conn, dst)  # the file isn't there, so any row at dst is stale
    crud.tracks.move(conn, src, dst)


def _discard(conn, root, path, stamp, entries):
    """Move a duplicate (and its lyrics) into the library's trash folder and drop it from the catalog."""
    trash = os.path.join(TRASH_DIR, stamp)
    ignore = os.path.join(root, TRASH_DIR, ".ndignore")  # keeps Navidrome (and anything honouring it) out
    if not os.path.exists(ignore):
        os.makedirs(os.path.dirname(ignore), exist_ok=True)
        open(ignore, "w").close()
    _move(root, path, os.path.join(trash, path), entries, "discarded")
    # Lyrics files are named after the song without its extension, so side-by-side copies ("Song.flac",
    # "Song.m4a") share one. It only goes with the discarded copy if no other copy stays behind.
    stem = os.path.splitext(path)[0]
    shared = any(os.path.exists(os.path.join(root, stem + ext)) for ext in AUDIO_EXTS)
    for ext in () if shared else LYRICS_SIDECARS:
        sidecar = stem + ext
        if os.path.exists(os.path.join(root, sidecar)):
            _move(root, sidecar, os.path.join(trash, sidecar), entries, "extra")
    crud.tracks.delete(conn, path)


def _lyrics_to_carry(conn, root, plan, resolutions):
    """{kept path: lyrics} for clash keepers whose discarded copies have better lyrics: any lyrics when the keeper
    has none, synced ones when it only has plain."""
    from lyrics import files  # imported here: lyrics uses this module
    groups = {g["target"].lower(): g for g in plan.get("clash_groups", [])}
    ids = {path: tid for tid, path in conn.execute("SELECT id, path FROM tracks")}
    embedded = files.embedded_by_track(conn)

    def lyrics_of(path):
        return files.best(root, path, embedded.get(ids.get(path), ()))
    carry = {}
    for target, keeper in (resolutions or {}).items():
        own = lyrics_of(keeper)
        if own and own["synced"]:
            continue
        donors = [found for m in groups[target.lower()]["members"] if m["path"] != keeper
                  for found in [lyrics_of(m["path"])] if found and (not own or found["synced"])]
        if donors:
            carry[keeper] = sorted(donors, key=lambda found: not found["synced"])[0]
    return carry


def _resolve(plan, resolutions):
    """Validate {target: path to keep} against the plan's clash groups. Returns (paths to discard, keepers)."""
    groups = {g["target"].lower(): g for g in plan.get("clash_groups", [])}
    discard, keep = set(), set()
    for target, keeper in (resolutions or {}).items():
        group = groups.get(target.lower())
        members = {m["path"] for m in group["members"]} if group else set()
        if keeper not in members:
            raise ValueError(f"invalid clash choice for {target}")
        discard |= members - {keeper}
        keep.add(keeper)
    return discard, keep


def check_catalog(conn, root, limit=10):
    """Summarise how the catalog and the disk differ (should be nothing after a cleanup)."""
    diff = crud.tracks.verify(conn, root, AUDIO_EXTS, skip_dirs={TRASH_DIR})
    return {"in_sync": not any(diff.values()),
            **{k: {"count": len(v), "examples": v[:limit]} for k, v in diff.items()}}


def _remove_empty_dirs(root, rel_dir):
    while rel_dir:
        full = os.path.join(root, rel_dir)
        if not os.path.isdir(full) or os.listdir(full):
            return
        os.rmdir(full)
        rel_dir = os.path.dirname(rel_dir)


def _has_audio(full_dir):
    return any(os.path.splitext(n)[1].lower() in AUDIO_EXTS for _, _, names in os.walk(full_dir) for n in names)


def _carry_leftovers(root, entries):
    """A folder whose tracks all went to one new folder takes its covers, logs and cue sheets along."""
    dest = collections.defaultdict(set)
    for e in entries:
        # a folder whose songs were all discarded sends its covers etc. to the trash with them
        if e["op"] == "move" and e["kind"] in ("audio", "discarded"):
            dest[os.path.dirname(e["from"])].add(os.path.dirname(e["to"]))
    for old_dir, new_dirs in dest.items():
        full_old = os.path.join(root, old_dir)
        # the inbox itself isn't an album folder: what's loose in it stays (and so does its .ndignore)
        if not old_dir or old_dir == config.INBOX or len(new_dirs) != 1 or not os.path.isdir(full_old) or _has_audio(full_old):
            continue
        new_dir = next(iter(new_dirs))
        for dirpath, _, names in os.walk(full_old):
            for name in names:
                rel = os.path.relpath(os.path.join(dirpath, name), full_old)
                if not name.endswith(".part") and name != ".ndignore" and not os.path.exists(os.path.join(root, new_dir, rel)):
                    _move(root, os.path.join(old_dir, rel), os.path.join(new_dir, rel), entries, "extra")
        for dirpath, _, _ in sorted(os.walk(full_old), reverse=True):
            _remove_empty_dirs(root, os.path.relpath(dirpath, root))


def _no_progress(done, total, message):
    pass


def move_into_place(conn, root, paths, entries):
    """Move the songs at `paths` to their clean locations, as Clean up would, with their lyrics files; then
    an emptied folder's covers and logs follow. A song whose place is taken is left where it is: that name
    clash is Clean up's to compare. Journals into `entries`; returns how many songs moved."""
    from . import plan as planner
    paths, moved = set(paths), 0
    for album in planner.build(root)["albums"]:
        for t in album["tracks"]:
            if t["path"] in paths and t["new_path"] != t["path"] and not t["clash"] \
                    and not os.path.exists(os.path.join(root, t["new_path"])):
                _move_track(conn, root, t["path"], t["new_path"], entries)
                for ext in LYRICS_SIDECARS:
                    sidecar = os.path.splitext(t["path"])[0] + ext
                    if os.path.exists(os.path.join(root, sidecar)):
                        _move(root, sidecar, os.path.splitext(t["new_path"])[0] + ext, entries, "extra")
                scan.refresh(conn, root, t["new_path"])
                conn.commit()
                moved += 1
    _carry_leftovers(root, entries)
    return moved


def apply(plan, db_path, selected=None, fix_tags=True, rename=True, progress=_no_progress, resolutions=None):
    """Apply the plan to the tracks whose paths are in `selected` (all when None), updating the catalog as it goes.

    `resolutions` ({clash target: path to keep}) settles name clashes: the keeper moves into place and
    the other files go to the trash folder. `progress(done, total, message)` is called once per step.
    """
    root = plan["root"]
    discard, keepers = _resolve(plan, resolutions)
    # Choosing which copy to keep in a clash means moving it into place, ticked or not.
    work = [t for album in plan["albums"] if not album["busy"] for t in album["tracks"]
            if (selected is None or t["path"] in selected or t["path"] in keepers) and t["path"] not in discard]
    total = len(discard) + len(work)
    conn = crud.connect(db_path)
    entries, stats = [], collections.Counter()
    stamp = journal_stamp()
    journal_path = os.path.join(JOURNAL_DIR, f"{stamp}.json")
    carry = _lyrics_to_carry(conn, root, plan, resolutions)  # read before the discarded copies leave
    try:
        # Discards first, so a keeper can move into a spot the discarded file occupied.
        for i, path in enumerate(sorted(discard)):
            progress(i, total, f"Discarding {path}")
            _discard(conn, root, path, stamp, entries)
            conn.commit()
            stats["discarded"] += 1
        final = {}  # where each song ended up
        for i, t in enumerate(work, len(discard)):
            path, touched = t["path"], False
            progress(i, total, path)
            if fix_tags and t["changes"]:
                size = os.path.getsize(os.path.join(root, path))
                before = tags.write(os.path.join(root, path), {c["field"]: c["values"] for c in t["changes"]})
                entries.append({"op": "tags", "path": path, "before": before, "size": size})
                stats["tags"] += 1
                touched = True
            if rename and t["new_path"] != path:
                if (t["clash"] and path not in keepers) or os.path.exists(os.path.join(root, t["new_path"])):
                    stats["clashes_skipped"] += 1
                else:
                    _move_track(conn, root, path, t["new_path"], entries)
                    for ext in LYRICS_SIDECARS:
                        sidecar = os.path.splitext(path)[0] + ext
                        if os.path.exists(os.path.join(root, sidecar)):
                            _move(root, sidecar, os.path.splitext(t["new_path"])[0] + ext, entries, "extra")
                    path, touched = t["new_path"], True
                    stats["moved"] += 1
            if touched:
                scan.refresh(conn, root, path)  # re-read tags/size/mtime from the file as it is now
                conn.commit()
            final[t["path"]] = path
        if keepers:
            # A copy kept in a name clash may come from another download, tagged with a different edition:
            # re-tag it to the edition the rest of its album is linked to.
            from musicbrainz import service as mb_service  # imported here: musicbrainz itself uses this module
            progress(total, total, "Aligning kept copies with their album's edition…")
            mb_service.align_songs(conn, root, [final.get(k, k) for k in keepers], entries, stats)
        if carry:
            # The kept copy had no lyrics but a discarded one did: save them next to the kept copy.
            from lyrics import service as lyrics_service
            for keeper, found in carry.items():
                if lyrics_service.write_sidecar(root, final.get(keeper, keeper), found["text"], found["synced"], entries):
                    stats["lyrics_carried_over"] += 1
        if rename or discard:
            progress(total, total, "Moving covers, logs and cue sheets…")
            _carry_leftovers(root, entries)
    finally:
        conn.commit()
        if entries:
            os.makedirs(JOURNAL_DIR, exist_ok=True)
            with open(journal_path, "w", encoding="utf-8") as f:
                json.dump({"root": root, "created": stamp, "stats": stats, "entries": entries}, f, ensure_ascii=False, indent=1)
    progress(total, total, "Checking the catalog against the disk…")
    crud.releases.recompute(conn)  # discarded duplicates may leave an album incomplete
    conn.commit()
    result = {"stats": dict(stats), "journal": journal_path if entries else None, "catalog": check_catalog(conn, root)}
    conn.close()
    return result


def latest_journal(root):
    """Newest journal not yet undone for this library root (journals of other libraries are ignored)."""
    for path in sorted(glob.glob(os.path.join(JOURNAL_DIR, "*.json")), reverse=True):
        if path.endswith(".undone.json"):
            continue
        with open(path, encoding="utf-8") as f:
            if json.load(f).get("root") == root:
                return path
    return None


def undo(db_path, journal_path=None, progress=_no_progress):
    """Reverse the most recent (or given) journal: moves back first, then tags restored in place."""
    conn = crud.connect(db_path)
    journal_path = journal_path or latest_journal(crud.get_root(conn))
    if not journal_path:
        conn.close()
        raise LookupError("nothing to undo")
    with open(journal_path, encoding="utf-8") as f:
        journal = json.load(f)
    root, counts = journal["root"], collections.Counter()
    entries = list(reversed(journal["entries"]))
    try:
        for i, e in enumerate(entries):
            progress(i, len(entries), e.get("from") or e["path"])
            if e["op"] == "move":
                os.makedirs(os.path.dirname(os.path.join(root, e["from"])), exist_ok=True)
                os.rename(os.path.join(root, e["to"]), os.path.join(root, e["from"]))
                _remove_empty_dirs(root, os.path.dirname(e["to"]))
                if e["kind"] in ("audio", "discarded"):
                    # discarded files have no row any more; refresh re-catalogues them
                    crud.tracks.delete(conn, e["from"])
                    crud.tracks.move(conn, e["to"], e["from"])
                    scan.refresh(conn, root, e["from"])
                counts["moved_back"] += 1
            elif e["op"] == "tags":
                tags.restore(os.path.join(root, e["path"]), e["before"], e.get("size"))
                scan.refresh(conn, root, e["path"])
                counts["tags_restored"] += 1
            elif e["op"] == "art":  # embedded artwork: put back exactly the pictures that were there
                from artwork import embed
                pictures = []
                for p in e["pictures"]:
                    with open(os.path.join(JOURNAL_DIR, p["file"]), "rb") as f:
                        pictures.append({**p, "data": f.read()})
                embed.restore(os.path.join(root, e["path"]), pictures, e.get("size"))
                scan.refresh(conn, root, e["path"])
                counts["art_restored"] += 1
            elif e["op"] == "file":  # a lyrics file written next to a song: put back what was there
                full = os.path.join(root, e["path"])
                if e.get("before") is None:
                    if os.path.exists(full):
                        os.remove(full)
                else:
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(e["before"])
                counts["files_restored"] += 1
            elif e["op"] == "import":  # brought in by Import: moved back where it came from, or a copy removed
                from transfer import service as transfer
                transfer.undo_entry(root, e)
                crud.tracks.delete(conn, e["path"])
                _remove_empty_dirs(root, os.path.dirname(e["path"]))
                counts["imports_undone"] += 1
            elif e["op"] == "removed":  # a file set aside (a folder cover being replaced): put it back
                shutil.copyfile(os.path.join(JOURNAL_DIR, e["backup"]), os.path.join(root, e["path"]))
                counts["files_restored"] += 1
            elif e["op"] == "created":  # a cover.jpg written next to the songs
                if os.path.exists(os.path.join(root, e["path"])):
                    os.remove(os.path.join(root, e["path"]))
                counts["files_removed"] += 1
            conn.commit()
        if journal.get("kind") == "artwork":
            crud.artwork.forget(conn, journal.get("albums", []))
        if journal.get("backup"):  # pictures kept for this undo: put back now, so no longer needed
            shutil.rmtree(os.path.join(JOURNAL_DIR, journal["backup"]), ignore_errors=True)
        os.rename(journal_path, journal_path[:-len(".json")] + ".undone.json")
        progress(len(entries), len(entries), "Checking the catalog against the disk…")
        crud.releases.recompute(conn)  # undoing a MusicBrainz apply unlinks its songs
        conn.commit()
        return {**counts, "catalog": check_catalog(conn, root)}
    finally:
        conn.commit()
        conn.close()
