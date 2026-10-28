"""Pure, regex-based text rules. Nothing in here touches the filesystem."""
import os
import re
import unicodedata

YEAR = r"(?:19|20)\d\d"

# Typographic look-alikes that make names hard to type or search for.
ASCII = {"’": "'", "‘": "'", "“": '"', "”": '"', "‐": "-", "‑": "-", "–": "-", "—": "-", "…": "...", " ": " "}
# Characters that are illegal or awkward in file names (FAT/NTFS/SMB, phones).
UNSAFE = {"/": "-", "\\": "-", ":": " -", "?": "", "*": "", '"': "'", "<": "", ">": "", "|": "-"}
VARIOUS = {"va", "various", "various artists", "apple music"}


def _load_protected():
    path = os.path.join(os.path.dirname(__file__), "protected_artists.txt")
    try:
        with open(path, encoding="utf-8") as f:
            return {line.strip().lower() for line in f if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        return set()


# Artist names that contain a separator but are one act ("Fitz and The Tantrums").
PROTECTED = _load_protected()


def normalize(text):
    """Unicode NFC, ASCII punctuation, tidy whitespace: 'Aadat (From  Kalyug )' -> 'Aadat (From Kalyug)'."""
    text = unicodedata.normalize("NFC", str(text))
    for bad, good in ASCII.items():
        text = text.replace(bad, good)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]},])", r"\1", text)
    return text.strip()


# --- artists -------------------------------------------------------------------

# Separators that always mean "another artist".
STRONG_SEP = re.compile(r"\s*;\s*|\s+/\s*|\s*/\s+|\s+(?:featuring|feat\.?|ft\.?)\s+", re.I)
# Separators that usually do, unless the whole name is protected.
WEAK_SEP = re.compile(r"\s*,\s+|\s+&\s+|\s+\+\s+")
# 'and' is only split inside a featured list ("feat. RBX and Sticky Fingaz"); bands love it.
AND_SEP = re.compile(r"\s+and\s+", re.I)
FEAT_WORD = re.compile(r"\s+(?:featuring|feat\.?|ft\.?)\s+", re.I)


def _split_weak(chunk, allow_and):
    # Shield protected names inside a longer list ("Vishal & Shekhar, Piyush Mishra") before splitting.
    shielded = []

    def shield(m):
        shielded.append(m.group(0))
        return f"\x00{len(shielded) - 1}\x00"

    for name in sorted(PROTECTED, key=len, reverse=True):
        chunk = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", shield, chunk, flags=re.I)
    out = []
    for piece in WEAK_SEP.split(chunk):
        out.extend(AND_SEP.split(piece) if allow_and else [piece])
    return [re.sub(r"\x00(\d+)\x00", lambda m: shielded[int(m.group(1))], p) for p in out]


def split_artists(values):
    """['Eminem feat. Dr. Dre, Snoop Dogg and Nate Dogg'] -> ['Eminem', 'Dr. Dre', 'Snoop Dogg', 'Nate Dogg'].

    Accepts a string or a list (multi-valued tags). Order is kept, duplicates dropped.
    """
    if isinstance(values, str):
        values = [values]
    out = []
    for value in values:
        value = normalize(value)
        if value.lower() in PROTECTED:
            out.append(value)
            continue
        main, *featured = FEAT_WORD.split(value, maxsplit=1)
        for chunk in STRONG_SEP.split(main):
            out.extend(_split_weak(chunk, allow_and=False))
        for chunk in featured:
            for sub in STRONG_SEP.split(chunk):
                out.extend(_split_weak(sub, allow_and=True))
    return dedupe(out)


def dedupe(names):
    seen, out = set(), []
    for name in names:
        name = name.strip(" ,;&")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def is_various(name):
    return normalize(name).lower() in VARIOUS


# --- titles & albums -----------------------------------------------------------

# "(feat. X)", "[ft. X]" anywhere, or a bare trailing "feat. X" before the next bracket/end.
FEAT_IN_TITLE = re.compile(
    r"\s*[(\[]\s*(?:featuring|feat\.?|ft\.?)\s+([^)\]]+)[)\]]"
    r"|\s+(?:featuring|feat\.?|ft\.?)\s+(.+?)(?=\s*[(\[]|$)",
    re.I,
)
# "(2014)", "[2014]", "{2014}"
BRACKET_YEAR = re.compile(rf"\s*[(\[{{]\s*{YEAR}\s*[)\]}}]")
# "(Remastered 2016)", "(2010 Digital Remaster)", " - 2011 Remaster"
REMASTER = re.compile(
    rf"\s*[(\[][^()\[\]]*\b(?:{YEAR}\b[^()\[\]]*\bremaster(?:ed)?|remaster(?:ed)?\b[^()\[\]]*\b{YEAR})\b[^()\[\]]*[)\]]"
    rf"|\s+-\s+(?:{YEAR}\s+)?(?:digital\s+)?remaster(?:ed)?(?:\s+{YEAR})?(?:\s+version)?$",
    re.I,
)
# "2013 - Random Access Memories", "[2014] Ek Villain" at the start of an album name
LEADING_YEAR = re.compile(rf"^\s*[(\[]?{YEAR}[)\]]?\s*(?:-|\.|(?<=[)\]]))\s*")
# Bracket groups made only of format/quality junk: "[FLAC]", "{24-96}", "[24B-44.1kHz]", "(flac)"
JUNK_TOKEN = r"(?:flac|mp3|aac|alac|wav|web|cd|vinyl|lossless|hi-?res|320|v0|\d+\s*-?bits?|\d+b|\d+(?:\.\d+)?\s*khz|\d+-\d+(?:\.\d+)?)"
FORMAT_JUNK = re.compile(rf"\s*[(\[{{]\s*(?:{JUNK_TOKEN}[\s,/-]*)+[)\]}}]", re.I)


def strip_feat(title):
    """'Middle (Feat. Bipolar Sunshine)' -> ('Middle', ['Bipolar Sunshine'])."""
    featured = []

    def grab(m):
        featured.extend(split_artists("x feat. " + (m.group(1) or m.group(2)))[1:])
        return ""

    return FEAT_IN_TITLE.sub(grab, title), featured


def strip_years(text):
    """Remove years that are decoration rather than part of the name."""
    text = REMASTER.sub("", text)
    return BRACKET_YEAR.sub("", text)


def clean_title(title):
    """Returns (clean title, featured artists pulled out of it)."""
    title, featured = strip_feat(normalize(title))
    return normalize(strip_years(title)), featured


def clean_album(album):
    album = normalize(album)
    album, _ = strip_feat(album)
    album = FORMAT_JUNK.sub("", album)
    album = strip_years(album)
    album = LEADING_YEAR.sub("", album)
    album = re.sub(r"\s+-\s+single$", "", album, flags=re.I)
    album = re.sub(r"\s+-\s+(EP|LP)$", r" \1", album)
    return normalize(album)


def clean_date(date):
    """'2014-00-00' -> '2014'."""
    return re.sub(r"(-00)+$", "", normalize(date))


def year_of(date):
    m = re.match(rf"^({YEAR})", date or "")
    return int(m.group(1)) if m else None


# --- file names ----------------------------------------------------------------

def safe_name(text):
    """Make a string usable as a single path component on any filesystem."""
    text = normalize(text)
    for bad, good in UNSAFE.items():
        text = text.replace(bad, good)
    return re.sub(r"\s+", " ", text).strip().rstrip(". ") or "Unknown"
