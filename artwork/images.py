"""Image checks, the artwork cache, and resizing for embedding (ffprobe/ffmpeg, no extra libraries)."""
import hashlib
import json
import os
import subprocess

import config

CACHE_DIR = config.ART_CACHE
IMAGE_CODECS = {"mjpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "bmp": "image/bmp", "gif": "image/gif"}
EMBED_MAX_PX = 1200          # larger covers are scaled down before embedding
EMBED_MAX_BYTES = 1_500_000  # and recompressed if still bigger than this


class NotAnImage(ValueError):
    pass


def probe(data):
    """(mime, width, height) of image bytes, or NotAnImage."""
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-"],
                         input=data, capture_output=True)
    streams = json.loads(out.stdout or b"{}").get("streams", []) if out.returncode == 0 else []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video or video.get("codec_name") not in IMAGE_CODECS or not video.get("width"):
        raise NotAnImage("not a JPEG, PNG, WebP, BMP or GIF image")
    return IMAGE_CODECS[video["codec_name"]], video["width"], video["height"]


def store(data):
    """Put image bytes in the cache (named by content hash). Returns (sha, mime, width, height)."""
    mime, width, height = probe(data)
    sha = hashlib.sha1(data).hexdigest()
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, sha)
    if not os.path.exists(path):
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    return sha, mime, width, height


def load(sha):
    if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 40:
        raise ValueError("bad image id")
    with open(os.path.join(CACHE_DIR, sha), "rb") as f:
        return f.read()


def for_embedding(data):
    """Return (bytes, mime, width, height) fit to embed: JPEG/PNG, at most EMBED_MAX_PX on a side."""
    mime, width, height = probe(data)
    if mime in ("image/jpeg", "image/png") and max(width, height) <= EMBED_MAX_PX and len(data) <= EMBED_MAX_BYTES:
        return data, mime, width, height
    out = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", "-", "-frames:v", "1",
         "-vf", f"scale='min({EMBED_MAX_PX},iw)':'min({EMBED_MAX_PX},ih)':force_original_aspect_ratio=decrease",
         "-pix_fmt", "yuvj420p", "-q:v", "3", "-c:v", "mjpeg", "-f", "image2pipe", "-"],
        input=data, capture_output=True)
    if out.returncode != 0 or not out.stdout:
        raise NotAnImage("could not convert the image")
    mime, width, height = probe(out.stdout)
    return out.stdout, mime, width, height
