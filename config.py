"""Settings, read from the environment and an optional `.env` file next to this one.

Real environment variables win over `.env`. See `.env.example` for every setting.
"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes, `export` prefix. No dependencies."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()  # trailing comment on an unquoted value
        os.environ.setdefault(key, value)


def _path(name, default):
    """A path setting: ~ expanded, relative paths taken from the project folder."""
    value = os.environ.get(name) or default
    return os.path.join(HERE, os.path.expanduser(value)) if value else None


_load_dotenv(os.path.join(HERE, ".env"))

# The music folder Muse catalogs and organizes. Required to scan; the database remembers it afterwards.
LIBRARY = _path("MUSE_LIBRARY", None)
# The SQLite catalog. Created (with a first scan) if it doesn't exist.
DB_PATH = _path("MUSE_DB", "data/muse.db")
# Undo journals for the cleanup, MusicBrainz and artwork operations.
JOURNAL_DIR = _path("MUSE_JOURNAL_DIR", "data/journal")
# Downloaded and uploaded cover images, named by content hash.
ART_CACHE = _path("MUSE_ART_CACHE", "data/artwork-cache")
# How long a change stays undoable. Older journals expire together with their picture backups and the
# discarded duplicates in .muse-trash (which are then deleted for good). 0 keeps everything.
UNDO_DAYS = float(os.environ.get("MUSE_UNDO_DAYS") or 30)
# Web app address. Keep 127.0.0.1 unless you mean to expose it: the app can rename and retag your files.
HOST = os.environ.get("MUSE_HOST") or "127.0.0.1"
PORT = int(os.environ.get("MUSE_PORT") or 8800)
# Lookups (Scrape Details, artwork, lyrics) stop by themselves, keeping what they found, when no Muse page has
# checked on them for this many seconds (every page open on Muse counts, even in a background tab). 0: never.
STOP_UNWATCHED = float(os.environ.get("MUSE_STOP_UNWATCHED") or 120)
# Contact details (URL or e-mail) for the MusicBrainz User-Agent, which MusicBrainz asks for. Optional.
MB_CONTACT = os.environ.get("MUSE_MB_CONTACT") or ""

# Accent colours of the web app, as (light mode, dark mode). Edited in the Appearance menu, which saves
# them to .env as MUSE_COLOR_<NAME> and MUSE_COLOR_<NAME>_DARK.
COLOR_DEFAULTS = {
    "accent": ("#b4532a", "#e08a5f"),   # buttons, highlights, active tab, progress bars
    "link":   ("#1f6f78", "#6ec3c9"),   # hyperlinks
    "ok":     ("#27ae60", "#4cc38a"),   # success: complete, matched, better values, added text
    "warn":   ("#d4a03b", "#e0b45c"),   # warnings: incomplete, check this, name clashes
    "danger": ("#c0392b", "#ef6b5b"),   # errors, poor matches, removed text
}
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def colors():
    """{name: (light, dark)}: .env / environment values over the defaults, ignoring anything malformed."""
    out = {}
    for name, (light, dark) in COLOR_DEFAULTS.items():
        values = []
        for value, fallback in ((os.environ.get(f"MUSE_COLOR_{name.upper()}"), light),
                                (os.environ.get(f"MUSE_COLOR_{name.upper()}_DARK"), dark)):
            values.append(value.lower() if value and HEX_COLOR.match(value) else fallback)
        out[name] = tuple(values)
    return out


def save_env(updates):
    """Write KEY=VALUE settings into .env: existing lines are replaced in place, new ones are appended under
    an "Appearance" heading, a None value removes the line, and comments and every other setting stay as
    they are. Also applied to the running process, so they take effect without a restart."""
    path = os.path.join(HERE, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    left = dict(updates)
    kept = []
    for line in lines:
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key in updates and not line.lstrip().startswith("#"):
            value = left.pop(key, None)
            if value is None:
                continue  # removed, or a repeat of a key already written
            line = f"{key}={value}"
        kept.append(line)
    lines = kept
    left = {k: v for k, v in left.items() if v is not None}
    if left:
        if not any(line.strip() == "# Appearance (edited from the Appearance menu)" for line in lines):
            lines += ["", "# Appearance (edited from the Appearance menu)"]
        lines += [f"{key}={value}" for key, value in left.items()]
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)


# Navidrome (optional): plays the library through its Subsonic API. Muse rescans it after every change,
# links songs to their Navidrome ids and proxies streams, so the password never reaches the browser.
NAVIDROME_URL = (os.environ.get("NAVIDROME_URL") or "").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER") or ""
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD") or ""
# The address your browser uses for "Open in Navidrome" links, if different from NAVIDROME_URL.
NAVIDROME_PUBLIC_URL = (os.environ.get("NAVIDROME_PUBLIC_URL") or NAVIDROME_URL).rstrip("/")
# systemd user service Muse starts when Navidrome isn't answering. Empty: don't try.
NAVIDROME_SERVICE = os.environ.get("NAVIDROME_SERVICE", "navidrome")
