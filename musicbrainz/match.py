"""Pick the MusicBrainz release for a local album and pair its tracks with the local files."""
import difflib
import re

from cleanup import rules


def _norm(text):
    text = rules.clean_title(text or "")[0].lower()
    return re.sub(r"[^\w]+", " ", text).strip()


def similarity(a, b):
    a, b = _norm(a), _norm(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def credit_names(artist_credit):
    return [rules.normalize(c["name"]) for c in artist_credit or []]


def credit_ids(artist_credit):
    return [c["artist"]["id"] for c in artist_credit or [] if c.get("artist")]


def _track_count(release):
    return release.get("track-count") or sum(m.get("track-count", 0) for m in release.get("media", []))


def simplify_album(album):
    """'Bangarang EP' -> 'Bangarang', 'Badlands (Deluxe Edition)' -> 'Badlands': MusicBrainz keeps these out of titles."""
    album = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", album)
    album = re.sub(r"\s+-?\s*(EP|LP|Single)$", "", album, flags=re.I)
    return album.strip() or album


def artist_similarity(candidate, artist):
    if not artist or rules.is_various(artist):
        return 1.0
    names = credit_names(candidate.get("artist-credit"))
    credit = "".join(c["name"] + c.get("joinphrase", "") for c in candidate.get("artist-credit") or [])
    return max([similarity(n, artist) for n in names] + [similarity(credit, artist), 0.0])


def rank(candidate, album, artist, local_count, year):
    """Combine MusicBrainz's search score with how well the release fits what's on disk."""
    score = int(candidate.get("score", 0))
    artist_match = artist_similarity(candidate, artist)
    score += 40 * artist_match - (60 if artist_match < 0.6 else 0)
    n = _track_count(candidate)
    if n == local_count:
        score += 25
    elif n > local_count:          # we may simply be missing songs
        score += max(0, 15 - (n - local_count))
    else:                          # release is shorter than what we have: probably the wrong edition
        score -= 30
    if year and (candidate.get("date") or "")[:4] == str(year):
        score += 10
    if candidate.get("status") == "Official":
        score += 5
    return score + 20 * max(similarity(candidate.get("title"), album),
                            similarity(candidate.get("title"), simplify_album(album)))


def tracklist(release):
    """Every track on the release, flattened, with disc and position."""
    out = []
    for medium in release.get("media", []):
        for t in medium.get("tracks", []):
            out.append({"id": t["id"], "disc": medium.get("position") or 1, "track": t.get("position"),
                        "discs": len(release.get("media", [])), "tracks_on_disc": medium.get("track-count"),
                        "title": t.get("title"), "length": (t.get("length") or 0) / 1000 or None,
                        "recording": (t.get("recording") or {}).get("id"),
                        "artist_credit": t.get("artist-credit") or release.get("artist-credit"),
                        "format": medium.get("format")})
    return out


def pair_tracks(local, mb_tracks):
    """Greedy best-first pairing of local songs with release tracks.

    Each pair is scored on title similarity, matching disc/track position and duration.
    Returns (pairs [(local, mb, score)], local songs left over, release tracks left over).
    """
    candidates = []
    for i, song in enumerate(local):
        for j, t in enumerate(mb_tracks):
            title = similarity(song["title"], t["title"])
            position = song.get("track_no") == t["track"] and (song.get("disc_no") or 1) == t["disc"]
            if song.get("duration") and t["length"]:
                timing = max(0.0, 1 - abs(song["duration"] - t["length"]) / 10)
            else:
                timing = 0.5
            score = 0.6 * title + 0.3 * position + 0.1 * timing
            if title >= 0.6 or (position and title >= 0.35):
                candidates.append((score, i, j))
    used_local, used_mb, pairs = set(), set(), []
    for score, i, j in sorted(candidates, reverse=True):
        if i not in used_local and j not in used_mb:
            used_local.add(i)
            used_mb.add(j)
            pairs.append((local[i], mb_tracks[j], round(score, 3)))
    extras = [s for i, s in enumerate(local) if i not in used_local]
    missing = [t for j, t in enumerate(mb_tracks) if j not in used_mb]
    return pairs, extras, missing


def release_summary(release):
    group = release.get("release-group") or {}
    labels = [li for li in release.get("label-info") or [] if li.get("label")]
    names = credit_names(release.get("artist-credit"))
    tracks = tracklist(release)
    return {
        "id": release["id"],
        "release_group_id": group.get("id"),
        "title": rules.clean_album(release.get("title") or ""),
        "artist": names[0] if names else None,
        "artist_credit": "".join(c["name"] + c.get("joinphrase", "") for c in release.get("artist-credit") or []),
        "artist_ids": credit_ids(release.get("artist-credit")),
        # the artists' own names: a release may credit them differently ("Vishal & Shekhar" for Vishal–Shekhar)
        "artist_names": [rules.normalize(c["artist"]["name"]) for c in release.get("artist-credit") or [] if c.get("artist")],
        "date": release.get("date"),
        "original_date": group.get("first-release-date"),
        "country": release.get("country"),
        "status": release.get("status"),
        "type": group.get("primary-type"),
        "label": ", ".join(dict.fromkeys(li["label"]["name"] for li in labels)) or None,
        "labels": list(dict.fromkeys(li["label"]["name"] for li in labels)),
        "catalog_numbers": [li["catalog-number"] for li in labels if li.get("catalog-number")],
        "barcode": release.get("barcode") or None,
        "track_count": len(tracks),
        "disc_count": len(release.get("media", [])),
        "url": f"https://musicbrainz.org/release/{release['id']}",
        "tracklist": [{"id": t["id"], "disc": t["disc"], "track": t["track"], "title": rules.normalize(t["title"] or "")}
                      for t in tracks],
        "tracks": tracks,  # full per-track details, so songs can later be aligned without another lookup
    }


def tag_values(summary, t):
    """The tags a local file paired with release track `t` should carry (empty MusicBrainz data is skipped)."""
    artists = credit_names(t["artist_credit"])
    values = {
        "title": [rules.clean_title(t["title"] or "")[0]],
        "artist": artists[:1],
        "artists": artists if len(artists) > 1 else [],
        "album": [summary["title"]],
        "albumartist": [summary["artist"]] if summary["artist"] else [],
        "date": [summary["date"]] if summary["date"] else [],
        "originaldate": [summary["original_date"]] if summary["original_date"] else [],
        "tracknumber": [str(t["track"])],
        "tracktotal": [str(t["tracks_on_disc"])] if t["tracks_on_disc"] else [],
        "discnumber": [str(t["disc"])],
        "disctotal": [str(t["discs"])],
        "musicbrainz_albumid": [summary["id"]],
        "musicbrainz_releasegroupid": [summary["release_group_id"]] if summary["release_group_id"] else [],
        "musicbrainz_releasetrackid": [t["id"]],
        "musicbrainz_trackid": [t["recording"]] if t["recording"] else [],
        "musicbrainz_artistid": credit_ids(t["artist_credit"]),
        "musicbrainz_albumartistid": summary["artist_ids"],
        "label": summary["labels"],
        "catalognumber": summary["catalog_numbers"],
        "barcode": [summary["barcode"]] if summary["barcode"] else [],
        "releasecountry": [summary["country"]] if summary["country"] else [],
        "releasestatus": [summary["status"].lower()] if summary["status"] else [],
        "releasetype": [summary["type"].lower()] if summary["type"] else [],
        "media": [t["format"]] if t["format"] else [],
    }
    return {k: v for k, v in values.items() if v}
