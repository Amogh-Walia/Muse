"""Where cover candidates come from. Each source returns [{data, label, url, rank}] (lower rank = better guess)."""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from musicbrainz import client as mb_client
from musicbrainz import match

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
SCAN_DIRS = ("scans", "scan", "artwork", "covers", "cover", "images")
MAX_DOWNLOAD = 25_000_000


def _name_rank(name):
    """Guess from a file name how likely it is to be the front cover ('CD-DVD Cover.jpg' is; 'Atif CD.jpg' isn't)."""
    n = name.lower()
    if re.search(r"back|inlay|tray|booklet|insert|matrix|spine|obi", n):
        return 60
    if re.search(r"cover|front|folder|albumart", n):
        return 10
    if re.search(r"disc|\bcd\b", n):
        return 60
    return 30


def folder_images(root, folder):
    """Image files next to the songs (and in a Scans/Artwork subfolder)."""
    base = os.path.join(root, folder)
    found = []
    dirs = [base] + [os.path.join(base, d) for d in sorted(os.listdir(base))
                     if d.lower() in SCAN_DIRS and os.path.isdir(os.path.join(base, d))] if os.path.isdir(base) else []
    for d in dirs:
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if name.lower().endswith(IMAGE_EXTS) and os.path.isfile(path) and os.path.getsize(path) <= MAX_DOWNLOAD:
                with open(path, "rb") as f:
                    found.append({"data": f.read(), "label": os.path.relpath(path, base), "url": None,
                                  "rank": _name_rank(name)})
    return found


def sibling_art(root, songs):
    """Every distinct cover already embedded in the album's songs, the most widely used first.

    Albums pieced together from several downloads often carry different covers on different songs;
    each one is offered, labelled with how many songs use it.
    """
    found = {}  # image bytes -> songs carrying it
    for song in songs:
        if not song["has_cover"]:
            continue
        out = subprocess.run(["ffmpeg", "-v", "quiet", "-i", os.path.join(root, song["path"]), "-an", "-frames:v", "1",
                              "-c:v", "copy", "-f", "image2pipe", "-"], capture_output=True)
        if out.returncode == 0 and out.stdout:
            found.setdefault(out.stdout, []).append(song)
    ranked = sorted(found.items(), key=lambda item: -len(item[1]))
    out = []
    for i, (data, carriers) in enumerate(ranked):
        first = carriers[0]["title"] or carriers[0]["filename"]
        others = f" and {len(carriers) - 1} more song{'s' if len(carriers) > 2 else ''}" if len(carriers) > 1 else ""
        out.append({"data": data, "label": f"Embedded in “{first}”{others}", "url": None, "rank": 5 if i == 0 else 8})
    return out


def _download(url, as_json=False):
    request = urllib.request.Request(url, headers={"User-Agent": mb_client.USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(MAX_DOWNLOAD + 1)
                if len(data) > MAX_DOWNLOAD:
                    return None
                return json.loads(data) if as_json else data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise mb_client.MusicBrainzError(f"{url} said {e.code}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                raise mb_client.MusicBrainzError(f"could not reach {urllib.parse.urlparse(url).netloc}: {e}") from e
        time.sleep(2 ** attempt)
    return None


def cover_art_archive(release_id=None, release_group_id=None, limit=3):
    """Front (then other) covers from coverartarchive.org, 1200 px where available."""
    listing = None
    if release_id:
        listing = _download(f"https://coverartarchive.org/release/{release_id}", as_json=True)
    if not listing and release_group_id:
        listing = _download(f"https://coverartarchive.org/release-group/{release_group_id}", as_json=True)
    images = sorted((listing or {}).get("images", []), key=lambda i: (not i.get("front"), not i.get("approved")))
    out = []
    for image in images[:limit]:
        thumbs = image.get("thumbnails") or {}
        url = thumbs.get("1200") or thumbs.get("large") or image.get("image")
        data = _download(url) if url else None
        if data:
            kinds = ", ".join(image.get("types") or []) or "Image"
            out.append({"data": data, "label": f"Cover Art Archive: {kinds}", "url": url,
                        "rank": 15 if image.get("front") else 55})
    return out


def itunes(album, artist, limit=2):
    """Album covers from the iTunes Search API, kept only if album and artist names really match."""
    term = f"{artist} {match.simplify_album(album)}" if artist else match.simplify_album(album)
    found = _download("https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "album", "media": "music", "limit": 8}), as_json=True) or {}
    out = []
    for r in found.get("results", []):
        same_album = max(match.similarity(r.get("collectionName"), album),
                         match.similarity(r.get("collectionName"), match.simplify_album(album)))
        same_artist = match.similarity(r.get("artistName"), artist) if artist else 1.0
        art = r.get("artworkUrl100")
        if art and same_album >= 0.7 and same_artist >= 0.7:
            data = _download(art.replace("100x100bb", "1200x1200bb"))
            if data:
                out.append({"data": data, "label": f"iTunes: {r.get('collectionName')} ({r.get('artistName')})",
                            "url": art, "rank": 40})
        if len(out) >= limit:
            break
    return out
