#!/usr/bin/env python3
"""Serve the Muse catalog (built by scan.py) as a small web app.

Usage: .venv/bin/python app.py [--db PATH] [--host HOST] [--port PORT]

Settings come from .env (see .env.example). The catalog database is created, with a first scan of
MUSE_LIBRARY, if it doesn't exist yet.
"""
import argparse
import collections
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

import config
import crud
import scan

HERE = os.path.dirname(os.path.abspath(__file__))
COVER_NAMES = ("cover.jpg", "cover.png", "folder.jpg", "front.jpg", "albumart.jpg")
MAX_UPLOAD = 40_000_000  # request body limit; artwork uploads arrive base64-encoded
# Scans, cleanups and MusicBrainz runs change files and the catalog: one at a time.
JOB_LOCK = threading.Lock()
# Shared with /api/progress so the page can draw a progress bar while a job runs.
PROGRESS = {"running": False}
# Set by the Stop button. Lookups (MusicBrainz, artwork, lyrics) check it between items.
STOP = threading.Event()
# The last finished job, so a page that comes back (or another device) can say how it ended. Jobs are
# numbered so a page shows each outcome once.
JOBS = {"seq": 0, "last": None}
# When a page last asked for progress: a lookup nobody is watching stops by itself (config.STOP_UNWATCHED).
WATCHED = {"at": time.monotonic()}
# Navidrome is rescanned and re-linked in the background after each job; one pass at a time, and a
# request arriving mid-pass triggers one more.
NAVIDROME_FOLLOWUP = {"lock": threading.Lock(), "again": False, "last": None}
STREAM_CHUNK = 64 * 1024


def theme_css():
    """The accent colours from .env (or their defaults) as CSS variables, light and dark."""
    colors = config.colors()
    light = "".join(f"--{name}: {pair[0]}; " for name, pair in colors.items())
    dark = "".join(f"--{name}: {pair[1]}; " for name, pair in colors.items())
    return (f":root {{ {light}}}\n"
            f"@media (prefers-color-scheme: dark) {{ :root {{ {dark}}} }}\n")


def navidrome_followup(db_path):
    """Rescan Navidrome and re-link songs, in a background thread. Silent if Navidrome isn't configured."""
    from navidrome import client
    if not client.configured():
        return
    NAVIDROME_FOLLOWUP["again"] = True
    if not NAVIDROME_FOLLOWUP["lock"].acquire(blocking=False):
        return  # the running pass will go round again

    def run():
        from navidrome import service
        try:
            while NAVIDROME_FOLLOWUP["again"]:
                NAVIDROME_FOLLOWUP["again"] = False
                try:
                    NAVIDROME_FOLLOWUP["last"] = service.sync(db_path)
                except Exception as e:  # noqa: BLE001 - Navidrome being down must never break Muse
                    NAVIDROME_FOLLOWUP["last"] = {"error": str(e)}
        finally:
            NAVIDROME_FOLLOWUP["lock"].release()
    threading.Thread(target=run, daemon=True, name="navidrome-sync").start()


class Stopped(Exception):
    """Raised from a stoppable job's progress callback. Args: (done, total, reason), reason being
    "requested" (the Stop button) or "unwatched" (no page has checked on it for a while)."""


def report_progress(op, stoppable=False, unwatched=True):
    """A progress callback for job `op`. Stoppable jobs stop at the next call once Stop is pressed, and also
    (with `unwatched`) once no page has been watching for config.STOP_UNWATCHED seconds."""
    def update(done, total, message):
        if stoppable:
            if STOP.is_set():
                raise Stopped(done, total, "requested")
            if unwatched and config.STOP_UNWATCHED and time.monotonic() - WATCHED["at"] > config.STOP_UNWATCHED:
                raise Stopped(done, total, "unwatched")
        PROGRESS.update(running=True, op=op, done=done, total=total, message=message, stoppable=stoppable)
    return update


def run_stoppable(op, unit, message, path, body, lookup, unwatched=True):
    """Run a lookup that can stop between items. Lookups save each item's result as they go, so a stop keeps
    everything found so far. The result then says how far it got, and carries the request that continues
    with the rest: the same one, with the finished items (album keys or song ids) added to `skip`."""
    finished = []
    progress = report_progress(op, stoppable=True, unwatched=unwatched)
    try:
        progress(0, 0, message)
        return lookup(progress, finished)
    except Stopped as e:
        done, total, reason = e.args
        return {"stopped": True, "reason": reason, "done": done, "total": total, "unit": unit,
                "continue": {"path": path, "body": {**body, "skip": list(body.get("skip") or []) + finished}}}


class Handler(BaseHTTPRequestHandler):
    db_path = None

    def db(self):
        return crud.connect(self.db_path, readonly=True)

    def root(self):
        with self.db() as conn:
            return crud.get_root(conn)

    def send(self, status, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if ctype.split(";")[0] in ("text/html", "text/css", "text/javascript", "application/json"):
            self.send_header("Cache-Control", "no-cache")  # the page and its data: never a stale copy after an update
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser stopped waiting (closed the page, skipped an image): nothing to do

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
        try:
            if url.path == "/":
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if parts[0] == "api":
                route = getattr(self, "api_" + parts[1], None) if len(parts) > 1 else None
                if route:
                    with self.db() as conn:
                        return self.send(200, route(conn, q, *parts[2:]))
            if parts[0] == "artist-image":  # ?name=: the picture in an artist's folder
                return self.artist_image(q.get("name") or "")
            if parts[0] == "cover" and len(parts) == 2:
                return self.cover(int(parts[1]), embedded_only=q.get("embedded") == "1")
            if parts[0] == "stream" and len(parts) == 2:
                return self.stream(int(parts[1]))
            if url.path == "/theme.css":
                return self.send(200, theme_css().encode(), "text/css; charset=utf-8")
            if parts[0] == "static" and len(parts) == 2 and parts[1] in os.listdir(os.path.join(HERE, "static")) \
                    and parts[1].endswith((".css", ".js")):
                with open(os.path.join(HERE, "static", parts[1]), "rb") as f:
                    ctype = "text/css" if parts[1].endswith(".css") else "text/javascript"
                    return self.send(200, f.read(), ctype + "; charset=utf-8")
            if parts[0] == "art" and len(parts) == 2:  # a cached artwork candidate, by sha
                from artwork import images
                data = images.load(parts[1])
                return self.send(200, data, images.probe(data)[0])
            self.send(404, {"error": "not found"})
        except (ValueError, LookupError, OSError) as e:
            self.send(400, {"error": str(e)})

    def do_POST(self):
        # A JSON content type forces a CORS preflight, so other websites can't trigger these.
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "expected application/json"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self.send(413, {"error": "that file is too large (limit 40 MB)"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/art/upload":  # quick, and doesn't touch audio files: no job lock
                return self.send(200, self.art_upload(body))
            if self.path == "/api/merge/preview":  # only reads
                return self.send(200, self.merge_preview(body))
            if self.path == "/api/merge/ignore":  # one catalog row, no files
                return self.send(200, self.merge_ignore(body))
            if self.path == "/api/appearance":  # only .env colour lines
                return self.send(200, self.save_appearance(body))
            if self.path == "/api/artists/picture":  # an artist's picture and the candidates found so far (no network)
                from artwork import artists
                conn = crud.connect(self.db_path)
                try:
                    return self.send(200, artists.state(conn, crud.get_root(conn), body.get("name") or ""))
                finally:
                    conn.close()
            if self.path == "/api/art/folder":  # an album folder's artwork for Edit artwork (local images only)
                from artwork import service as art_service
                conn = crud.connect(self.db_path)
                try:
                    return self.send(200, art_service.folder_state(conn, crud.get_root(conn), body.get("folder") or ""))
                finally:
                    conn.close()
            if self.path == "/api/remake/choose":  # one catalog row: the release a song should go to
                from musicbrainz import remake
                conn = crud.connect(self.db_path)
                try:
                    remake.choose(conn, int(body["track_id"]), body.get("choice"))
                    conn.commit()
                finally:
                    conn.close()
                return self.send(200, {"ok": True})
            if self.path == "/api/transfer/preview":  # only reads
                with self.db() as conn:
                    from transfer import service as transfer
                    return self.send(200, transfer.import_preview(crud.get_root(conn), body.get("source")))
            if self.path == "/api/jobs/stop":  # asks the running lookup to stop after its current item
                stoppable = bool(PROGRESS.get("running") and PROGRESS.get("stoppable"))
                if stoppable:
                    STOP.set()
                return self.send(200, {"stopping": stoppable})
            route = {"/api/cleanup/apply": self.cleanup_apply, "/api/cleanup/undo": self.cleanup_undo,
                     "/api/scan": self.run_scan, "/api/mb/fetch": self.mb_fetch, "/api/mb/apply": self.mb_apply,
                     "/api/art/find": self.art_find, "/api/art/apply": self.art_apply,
                     "/api/housekeeping/prune": self.prune, "/api/navidrome/sync": self.navidrome_sync,
                     "/api/lyrics/find": self.lyrics_find, "/api/lyrics/apply": self.lyrics_apply,
                     "/api/lyrics/orphans": self.lyrics_orphans, "/api/merge/apply": self.merge_apply,
                     "/api/art/set": self.art_set, "/api/artists/picture/find": self.artist_picture_find,
                     "/api/artists/picture/set": self.artist_picture_set, "/api/singles": self.make_singles, "/api/remake/lookup": self.remake_lookup, "/api/remake/apply": self.remake_apply,
                     "/api/degrease/lookup": self.degrease_lookup, "/api/degrease/apply": self.degrease_apply,
                     "/api/transfer/import": self.transfer_import, "/api/transfer/export": self.transfer_export,
                     }.get(self.path)
            if route is None:
                return self.send(404, {"error": "not found"})
            if not JOB_LOCK.acquire(blocking=False):
                return self.send(409, {"error": f"busy: a {PROGRESS.get('op', 'job')} is already running"})
            STOP.clear()
            JOBS["seq"] += 1
            job = {"id": JOBS["seq"], "path": self.path, "op": None}
            PROGRESS.update(job=job["id"])
            WATCHED["at"] = time.monotonic()
            try:
                result = route(body)
                job["op"] = PROGRESS.get("op")
                if route not in (self.prune, self.navidrome_sync):
                    self.housekeep()
                    navidrome_followup(self.db_path)  # files may have changed: Navidrome rescans, links refresh
                if isinstance(result, dict):
                    result["job_id"] = job["id"]
                job["result"] = result
            except Exception as e:
                job.update(op=job["op"] or PROGRESS.get("op"), error=str(e))
                raise
            finally:
                job["finished_at"], job["finished_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S"), time.time()
                JOBS["last"] = job
                PROGRESS.clear()  # before the response goes out: the page then finds the job finished
                PROGRESS["running"] = False
                JOB_LOCK.release()
            return self.send(200, result)
        except ImportError as e:
            self.send(500, {"error": str(e)})
        except Exception as e:  # report MusicBrainz/IO failures to the page instead of dropping the connection
            self.send(400, {"error": str(e)})

    def refresh_status(self, progress):
        """After any change: recompute processed flags, completeness, and align proposals for mixed editions."""
        from cleanup import status
        from musicbrainz import service as mb_service
        progress(0, 0, "Updating ingestion status…")
        conn = crud.connect(self.db_path)
        try:
            root = crud.get_root(conn)
            status.refresh(conn, root)
            crud.releases.recompute(conn)
            mb_service.propose_alignments(conn, root)
        finally:
            conn.close()

    # --- API ---------------------------------------------------------------

    def api_stats(self, conn, q):
        return {**crud.tracks.stats(conn), "root": crud.get_root(conn)}

    def api_albums(self, conn, q):
        kinds = crud.tracks.release_kinds(conn)
        return [{**a, "kind": kinds.get(a["folder"], "album")} for a in crud.tracks.albums(conn)]

    def api_artists(self, conn, q):
        """Album artists with their albums, EPs and singles; with ?name=, one artist's page (incl. appearances)."""
        albums = self.api_albums(conn, q)
        if q.get("name"):
            name = q["name"].lower()
            own = [a for a in albums if (a["artist"] or "").lower() == name and a["folder"]]
            guest = crud.tracks.appearances(conn, q["name"])
            folders = {f: n for found in guest.values() for f, n in found.items()}
            from artwork import artists
            artist_folder = artists.folder(conn, q["name"])
            return {"artist": own[0]["artist"] if own else q["name"], "albums": own, "folder": artist_folder,
                    "picture": artists.picture_file(crud.get_root(conn), artist_folder),
                    "appears_on": [{**a, "songs_by": folders[a["folder"]]} for a in albums if a["folder"] in folders]}
        guest = {k.lower(): len(v) for k, v in crud.tracks.appearances(conn).items()}
        out = {}
        for a in albums:
            if not a["folder"] or not a["artist"]:
                continue
            entry = out.setdefault(a["artist"].lower(), {"artist": a["artist"], "album": 0, "ep": 0, "single": 0, "songs": 0,
                                                        "cover_id": a["cover_id"], "appears_on": 0, "tops": collections.Counter()})
            if "/" in a["folder"]:
                entry["tops"][a["folder"].split("/")[0]] += 1
            entry[a["kind"]] += 1
            entry["songs"] += a["tracks"]
        from artwork import artists
        root = crud.get_root(conn)
        for key, entry in out.items():
            entry["appears_on"] = guest.get(key, 0)
            tops = entry.pop("tops")
            entry["picture"] = bool(tops and artists.picture_file(root, tops.most_common(1)[0][0]))
        return sorted(out.values(), key=lambda e: e["artist"].lower())

    def api_tracks(self, conn, q):
        return crud.tracks.search(conn, q=q.get("q"), folder=q.get("folder"), sort=q.get("sort"), direction=q.get("dir"))

    def api_track(self, conn, q, track_id):
        track = crud.tracks.get(conn, int(track_id))
        if track is None:
            raise LookupError("no such track")
        return track

    # --- cleanup -------------------------------------------------------------

    def api_cleanup(self, conn, q):
        try:
            from cleanup import apply as applier, clashes, plan as planner
        except ImportError as e:
            return {"error": f"{e}. Start the app with .venv/bin/python app.py"}
        root = crud.get_root(conn)
        plan = planner.build(root)
        for album in plan["albums"]:
            for t in album["tracks"]:
                for c in t["changes"]:
                    del c["values"]
        plan["clash_groups"] = clashes.describe(root, plan["clash_groups"], lambda g: self.linked_release(conn, g))
        plan["undo_available"] = applier.latest_journal(root) is not None
        return plan

    def linked_release(self, conn, group):
        """The stored release (with details) the songs already at a clash's destination are linked to."""
        folder = os.path.dirname(group["target"])
        row = conn.execute("""SELECT r.mb_release_id id, r.title, r.date FROM tracks t JOIN releases r ON r.mb_release_id = t.mb_album_id
                              WHERE t.folder = ? AND r.summary IS NOT NULL GROUP BY r.mb_release_id
                              ORDER BY count(*) DESC LIMIT 1""", (folder,)).fetchone()
        return dict(row) if row else None

    # --- undo history housekeeping ------------------------------------------

    def housekeep(self):
        """Expire undo history past its lifetime. Never lets a failure here spoil the job that just ran."""
        try:
            from cleanup import retention
            conn = crud.connect(self.db_path)
            try:
                retention.prune(conn, crud.get_root(conn))
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            print(f"housekeeping skipped: {e}", flush=True)

    def api_housekeeping(self, conn, q):
        from cleanup import retention
        return retention.inventory(crud.get_root(conn))

    def prune(self, body):
        from cleanup import retention
        report_progress("housekeeping")(0, 0, "Clearing expired undo history…")
        conn = crud.connect(self.db_path)
        try:
            root = crud.get_root(conn)
            return {"removed": retention.prune(conn, root), "inventory": retention.inventory(root)}
        finally:
            conn.close()

    # --- appearance ---------------------------------------------------------------

    def api_appearance(self, conn, q):
        return {"colors": config.colors(), "defaults": config.COLOR_DEFAULTS}

    def save_appearance(self, body):
        """Save accent colours ({name: [light, dark]}) to .env; {"reset": true} goes back to the defaults.
        A colour equal to its default is removed from .env rather than written, so later default changes apply."""
        chosen = config.COLOR_DEFAULTS if body.get("reset") else body.get("colors") or {}
        updates = {}
        for name, pair in chosen.items():
            if name not in config.COLOR_DEFAULTS or not isinstance(pair, (list, tuple)) or len(pair) != 2 \
                    or not all(config.HEX_COLOR.match(str(c)) for c in pair):
                raise ValueError(f"{name}: colours must look like #1a2b3c")
            for key, value, default in ((f"MUSE_COLOR_{name.upper()}", pair[0], config.COLOR_DEFAULTS[name][0]),
                                        (f"MUSE_COLOR_{name.upper()}_DARK", pair[1], config.COLOR_DEFAULTS[name][1])):
                updates[key] = None if value.lower() == default else value.lower()
        config.save_env(updates)
        return {"colors": config.colors()}

    # --- merging albums by hand -------------------------------------------------

    def api_merge(self, conn, q):
        from cleanup import merge
        return {"albums": merge.albums(conn), "suggestions": merge.suggestions(conn),
                "ignored": list(crud.merges.ignored(conn).values())}

    def merge_ignore(self, body):
        """Mark a suggestion as fine ({"keys", "reason"}), or bring it back ({"keys", "undo": true})."""
        from cleanup import merge
        conn = crud.connect(self.db_path)
        try:
            if body.get("undo"):
                crud.merges.unignore(conn, body.get("keys") or [])
            else:
                merge.ignore(conn, body.get("keys") or [], body.get("reason"))
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

    def merge_preview(self, body):
        from cleanup import merge
        with self.db() as conn:
            result = merge.preview(conn, crud.get_root(conn), body.get("keys") or [], body.get("target"))
        for song in result["songs"]:
            song.pop("values", None)
        return result

    def merge_apply(self, body):
        from cleanup import merge
        progress = report_progress("merge")
        result = merge.apply(self.db_path, body.get("keys") or [], body.get("target"), move=bool(body.get("move", True)),
                             progress=progress)
        self.refresh_status(progress)
        return result

    # --- remake Various Artists -----------------------------------------------------

    def api_remake(self, conn, q):
        from musicbrainz import remake
        return remake.state(conn, crud.get_root(conn))

    def api_degrease(self, conn, q):
        from musicbrainz import remake
        return remake.state(conn, crud.get_root(conn), mode="degrease")

    def _song_lookup(self, body, mode):
        from musicbrainz import remake
        return run_stoppable(mode, "songs", "Finding the songs…", self.path, body, lambda progress, finished: remake.lookup(
            self.db_path, body.get("keys") or [], refresh=bool(body.get("refresh")), skip={int(t) for t in body.get("skip") or []},
            finished=finished, progress=progress, mode=mode))

    def _song_apply(self, body, mode):
        from musicbrainz import remake
        progress = report_progress(mode)
        result = remake.apply(self.db_path, [int(t) for t in body.get("track_ids") or []], progress=progress, mode=mode)
        self.refresh_status(progress)
        return result

    def remake_lookup(self, body):
        return self._song_lookup(body, "remake")

    def remake_apply(self, body):
        return self._song_apply(body, "remake")

    def degrease_lookup(self, body):
        return self._song_lookup(body, "degrease")

    def degrease_apply(self, body):
        return self._song_apply(body, "degrease")

    # --- import & export ----------------------------------------------------------

    def api_transfer(self, conn, q, what=None):
        """/api/transfer/browse?path=: folders on this machine, for picking an import source or export destination."""
        from transfer import service as transfer
        if what == "browse":
            return transfer.browse(crud.get_root(conn), q.get("path"))
        return {"inbox": config.INBOX or None, "places": transfer.places(crud.get_root(conn))}

    def transfer_import(self, body):
        """Move or copy a folder into the inbox (stoppable: what's moved stays moved), then catalogue it."""
        from transfer import service as transfer
        move = bool(body.get("move", True))
        result = run_stoppable("import", "files", "Reading the folder…", self.path, body, lambda progress, finished:
                               transfer.import_folder(self.db_path, body.get("source"), move=move, skip=set(body.get("skip") or []),
                                                      finished=finished, progress=progress), unwatched=False)
        progress = report_progress("import")
        scan.scan(self.root(), self.db_path, progress=progress)
        self.refresh_status(progress)
        return result

    def transfer_export(self, body):
        """Copy album folders out of the library (stoppable; running it again carries on)."""
        from transfer import service as transfer
        return run_stoppable("export", "files", "Working out what to copy…", self.path, body, lambda progress, finished:
                             transfer.export(self.db_path, body.get("folders") or [], body.get("dest"), safe=bool(body.get("safe", True)),
                                             tracks=body.get("tracks") or [],
                                             skip=set(body.get("skip") or []), finished=finished, progress=progress), unwatched=False)

    # --- lyrics -----------------------------------------------------------------

    def api_lyrics(self, conn, q, track_id=None):
        """With a track id: that song's lyrics for the player. Without: the Lyrics overview."""
        from lyrics import files, service
        root = crud.get_root(conn)
        if track_id is None:
            return service.overview(conn, root)
        track = crud.tracks.get(conn, int(track_id))
        if track is None:
            raise LookupError("no such track")
        found = files.best(root, track["path"], files.embedded_by_track(conn, {track["id"]}).get(track["id"], ()))
        if not found:
            return {"kind": None}
        if found["synced"]:
            return {"kind": "synced", "source": found["source"], "lines": files.parse(found["text"])}
        return {"kind": "plain", "source": found["source"], "text": found["text"]}

    def lyrics_find(self, body):
        from lyrics import service
        return run_stoppable("lyrics", "songs", "Finding songs without lyrics…", self.path, body, lambda progress, finished: service.find(
            self.db_path, upgrade=bool(body.get("upgrade")), refresh=bool(body.get("refresh")),
            skip={int(t) for t in body.get("skip") or []}, finished=finished, progress=progress))

    def lyrics_apply(self, body):
        from lyrics import service
        return service.apply(self.db_path, [int(t) for t in body.get("track_ids") or []], progress=report_progress("lyrics"))

    def lyrics_orphans(self, body):
        from lyrics import service
        return service.fix_orphans(self.db_path, body.get("decisions") or {}, progress=report_progress("lyrics"))

    # --- Navidrome & playback -------------------------------------------------

    def api_navidrome(self, conn, q, what=None):
        from navidrome import service
        if what == "access":  # /api/navidrome/access: addresses for phones and for the admin pages
            return service.access()
        return {**service.status(), **crud.tracks.navidrome_counts(conn, config.INBOX), "linked_at": crud.get_meta(conn, "navidrome_linked"),
                "syncing": NAVIDROME_FOLLOWUP["lock"].locked(), "last_sync": NAVIDROME_FOLLOWUP["last"]}

    def navidrome_sync(self, body):
        from navidrome import service
        return service.sync(self.db_path, progress=report_progress("navidrome"))

    def stream(self, track_id):
        """Play a song: through Navidrome when it's linked (ALAC etc. transcoded to MP3), else from disk."""
        from navidrome import client
        with self.db() as conn:
            track = crud.tracks.get(conn, track_id)
            root = crud.get_root(conn)
        if track is None:
            return self.send(404, {"error": "no such track"})
        playable = track["codec"] in client.BROWSER_CODECS or (track["codec"] or "").startswith("pcm_")
        if track["navidrome_id"] and client.configured():
            try:
                upstream = client.stream(track["navidrome_id"], transcode=not playable, range_header=self.headers.get("Range"))
            except client.NavidromeError as e:
                return self.send(502, {"error": str(e)})
            with upstream:
                self.send_response(upstream.status)
                for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if upstream.headers.get(header):
                        self.send_header(header, upstream.headers[header])
                self.end_headers()
                try:
                    while chunk := upstream.read(STREAM_CHUNK):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser stopped listening (seek, skip, pause): normal
            return None
        if not playable:
            return self.send(415, {"error": "this format needs Navidrome to play in a browser (it transcodes it)"})
        return self.send_file(os.path.join(root, track["path"]))

    def send_file(self, path):
        """Serve a local file with HTTP Range support, so the player can seek."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if match:
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else end
            else:  # suffix range: the last N bytes
                start = max(0, size - int(match.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return None
        end = min(end, size - 1)
        self.send_response(206 if match else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if match:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0 and (chunk := f.read(min(STREAM_CHUNK, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
        return None

    def api_progress(self, conn, q):
        """The running job's progress, and how the last one ended. Asking counts as watching (STOP_UNWATCHED)."""
        WATCHED["at"] = time.monotonic()
        last = JOBS["last"] and {**JOBS["last"], "age": round(time.time() - JOBS["last"]["finished_ts"])}
        return {**PROGRESS, "last": last}

    def cleanup_apply(self, body):
        from cleanup import apply as applier, plan as planner
        progress = report_progress("apply")
        progress(0, 0, "Reading tags…")
        # Re-plan server side; the client only says which tracks to include.
        result = applier.apply(planner.build(self.root()), self.db_path, selected=set(body.get("paths") or []),
                               fix_tags=bool(body.get("tags", True)), rename=bool(body.get("rename", True)),
                               progress=progress, resolutions=body.get("resolutions") or {})
        self.refresh_status(progress)
        return result

    def cleanup_undo(self, body):
        from cleanup import apply as applier
        progress = report_progress("undo")
        result = applier.undo(self.db_path, progress=progress)
        self.refresh_status(progress)
        return result

    # --- ingestion -----------------------------------------------------------

    def api_ingest(self, conn, q):
        """Files on disk the catalog doesn't know, and catalogued songs not yet at their clean location."""
        root = crud.get_root(conn)
        diff = crud.tracks.verify(conn, root, scan.AUDIO_EXTS, skip_dirs={scan.TRASH_DIR})
        def stat(path):
            try:
                return os.path.getsize(os.path.join(root, path))
            except OSError:
                return None
        return {
            "root": root,
            "last_scan": crud.get_meta(conn, "last_scan"),
            "not_ingested": [{"path": p, "folder": os.path.dirname(p), "size": stat(p)} for p in diff["untracked"]],
            "changed": diff["stale"],
            "gone": diff["missing"],
            "downloading": scan.partial_downloads(root),
            "inbox": {"folder": config.INBOX, "songs": scan.inbox_songs(root)} if config.INBOX else None,
            "unprocessed": [{k: t[k] for k in ("id", "path", "clean_path", "title", "artist", "album")}
                            for t in crud.tracks.unprocessed(conn)],
            "mixed": crud.releases.mixed_albums(conn),
        }

    def run_scan(self, body):
        progress = report_progress("scan")
        scan.ensure_inbox(self.root())
        result = scan.scan(self.root(), self.db_path, full=bool(body.get("full")), progress=progress)
        try:
            self.refresh_status(progress)
        except ImportError:  # running without the venv: catalogue only, no processed flags
            pass
        return result

    # --- MusicBrainz -----------------------------------------------------------

    def api_mb(self, conn, q):
        proposals = crud.releases.proposals(conn)
        for p in proposals:
            for t in p["payload"].get("tracks", []):
                t.pop("values", None)
        return {"proposals": proposals, "incomplete": crud.releases.incomplete(conn),
                "mixed": crud.releases.mixed_albums(conn)}

    def api_missing(self, conn, q):
        return crud.releases.missing_for_folder(conn, q.get("folder", ""))

    def mb_fetch(self, body):
        from musicbrainz import service
        return run_stoppable("musicbrainz", "albums", "Finding albums to look up…", self.path, body, lambda progress, finished: service.fetch(
            self.db_path, refresh=bool(body.get("refresh")), skip=set(body.get("skip") or []), finished=finished,
            progress=progress))

    # --- artwork (Operations tab) ---------------------------------------------

    def api_art(self, conn, q):
        from artwork import service
        return {"albums": service.albums(conn, include_covered=q.get("all") == "1")}

    def art_find(self, body):
        from artwork import service
        return run_stoppable("artwork", "albums", "Finding albums without artwork…", self.path, body, lambda progress, finished: service.find(
            self.db_path, online=bool(body.get("online", True)), use_itunes=bool(body.get("itunes")),
            include_covered=bool(body.get("all")), keys=set(body["keys"]) if body.get("keys") else None,
            skip=set(body.get("skip") or []), finished=finished, progress=progress))

    def art_upload(self, body):
        import base64
        import binascii
        from artwork import images, service
        try:
            data = base64.b64decode(body.get("data") or "", validate=True)
        except binascii.Error:
            raise ValueError("the upload was garbled")
        try:
            return service.add_upload(self.db_path, body.get("key"), body.get("name"), data)
        except images.NotAnImage as e:
            raise ValueError(f"{body.get('name') or 'that file'} isn't usable: {e}")

    def art_apply(self, body):
        from artwork import service
        return service.apply(self.db_path, body.get("choices") or {}, replace=bool(body.get("replace")),
                             save_folder=bool(body.get("save_folder", True)), progress=report_progress("artwork"))

    def make_singles(self, body):
        from cleanup import singles
        progress = report_progress("single")
        result = singles.apply(self.db_path, [int(t) for t in body.get("track_ids") or []], progress=progress)
        self.refresh_status(progress)
        return result

    def artist_picture_find(self, body):
        from artwork import artists
        return artists.find(self.db_path, body.get("name") or "", progress=report_progress("artistart"))

    def artist_picture_set(self, body):
        from artwork import artists
        report_progress("artistart")(0, 1, "Saving the picture…")
        return artists.set_picture(self.db_path, body.get("name") or "", body.get("sha"))

    def art_set(self, body):
        """Edit artwork: one image onto a whole album (songs and cover file), or onto chosen songs."""
        from artwork import service
        paths = body.get("paths")
        return service.set_artwork(self.db_path, body.get("folder") or "", body.get("sha"),
                                   paths=None if paths is None else list(paths), progress=report_progress("artwork"))

    def mb_apply(self, body):
        from musicbrainz import service
        progress = report_progress("musicbrainz")
        result = service.apply(self.db_path, body.get("keys") or [], progress=progress)
        self.refresh_status(progress)
        return result

    # --- cover art -----------------------------------------------------------

    def artist_image(self, name):
        from artwork import artists, images
        with self.db() as conn:
            root, artist_folder = crud.get_root(conn), artists.folder(conn, name)
        current = artists.picture_file(root, artist_folder)
        if not current:
            return self.send(404, {"error": "no artist picture"})
        with open(os.path.join(root, artist_folder, current), "rb") as f:
            data = f.read()
        return self.send(200, data, images.probe(data)[0])

    def cover(self, track_id, embedded_only=False):
        """A song's cover: the folder's cover file if there is one, else the picture in the song (with
        `embedded_only`, always the song's own, for showing which songs carry which art)."""
        with self.db() as conn:
            track = crud.tracks.get(conn, track_id)
            root = crud.get_root(conn)
        if track is None:
            return self.send(404, {"error": "no such track"})
        folder = os.path.join(root, track["folder"])
        for name in [] if embedded_only else os.listdir(folder):
            if name.lower() in COVER_NAMES:
                with open(os.path.join(folder, name), "rb") as f:
                    return self.send(200, f.read(), mimetypes.guess_type(name)[0] or "image/jpeg")
        # fall back to the embedded picture
        out = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", os.path.join(root, track["path"]), "-an", "-frames:v", "1",
             "-c:v", "copy", "-f", "image2pipe", "-"],
            capture_output=True,
        )
        if out.returncode == 0 and out.stdout:
            ctype = "image/png" if out.stdout[:4] == b"\x89PNG" else "image/jpeg"
            return self.send(200, out.stdout, ctype)
        self.send(404, {"error": "no cover"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=config.DB_PATH, help="catalog database (default: MUSE_DB)")
    ap.add_argument("--port", type=int, default=config.PORT, help="default: MUSE_PORT")
    ap.add_argument("--host", default=config.HOST, help="default: MUSE_HOST")
    args = ap.parse_args()
    Handler.db_path = os.path.abspath(args.db)
    root = scan.resolve_root(Handler.db_path)
    if not os.path.exists(Handler.db_path):
        print(f"Creating the catalog at {Handler.db_path} and scanning {root} (one-time; later scans only read changes)…", flush=True)
        scan.scan(root, Handler.db_path)
    conn = crud.connect(Handler.db_path)  # adds any new tables/columns before read-only requests use them
    filled = crud.tracks.backfill_mb_ids(conn)  # ids of songs catalogued before those columns existed
    if filled:
        print(f"Filled in MusicBrainz ids for {filled} songs from their stored tags", flush=True)
    crud.set_root(conn, root)  # MUSE_LIBRARY wins, so a moved library only needs .env updated
    conn.commit()
    if os.path.isdir(os.path.join(root, scan.TRASH_DIR)):
        scan.hide_from_navidrome(os.path.join(root, scan.TRASH_DIR))  # discarded duplicates stay out of Navidrome
    scan.ensure_inbox(root)  # new downloads stay out of Navidrome until Clean up moves them into place
    if crud.tracks.needs_status(conn):
        try:
            from cleanup import status
            print("Working out which songs are processed…")
            status.refresh(conn, crud.get_root(conn))
        except ImportError:  # plain python3 without mutagen: left for the next scan under the venv
            pass
    crud.releases.recompute(conn)  # completeness rules may have changed since it was last worked out
    conn.commit()
    try:  # lyrics files can come and go without the songs changing
        from lyrics import service as lyrics_service
        lyrics_service.refresh_status(conn, crud.get_root(conn))
    except ImportError:
        pass
    try:  # albums split across editions get an "align" proposal ready in the MusicBrainz dialog
        from musicbrainz import service as mb_service
        mb_service.propose_alignments(conn, crud.get_root(conn))
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - a convenience: never a reason not to start
        print(f"Couldn't prepare edition fixes ({type(e).__name__}: {e}); they're retried after the next job.", flush=True)
    try:  # undo history past MUSE_UNDO_DAYS, and anything nothing can use any more
        from cleanup import retention
        removed = retention.prune(conn, crud.get_root(conn))
        if removed.get("bytes"):
            print(f"Cleared expired undo history: {removed}", flush=True)
    except ImportError:
        pass
    conn.close()
    navidrome_followup(Handler.db_path)  # link songs to Navidrome (and start it if it's down), in the background
    print(f"Muse on http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
