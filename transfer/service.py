"""Import and export.

Import moves (or copies) a folder of music from anywhere on this machine into the inbox, keeping its
folder structure under the inbox: <inbox>/<source folder name>/... Songs then go through the usual steps
and Clean up releases them into the library. Imports are journaled: Undo puts moved files back where they
came from and removes copies.

Export copies chosen album folders out of the library to another folder (a USB stick, a folder to burn to
a DVD), keeping the library's structure (Artist/Year - Album/...). The library isn't changed, so exports
aren't journaled. Files already there with the same size are skipped, so an interrupted export picks up
where it stopped.
"""
import getpass
import json
import os
import shutil

import config
import crud
from cleanup import apply as cleanup_apply
from scan import AUDIO_EXTS, TRASH_DIR

# Files that belong with an album: the songs, lyrics, covers, cue sheets and rip logs.
ALBUM_FILE_EXTS = AUDIO_EXTS | {".lrc", ".txt", ".cue", ".log", ".nfo", ".m3u", ".m3u8", ".pdf",
                                ".jpg", ".jpeg", ".png", ".webp", ".gif"}
PARTIAL = ".muse-part"  # a file being copied; renamed into place once complete
# Characters FAT/exFAT (most USB sticks), Windows and some DVD formats don't allow in names.
UNSAFE_CHARS = {":": " -", "<": "(", ">": ")", '"': "'", "\\": "-", "|": "-", "?": "", "*": ""}


def _no_progress(done, total, message):
    pass


def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _album_files(folder, recursive):
    """(relative path, size) of the album files in a folder, skipping Muse's own and hidden folders."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")) if recursive else []
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in ALBUM_FILE_EXTS and not name.startswith("."):
                full = os.path.join(dirpath, name)
                out.append((os.path.relpath(full, folder), os.path.getsize(full)))
    return out


def _copy(src, dst):
    """Copy through a temporary name, so an interrupted copy never leaves a half file under the real name."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst + PARTIAL)
    os.replace(dst + PARTIAL, dst)


# --- choosing folders on this machine ---------------------------------------------------

def places(root):
    """Starting points for the folder picker: home, the library, and mounted drives (USB sticks, discs)."""
    out = [{"name": "Home", "path": os.path.expanduser("~")}, {"name": "Library", "path": root}]
    user = getpass.getuser()
    for base in (f"/run/media/{user}", f"/media/{user}", "/media", "/mnt"):
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                path = os.path.join(base, name)
                if os.path.isdir(path) and os.path.ismount(path) and all(p["path"] != path for p in out):
                    out.append({"name": name, "path": path, "drive": True})
    return out


def browse(root, path=None):
    """A folder's subfolders (not hidden ones) and how many songs sit directly in it, for the folder picker."""
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(path):
        raise LookupError(f"no such folder: {path}")
    folders, songs = [], 0
    with os.scandir(path) as entries:
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    folders.append(e.name)
                elif os.path.splitext(e.name)[1].lower() in AUDIO_EXTS:
                    songs += 1
            except OSError:
                continue
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None, "folders": sorted(folders, key=str.lower),
            "songs_here": songs, "writable": os.access(path, os.W_OK), "places": places(root)}


# --- import -----------------------------------------------------------------------------

def _inbox(root):
    if not config.INBOX:
        raise ValueError("there's no inbox to import into: set MUSE_INBOX in .env")
    return os.path.join(root, config.INBOX)


def _import_plan(root, source):
    source = os.path.abspath(os.path.expanduser(source or ""))
    if not os.path.isdir(source):
        raise LookupError(f"no such folder: {source}")
    if _inside(source, root):
        raise ValueError("that folder is already in the library: Scan finds what's there")
    if _inside(root, source):
        raise ValueError("that folder contains the library: pick the folder with the new music in it")
    target = os.path.join(config.INBOX, os.path.basename(source.rstrip(os.sep)) or "Import")
    files = [{"rel": rel, "size": size, "to": os.path.join(target, rel)} for rel, size in _album_files(source, True)]
    for f in files:
        f["exists"] = os.path.exists(os.path.join(root, f["to"]))
    return source, target, files


def import_preview(root, source):
    """What importing `source` would bring in: counts, size, its folders, and files already in the inbox."""
    _inbox(root)
    source, target, files = _import_plan(root, source)
    songs = [f for f in files if os.path.splitext(f["rel"])[1].lower() in AUDIO_EXTS]
    folders = {}
    for f in songs:
        folder = os.path.dirname(f["rel"]) or "."
        folders[folder] = folders.get(folder, 0) + 1
    return {"source": source, "target": target, "files": len(files), "songs": len(songs),
            "bytes": sum(f["size"] for f in files), "folders": [{"folder": k, "songs": v} for k, v in sorted(folders.items())],
            "already_there": sum(f["exists"] for f in files),
            "same_drive": os.stat(source).st_dev == os.stat(root).st_dev}


def import_folder(db_path, source, move=True, skip=(), finished=None, progress=_no_progress):
    """Move (or copy) the album files under `source` into <inbox>/<source name>/, keeping their structure.
    Files already in the inbox are left alone. Journaled as one "import": Undo moves them back or removes copies."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    conn.close()
    _inbox(root)
    source, target, files = _import_plan(root, source)
    work = [f for f in files if not f["exists"] and f["rel"] not in skip]
    entries, stats = [], {"imported": 0, "already_there": sum(f["exists"] for f in files), "bytes": 0}
    stamp = cleanup_apply.journal_stamp()
    try:
        for i, f in enumerate(work):
            progress(i, len(work), f"{f['rel']} ({f['size'] / 1e6:.0f} MB)")
            src, dst = os.path.join(source, f["rel"]), os.path.join(root, f["to"])
            if move:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.rename(src, dst)  # same drive: instant
                except OSError:  # another drive (a USB stick): copy, then remove the original
                    _copy(src, dst)
                    os.remove(src)
            else:
                _copy(src, dst)
            entries.append({"op": "import", "path": f["to"], "source": src, "moved": move})
            stats["imported"] += 1
            stats["bytes"] += f["size"]
            if finished is not None:
                finished.append(f["rel"])
        progress(len(work), len(work), "Done")
    finally:
        if move:  # folders the import emptied (not the source folder itself, which you picked)
            for dirpath, _, _ in sorted(os.walk(source), reverse=True):
                if dirpath != source and not os.listdir(dirpath):
                    os.rmdir(dirpath)
        if entries:
            os.makedirs(cleanup_apply.JOURNAL_DIR, exist_ok=True)
            with open(os.path.join(cleanup_apply.JOURNAL_DIR, f"{stamp}-import.json"), "w", encoding="utf-8") as fh:
                json.dump({"root": root, "created": stamp, "kind": "import", "stats": stats, "entries": entries}, fh,
                          ensure_ascii=False, indent=1)
    return {"source": source, "target": target, "moved": move, **stats}


def undo_entry(root, e):
    """Reverse one imported file: a moved one goes back where it came from, a copy is removed."""
    full = os.path.join(root, e["path"])
    if not os.path.exists(full):
        return
    if e["moved"]:
        os.makedirs(os.path.dirname(e["source"]), exist_ok=True)
        if os.path.exists(e["source"]):
            raise FileExistsError(f"refusing to overwrite {e['source']}")
        shutil.move(full, e["source"])
    else:
        os.remove(full)


# --- export -----------------------------------------------------------------------------

def safe_name(name):
    """A file or folder name every common file system accepts (FAT/exFAT USB sticks, Windows, DVDs)."""
    for bad, good in UNSAFE_CHARS.items():
        name = name.replace(bad, good)
    name = "".join(c for c in name if ord(c) >= 32).rstrip(" .")
    return name or "_"


def _chosen_files(files, songs):
    """Of a folder's files, the ones that go with the chosen songs: each song and its own lyrics files, plus
    the album's shared files (covers, cue sheets, logs). Other songs' lyrics files stay behind."""
    def stem(rel):
        return os.path.splitext(rel)[0]
    audio = {rel for rel, _ in files if os.path.splitext(rel)[1].lower() in AUDIO_EXTS}
    others = {stem(rel) for rel in audio - set(songs)}  # songs left out: their lyrics files stay behind too
    return [(rel, size) for rel, size in files if (rel in songs if rel in audio else stem(rel) not in others)]


def _export_plan(root, folders, dest, safe, tracks=()):
    dest = os.path.abspath(os.path.expanduser(dest or ""))
    if not os.path.isdir(dest):
        raise LookupError(f"no such folder: {dest}")
    if _inside(dest, root):
        raise ValueError("pick a folder outside the library: copies inside it would be catalogued as duplicates")
    if not os.access(dest, os.W_OK):
        raise PermissionError(f"can't write to {dest}")
    songs = {}  # folder -> chosen songs (file names), for albums exported in part
    for path in tracks:
        songs.setdefault(os.path.dirname(path), set()).add(os.path.basename(path))
    files = []
    for folder in dict.fromkeys(list(folders) + [f for f in songs if f not in folders]):
        full = os.path.join(root, folder)
        if not folder or not os.path.isdir(full) or not _inside(full, root) or folder.split(os.sep)[0] == TRASH_DIR:
            continue
        own = _album_files(full, False)  # an album folder's own files: subfolders are albums of their own
        if folder not in folders:
            own = _chosen_files(own, songs[folder])
        for rel, size in own:
            path = os.path.join(folder, rel)
            out = os.path.join(*(safe_name(p) for p in path.split(os.sep))) if safe else path
            files.append({"path": path, "size": size, "to": os.path.join(dest, out)})
    for f in files:
        f["there"] = os.path.exists(f["to"]) and os.path.getsize(f["to"]) == f["size"]
    return dest, files


def export(db_path, folders, dest, safe=True, tracks=(), skip=(), finished=None, progress=_no_progress):
    """Copy whole album folders (`folders`) and single songs (`tracks`, library paths) to `dest`, keeping the
    library's structure. A song comes with its lyrics files and its album's cover. Files already there with
    the same size are skipped, so running it again carries on an interrupted export."""
    conn = crud.connect(db_path)
    root = crud.get_root(conn)
    conn.close()
    dest, files = _export_plan(root, folders, dest, safe, tracks)
    work = [f for f in files if not f["there"] and f["path"] not in skip]
    needed, free = sum(f["size"] for f in work), shutil.disk_usage(dest).free
    if needed > free:
        raise OSError(f"not enough space: {needed / 1e9:.1f} GB to copy, {free / 1e9:.1f} GB free on the destination")
    for i, f in enumerate(work):
        progress(i, len(work), f"{f['path']} ({f['size'] / 1e6:.0f} MB)")
        _copy(os.path.join(root, f["path"]), f["to"])
        if finished is not None:
            finished.append(f["path"])
    progress(len(work), len(work), "Done")
    return {"dest": dest, "folders": len({os.path.dirname(f["path"]) for f in files}), "copied": len(work), "already_there": len(files) - len(work),
            "bytes": needed, "free_after": shutil.disk_usage(dest).free}
