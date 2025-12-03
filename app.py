import textwrap
from typing import List, Dict, Tuple, Optional
from difflib import SequenceMatcher

from flask import Flask, request, jsonify, Response
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import requests

# ---------------------------------------------------------
#  CONFIG - SET YOUR SPOTIFY APP CREDENTIALS HERE
# ---------------------------------------------------------

import os

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

if SPOTIFY_CLIENT_ID.startswith("YOUR_") or SPOTIFY_CLIENT_SECRET.startswith("YOUR_"):
    print("WARNING: You must set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in the environment")

auth_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
)
sp = spotipy.Spotify(auth_manager=auth_manager)

RECCOBEATS_BASE_URL = "https://api.reccobeats.com"

# ---------------------------------------------------------
#  BASIC HELPERS
# ---------------------------------------------------------

def chunk_list(seq: List[str], size: int) -> List[List[str]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def normalize_separators(line: str) -> str:
    # normalise different dash characters
    return (
        line.replace("–", "-")
        .replace("—", "-")
        .replace("：", ":")
        .replace("；", ";")
    )


def clean_line(line: str) -> str:
    if not line:
        return ""
    import re
    result = line.strip()
    if not result:
        return ""

    # strip leading numbering like "1.", "A2 ", "B1 -"
    result = re.sub(r"^\s*[A-D]?\d+\s*[\.\-\)]\s*", "", result, flags=re.IGNORECASE)
    result = re.sub(r"^\s*[A-D]?\d+\s+", "", result, flags=re.IGNORECASE)

    # strip "Side A" etc
    result = re.sub(r"^Side\s+[A-D]\s*:?", "", result, flags=re.IGNORECASE).strip()

    # Discogs-style artist asterisk: Orlandivo* -> Orlandivo
    result = re.sub(r"\*+\s*(?=-\s|$)", "", result)

    # remove Discogs style bracket metadata at end
    result = re.sub(
        r"\s*\((?:LP|Album|EP|Single)[^)]*\)\s*$",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"\s*\([^)]*(Reissue|Remastered|Press|JP-|US-|UK-)[^)]*\)\s*$",
        "",
        result,
        flags=re.IGNORECASE,
    )

    # remove trailing [codes]
    result = re.sub(r"\s*\[[^\]]*\]\s*$", "", result)

    # remove trailing remix / version tags
    result = re.sub(
        r"\s+\[(Remastered|Remix|Edit|Version|Bonus Track)\]$",
        "",
        result,
        flags=re.IGNORECASE,
    )

    # kill lines that are just ASCII dividers or box drawing
    if re.fullmatch(r"[-_~=\*\u2500-\u257F\.\s]+", result):
        return ""

    # remove obvious non musical layout words at the very end
    result = re.sub(
        r"\s*\b(FOURSIDER|STEREO|MONO|QUADROPHONIC|33⅓|33RPM|45RPM)\b\s*$",
        "",
        result,
        flags=re.IGNORECASE,
    )

    # collapse multiple spaces
    result = re.sub(r"\s{2,}", " ", result)

    return result.strip()


def clean_and_normalize(raw: str) -> List[str]:
    lines: List[str] = []
    for line in raw.splitlines():
        line = normalize_separators(line)
        line = clean_line(line)
        if line:
            lines.append(line)
    return lines


# ---------------------------------------------------------
#  MUSICAL KEY HELPERS
# ---------------------------------------------------------

PITCH_CLASS_MAP = {
    0: "C",
    1: "C♯/D♭",
    2: "D",
    3: "D♯/E♭",
    4: "E",
    5: "F",
    6: "F♯/G♭",
    7: "G",
    8: "G♯/A♭",
    9: "A",
    10: "A♯/B♭",
    11: "B",
}

# simple Camelot mapping
CAMELOT_MAP = {
    (0, 1): "8B",
    (1, 1): "3B",
    (2, 1): "10B",
    (3, 1): "5B",
    (4, 1): "12B",
    (5, 1): "7B",
    (6, 1): "2B",
    (7, 1): "9B",
    (8, 1): "4B",
    (9, 1): "11B",
    (10, 1): "6B",
    (11, 1): "1B",
    (0, 0): "5A",
    (1, 0): "12A",
    (2, 0): "7A",
    (3, 0): "2A",
    (4, 0): "9A",
    (5, 0): "4A",
    (6, 0): "11A",
    (7, 0): "6A",
    (8, 0): "1A",
    (9, 0): "8A",
    (10, 0): "3A",
    (11, 0): "10A",
}


def key_mode_to_camelot(key: int, mode: int) -> Tuple[str, str]:
    """
    key 0-11, mode 1=major 0=minor.
    """
    if key is None or mode is None:
        return "", ""
    if key < 0 or key > 11:
        return "", ""
    camelot = CAMELOT_MAP.get((key, mode), "")
    key_name = PITCH_CLASS_MAP.get(key, "")
    suffix = "maj" if mode == 1 else "min"
    full_name = f"{key_name} {suffix}" if key_name else ""
    return camelot, full_name


# ---------------------------------------------------------
#  PARSE "Artist - Title mm:ss" LINES
# ---------------------------------------------------------

def parse_artist_title_duration(line: str, inferred_artist: str = "") -> Tuple[str, str, Optional[int]]:
    """
    Returns (artist, title, duration_seconds or None).

    Handles:
      'Stan Getz, Charlie Byrd - Desafinado 1:59'
      'João Gilberto – Bim Bom'
      'Sérgio Mendes– Oba-La-La 2:26'
      'azymuth	- Club Morrocco	4:14' (tab-separated)
      '8		Dear Limmertz	4:34' (track number, no artist)
      'Maracana' (just track name, no artist)
    """
    import re

    original_line = line.strip()
    line = normalize_separators(line)
    
    # Extract duration first (before any processing)
    duration_sec: Optional[int] = None
    duration_match = re.search(r"(\d{1,2}):(\d{2})\s*$", line)
    if duration_match:
        mins = int(duration_match.group(1))
        secs = int(duration_match.group(2))
        duration_sec = mins * 60 + secs
        # Remove duration from line for further processing
        line = line[:duration_match.start()].rstrip()

    # Handle tab-separated values (like discogs exports)
    if "\t" in original_line:
        parts = [p.strip() for p in original_line.split("\t") if p.strip()]
        if len(parts) >= 1:
            # Remove duration from last part if present
            if parts[-1] and re.match(r"^\d{1,2}:\d{2}$", parts[-1]):
                parts = parts[:-1]
            
            # Check if first part is just a track number
            is_track_number = False
            if parts[0] and re.match(r"^\d+$", parts[0]):
                is_track_number = True
                parts = parts[1:]
            
            if len(parts) >= 2:
                # Multiple parts - check if first part contains " - " or "-"
                first_part = parts[0]
                if " - " in first_part or (first_part.endswith("-") and len(parts) > 1):
                    # Format: "artist -" or "artist - track"
                    if first_part.endswith("-"):
                        artist = first_part[:-1].strip()
                        title = " ".join(parts[1:]).strip()
                    else:
                        # Has " - " in first part
                        subparts = first_part.split(" - ", 1)
                        if len(subparts) == 2:
                            artist = subparts[0].strip()
                            title = (subparts[1] + " " + " ".join(parts[1:])).strip()
                        else:
                            artist = first_part.strip()
                            title = " ".join(parts[1:]).strip()
                else:
                    # Format: "artist track" (no dash)
                    artist = parts[0].strip()
                    title = " ".join(parts[1:]).strip()
            elif len(parts) == 1:
                # Only one part after removing track number
                if is_track_number:
                    # Track number was removed, this is just the track name
                    artist = inferred_artist if inferred_artist else ""
                    title = parts[0].strip()
                elif " - " in parts[0] or "-" in parts[0]:
                    # Has separator, parse normally
                    line = parts[0]
                    artist = ""
                    title = ""
                else:
                    # No separator - treat as track name if we have inferred artist
                    artist = inferred_artist if inferred_artist else ""
                    title = parts[0].strip()
            else:
                artist = ""
                title = ""
        else:
            artist = ""
            title = ""
    else:
        # Remove leading track numbers (e.g., "8", "9", "10")
        line = re.sub(r"^\d+\s+", "", line.strip())

        # split artist vs title part
        artist = ""
        title = line.strip()

        if " - " in line:
            parts = line.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        elif "-" in line:
            parts = line.split("-", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
        elif ":" in line and not re.search(r"\d{1,2}:\d{2}", line):
            # Only use colon as separator if it's not a time format
            parts = line.split(":", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        # Discogs-style artist asterisk
        artist = artist.rstrip("*").strip()
        
        # If no artist found but we have an inferred artist, use it
        if not artist and inferred_artist:
            artist = inferred_artist
            # The whole line (minus duration) is the title
            if not title or title == line:
                title = original_line
                # Remove duration from title if present
                duration_match = re.search(r"(\d{1,2}):(\d{2})\s*$", title)
                if duration_match:
                    title = title[:duration_match.start()].rstrip()
                # Remove leading track numbers
                title = re.sub(r"^\d+\s+", "", title.strip())

    title = title.strip()
    
    if not artist and not inferred_artist:
        # no clear artist -> whole line treated as title
        return "", title, duration_sec

    # Use inferred artist if we still don't have one
    if not artist and inferred_artist:
        artist = inferred_artist

    return artist, title, duration_sec


def parse_lines_with_artist_inference(lines: List[str]) -> List[Tuple[str, str, Optional[int]]]:
    """
    Parse multiple lines, inferring artist from previous lines when not specified.
    Returns list of (artist, title, duration_seconds or None).
    """
    results = []
    last_artist = ""
    
    for line in lines:
        artist, title, duration = parse_artist_title_duration(line, last_artist)
        
        # Update last_artist if we found one
        if artist:
            last_artist = artist
        
        results.append((artist, title, duration))
    
    return results


# ---------------------------------------------------------
#  FUZZY MATCH UTIL
# ---------------------------------------------------------

def similarity(a: str, b: str) -> float:
    a = a.lower().strip()
    b = b.lower().strip()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def re_split_multi(s: str, seps: List[str]) -> List[str]:
    import re
    pattern = "|".join(re.escape(sep) for sep in seps)
    return re.split(pattern, s)


def best_artist_similarity(requested: str, candidate_artists: List[str]) -> float:
    if not requested:
        return 0.0
    requested = requested.lower()
    parts = [
        p.strip()
        for p in re_split_multi(requested, [",", "&", "feat.", "ft.", "featuring"])
        if p.strip()
    ]
    best = 0.0
    for cand in candidate_artists:
        c = cand.lower().strip()
        for r in parts:
            best = max(best, similarity(r, c))
    return best


# ---------------------------------------------------------
#  SPOTIFY SEARCH HELPERS WITH SCORING
# ---------------------------------------------------------

def search_album(artist: str, album: str):
    query = album
    if artist:
        query = f'album:"{album}" artist:"{artist}"'
    results = sp.search(q=query, type="album", limit=10)
    items = results.get("albums", {}).get("items", [])
    if not items and artist:
        # looser search
        results = sp.search(q=f"{artist} {album}", type="album", limit=10)
        items = results.get("albums", {}).get("items", [])
    if not items:
        return None
    items.sort(key=lambda a: (a.get("popularity", 0), a.get("total_tracks", 0)), reverse=True)
    return items[0]


def search_albums_multiple(artist: str, album: str, limit: int = 10) -> List[dict]:
    """Search for albums and return multiple results sorted by relevance."""
    query = album
    if artist:
        query = f'album:"{album}" artist:"{artist}"'
    results = sp.search(q=query, type="album", limit=limit)
    items = results.get("albums", {}).get("items", [])
    if not items and artist:
        # looser search
        results = sp.search(q=f"{artist} {album}", type="album", limit=limit)
        items = results.get("albums", {}).get("items", [])
    if not items:
        return []
    items.sort(key=lambda a: (a.get("popularity", 0), a.get("total_tracks", 0)), reverse=True)
    return items


def search_track(artist: str, title: str, duration_sec: Optional[int] = None):
    # build query
    if artist and title:
        query = f'track:"{title}" artist:"{artist}"'
    elif title:
        query = f'track:"{title}"'
    else:
        return None

    results = sp.search(q=query, type="track", limit=10)
    items = results.get("tracks", {}).get("items", [])
    if not items and artist:
        # looser fallback
        results = sp.search(q=f"{artist} {title}", type="track", limit=10)
        items = results.get("tracks", {}).get("items", [])
    if not items:
        return None

    # score candidates
    best = None
    best_score = -1.0

    for cand in items:
        cand_title = cand.get("name", "")
        cand_artists = [a.get("name", "") for a in cand.get("artists", [])]
        cand_dur_ms = cand.get("duration_ms")

        title_score = similarity(title, cand_title)

        artist_score = 0.0
        if artist:
            artist_score = best_artist_similarity(artist, cand_artists)

        # duration penalty
        dur_penalty = 0.0
        if duration_sec is not None and cand_dur_ms:
            cand_sec = int(round(cand_dur_ms / 1000))
            diff = abs(cand_sec - duration_sec)
            if diff > 5:
                dur_penalty = min(0.5, diff / 60.0)

        score = 0.6 * title_score + 0.4 * artist_score - dur_penalty

        if score > best_score:
            best_score = score
            best = cand

    if best_score < 0.55:
        return None

    return best


def search_tracks_multiple(artist: str, title: str, duration_sec: Optional[int] = None, limit: int = 10) -> List[Tuple[dict, float]]:
    """Search for tracks and return multiple results with scores."""
    # build query
    if artist and title:
        query = f'track:"{title}" artist:"{artist}"'
    elif title:
        query = f'track:"{title}"'
    else:
        return []

    results = sp.search(q=query, type="track", limit=limit)
    items = results.get("tracks", {}).get("items", [])
    if not items and artist:
        # looser fallback
        results = sp.search(q=f"{artist} {title}", type="track", limit=limit)
        items = results.get("tracks", {}).get("items", [])
    if not items:
        return []

    # score all candidates
    scored_items = []
    for cand in items:
        cand_title = cand.get("name", "")
        cand_artists = [a.get("name", "") for a in cand.get("artists", [])]
        cand_dur_ms = cand.get("duration_ms")

        title_score = similarity(title, cand_title)

        artist_score = 0.0
        if artist:
            artist_score = best_artist_similarity(artist, cand_artists)

        # duration penalty
        dur_penalty = 0.0
        if duration_sec is not None and cand_dur_ms:
            cand_sec = int(round(cand_dur_ms / 1000))
            diff = abs(cand_sec - duration_sec)
            if diff > 5:
                dur_penalty = min(0.5, diff / 60.0)

        score = 0.6 * title_score + 0.4 * artist_score - dur_penalty
        scored_items.append((cand, score))

    # Sort by score descending
    scored_items.sort(key=lambda x: x[1], reverse=True)
    # Filter out very low scores
    return [(item, score) for item, score in scored_items if score >= 0.3]


# ---------------------------------------------------------
#  RECCOBEATS - AUDIO FEATURES BY SPOTIFY ID
# ---------------------------------------------------------

def get_reccobeats_features_for_spotify_ids(spotify_ids: List[str]) -> Dict[str, dict]:
    """
    Call ReccoBeats /v1/audio-features with a list of Spotify track IDs.
    Returns a dict: spotify_track_id -> feature dict.
    """
    features_by_sp_id: Dict[str, dict] = {}
    if not spotify_ids:
        return features_by_sp_id

    for chunk in chunk_list(spotify_ids, 40):
        params = {"ids": ",".join(chunk)}
        try:
            resp = requests.get(
                f"{RECCOBEATS_BASE_URL}/v1/audio-features",
                params=params,
                headers={"Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print("ReccoBeats error in audio-features:", e)
            continue

        for item in data.get("content", []):
            href = item.get("href") or ""
            sp_id = None
            if "open.spotify.com/track/" in href:
                sp_id = href.split("/track/")[-1].split("?")[0]
            else:
                sp_id = item.get("id")
            if sp_id:
                features_by_sp_id[sp_id] = item

    return features_by_sp_id


# ---------------------------------------------------------
#  FLASK APP - FRONTEND
# ---------------------------------------------------------

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index() -> Response:
    html = textwrap.dedent(
        """
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="utf-8" />
          <title>Vinyl BPM and Key Sheets</title>
          <style>
            :root {
              --bg: #111;
              --card-bg: #181818;
              --accent: #52d273;
              --accent-soft: #303030;
              --text: #f4f4f4;
              --muted: #aaaaaa;
              --danger: #ff6b6b;
              --border-radius: 12px;
            }
            * {
              box-sizing: border-box;
            }
            body {
              margin: 0;
              font-family: system-ui, -apple-system, BlinkMacSystemFont, "Inter", sans-serif;
              background: radial-gradient(circle at top, #222 0, #050505 60%);
              color: var(--text);
              min-height: 100vh;
            }
            .page {
              max-width: 1100px;
              margin: 0 auto;
              padding: 24px 16px 48px;
            }
            h1 {
              font-size: 28px;
              margin: 0 0 8px;
            }
            .subtitle {
              color: var(--muted);
              font-size: 14px;
              margin-bottom: 24px;
            }
            .panel {
              background: #141414;
              border-radius: var(--border-radius);
              padding: 16px 18px 18px;
              box-shadow: 0 16px 40px rgba(0,0,0,0.6);
              margin-bottom: 18px;
            }
            .panel h2 {
              font-size: 16px;
              margin: 0 0 8px;
            }
            textarea {
              width: 100%;
              min-height: 120px;
              background: #101010;
              border-radius: 10px;
              border: 1px solid #282828;
              color: var(--text);
              padding: 10px 12px;
              font-family: "SF Mono", ui-monospace, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
              font-size: 13px;
              resize: vertical;
            }
            textarea:focus {
              outline: none;
              border-color: var(--accent);
              box-shadow: 0 0 0 1px rgba(82,210,115,0.4);
            }
            .row {
              display: flex;
              flex-wrap: wrap;
              align-items: center;
              gap: 12px;
              margin-top: 10px;
            }
            .mode-toggle {
              display: inline-flex;
              align-items: center;
              background: #101010;
              border-radius: 999px;
              padding: 3px;
              border: 1px solid #333;
            }
            .mode-btn {
              border-radius: 999px;
              padding: 4px 10px;
              font-size: 12px;
              border: none;
              cursor: pointer;
              background: transparent;
              color: var(--muted);
              transition: background 0.15s, color 0.15s;
            }
            .mode-btn.active {
              background: var(--accent);
              color: #000;
            }
            .select-group {
              display: inline-flex;
              align-items: center;
              gap: 6px;
              font-size: 13px;
              color: var(--muted);
            }
            select {
              background: #101010;
              border-radius: 999px;
              border: 1px solid #333;
              color: var(--text);
              padding: 4px 8px;
              font-size: 13px;
            }
            .btn {
              border-radius: 999px;
              border: none;
              padding: 6px 14px;
              font-size: 13px;
              background: var(--accent);
              color: #000;
              cursor: pointer;
              display: inline-flex;
              align-items: center;
              gap: 6px;
            }
            .btn:disabled {
              opacity: 0.5;
              cursor: default;
            }
            .btn-outline {
              background: transparent;
              border: 1px solid #333;
              color: var(--muted);
            }
            .btn-outline:hover {
              border-color: var(--accent);
              color: var(--accent);
            }
            .status {
              font-size: 12px;
              color: var(--muted);
              margin-top: 6px;
            }
            .cards {
              margin-top: 18px;
              display: grid;
              grid-template-columns: repeat(2, minmax(0, 1fr));
              gap: 10px;
              page-break-inside: avoid;
            }
            .cards.full-width {
              grid-template-columns: 1fr;
            }
            .column-toggles label {
              cursor: move;
              position: relative;
              padding-left: 18px;
            }
            .column-toggles label::before {
              content: "⋮⋮";
              position: absolute;
              left: 0;
              color: var(--muted);
              font-size: 10px;
              line-height: 1;
            }
            .column-toggles label:hover::before {
              color: var(--accent);
            }
            .column-toggles label.dragging {
              opacity: 0.5;
              background: var(--accent-soft);
            }
            .drag-handle {
              cursor: move;
              user-select: none;
              font-size: 14px;
              color: var(--muted);
              padding: 0 4px;
            }
            .drag-handle:hover {
              color: var(--accent);
            }
            .card {
              border-radius: 10px;
              background: #111;
              border: 1px solid #2b2b2b;
              padding: 10px 10px 8px;
              display: flex;
              flex-direction: column;
              gap: 6px;
              cursor: move;
            }
            .card.dragging {
              opacity: 0.5;
            }
            tr {
              cursor: move;
            }
            tr.dragging {
              opacity: 0.5;
              background: #1a1a1a;
            }
            tr.drag-over {
              border-top: 2px solid var(--accent);
            }
            .card-header {
              display: flex;
              justify-content: space-between;
              align-items: center;
              gap: 6px;
              margin-bottom: 4px;
            }
            .card-header-main {
              display: flex;
              flex-direction: column;
              gap: 2px;
            }
            .card-title {
              font-size: 13px;
              font-weight: 600;
            }
            .card-subtitle {
              font-size: 11px;
              color: var(--muted);
            }
            .card-title[contenteditable="true"],
            .card-subtitle[contenteditable="true"] {
              outline: none;
              border-bottom: 1px dotted transparent;
            }
            .card-title[contenteditable="true"]:focus,
            .card-subtitle[contenteditable="true"]:focus {
              border-bottom-color: #555;
            }
            .card-actions {
              display: inline-flex;
              align-items: center;
              gap: 6px;
            }
            .card-columns-modal {
              position: fixed;
              top: 0;
              left: 0;
              right: 0;
              bottom: 0;
              background: rgba(0,0,0,0.8);
              z-index: 2000;
              display: flex;
              align-items: center;
              justify-content: center;
            }
            .card-columns-modal-content {
              background: #1a1a1a;
              border-radius: 12px;
              padding: 20px;
              max-width: 400px;
              max-height: 80vh;
              overflow-y: auto;
              box-shadow: 0 8px 32px rgba(0,0,0,0.6);
            }
            .card-columns-modal-content h3 {
              margin: 0 0 16px;
              font-size: 16px;
              color: var(--text);
            }
            .card-columns-list {
              display: flex;
              flex-direction: column;
              gap: 8px;
            }
            .card-columns-list label {
              display: flex;
              align-items: center;
              gap: 8px;
              font-size: 13px;
              color: var(--text);
              cursor: pointer;
            }
            .card-columns-list input[type="checkbox"] {
              width: 14px;
              height: 14px;
              cursor: pointer;
            }
            .card-columns-modal-actions {
              margin-top: 16px;
              display: flex;
              gap: 8px;
              justify-content: flex-end;
            }
            .badge {
              border-radius: 999px;
              border: 1px solid #333;
              padding: 1px 7px;
              font-size: 10px;
              color: var(--muted);
            }
            .del-btn {
              border: none;
              background: transparent;
              color: var(--muted);
              cursor: pointer;
              font-size: 12px;
              padding: 0 4px;
            }
            .del-btn:hover {
              color: var(--danger);
            }
            table {
              width: 100%;
              border-collapse: collapse;
              font-size: 11px;
              table-layout: fixed;
            }
            th, td {
              padding: 2px 4px;
              text-align: left;
              position: relative;
              overflow: hidden;
            }
            th {
              color: var(--muted);
              font-weight: 500;
              border-bottom: 1px solid #222;
              user-select: none;
            }
            td {
              border-bottom: none;
            }
            /* Column resize handle */
            th .resize-handle {
              position: absolute;
              top: 0;
              right: 0;
              width: 4px;
              height: 100%;
              cursor: col-resize;
              background: transparent;
              z-index: 1;
            }
            th .resize-handle:hover {
              background: var(--accent);
              opacity: 0.5;
            }
            th.resizing .resize-handle {
              background: var(--accent);
            }
            .col-num {
              width: 18px;
              text-align: right;
            }
            .col-bpm {
              width: 34px;
              text-align: right;
            }
            .col-key {
              width: 30px;
            }
            .col-pop {
              width: 30px;
              text-align: right;
            }
            .col-dur {
              width: 36px;
            }
            .col-energy {
              width: 30px;
            }
            .col-val {
              width: 30px;
            }
            .col-canonical {
              width: 120px;
              font-size: 10px;
              color: var(--muted);
              font-style: italic;
            }
            .col-danceability, .col-acousticness, .col-speechiness {
              width: 30px;
            }
            .col-genres {
              width: 120px;
              font-size: 10px;
            }
            .col-year {
              width: 40px;
            }
            .col-stars {
              width: 40px;
            }
            .col-notes {
              width: 120px;
            }
            .col-actions {
              width: 90px;
              text-align: right;
              white-space: nowrap;
            }
            .pop-val {
              font-variant-numeric: tabular-nums;
            }
            .track-label {
              white-space: nowrap;
              overflow: hidden;
              text-overflow: ellipsis;
            }
            td[contenteditable="true"] {
              outline: none;
            }
            td[contenteditable="true"]:focus {
              background: #1a1a1a;
            }
            /* Color coding for data sources */
            td[data-source="api"] {
              color: #888;
            }
            td[data-source="manual"] {
              color: #fff;
              background-color: rgba(100, 150, 200, 0.1);
            }
            td[data-source="manual"]:focus {
              background-color: rgba(100, 150, 200, 0.2);
            }
            .track-btn {
              border: none;
              background: transparent;
              color: var(--muted);
              cursor: pointer;
              font-size: 11px;
              padding: 0 2px;
            }
            .track-btn:hover {
              color: var(--accent);
            }
            .stars {
              display: inline-flex;
              gap: 1px;
              cursor: pointer;
            }
            .star {
              font-size: 11px;
              opacity: 0.4;
            }
            .star.on {
              opacity: 1;
              color: #ffcc33;
            }
            .column-toggles {
              display: flex;
              flex-wrap: wrap;
              gap: 10px;
              font-size: 11px;
              color: var(--muted);
              margin-top: 8px;
            }
            .column-toggles label {
              display: inline-flex;
              align-items: center;
              gap: 4px;
              cursor: pointer;
            }
            .column-toggles input[type="checkbox"] {
              width: 12px;
              height: 12px;
            }
            .upload-wrap {
              display: inline-flex;
              align-items: center;
              gap: 6px;
              font-size: 12px;
            }
            .upload-wrap input {
              font-size: 11px;
            }
            .autocomplete-wrapper {
              position: relative;
            }
            .autocomplete-dropdown {
              position: absolute;
              top: 100%;
              left: 0;
              right: 0;
              background: #1a1a1a;
              border: 1px solid #333;
              border-radius: 8px;
              max-height: 200px;
              overflow-y: auto;
              z-index: 1000;
              margin-top: 4px;
              box-shadow: 0 4px 12px rgba(0,0,0,0.5);
            }
            .autocomplete-item {
              padding: 8px 12px;
              cursor: pointer;
              border-bottom: 1px solid #222;
              font-size: 12px;
            }
            .autocomplete-item:hover {
              background: #252525;
            }
            .autocomplete-item:last-child {
              border-bottom: none;
            }
            .autocomplete-item-title {
              font-weight: 600;
              color: var(--text);
            }
            .autocomplete-item-subtitle {
              font-size: 11px;
              color: var(--muted);
              margin-top: 2px;
            }
            .selection-modal {
              position: fixed;
              top: 0;
              left: 0;
              right: 0;
              bottom: 0;
              background: rgba(0,0,0,0.8);
              z-index: 2000;
              display: flex;
              align-items: center;
              justify-content: center;
            }
            .selection-modal-content {
              background: #1a1a1a;
              border-radius: 12px;
              padding: 20px;
              max-width: 600px;
              max-height: 80vh;
              overflow-y: auto;
              box-shadow: 0 8px 32px rgba(0,0,0,0.6);
            }
            .selection-modal-title {
              font-size: 16px;
              font-weight: 600;
              margin-bottom: 12px;
            }
            .selection-list {
              display: flex;
              flex-direction: column;
              gap: 8px;
            }
            .selection-item {
              padding: 12px;
              background: #111;
              border: 1px solid #333;
              border-radius: 8px;
              cursor: pointer;
              transition: all 0.2s;
            }
            .selection-item:hover {
              background: #222;
              border-color: var(--accent);
            }
            .selection-item.selected {
              background: #2a2a2a;
              border-color: var(--accent);
            }
            .selection-item-title {
              font-weight: 600;
              font-size: 14px;
              margin-bottom: 4px;
            }
            .selection-item-details {
              font-size: 12px;
              color: var(--muted);
            }
            .selection-modal-actions {
              display: flex;
              gap: 8px;
              margin-top: 16px;
              justify-content: flex-end;
            }
            .support-bar {
              margin-top: 16px;
              padding-top: 14px;
              border-top: 1px solid #262626;
              display: flex;
              flex-wrap: wrap;
              align-items: center;
              gap: 10px;
              font-size: 12px;
              color: var(--muted);
            }
            .support-bar-text {
              flex: 1 1 220px;
            }
            .support-bar button,
            .support-bar a {
              text-decoration: none;
            }
            .support-btn {
              border-radius: 999px;
              border: none;
              padding: 6px 14px;
              font-size: 12px;
              background: var(--accent);
              color: #000;
              cursor: pointer;
              display: inline-flex;
              align-items: center;
              gap: 6px;
              white-space: nowrap;
            }
            .support-btn:hover {
              filter: brightness(1.05);
            }
            .support-note {
              font-size: 11px;
              color: var(--muted);
              margin-top: 2px;
            }
            @media print {
              body {
                background: #fff;
                color: #000;
              }
              .page {
                max-width: none;
                margin: 0;
                padding: 0;
              }
              .panel:first-of-type {
                display: none;
              }
              .panel {
                box-shadow: none;
                border-radius: 0;
              }
              .status {
                display: none;
              }
              .cards {
                gap: 4px;
              }
              .card {
                border-radius: 0;
                border-color: #000;
              }
              .del-btn,
              .track-btn,
              .col-actions,
              .column-toggles,
              .upload-wrap,
              #downloadBtn {
                display: none;
              }
              .support-bar {
                display: none;
              }
            }
          </style>
        </head>
        <body>
          <div class="page">
            <div class="panel">
              <h1>Vinyl BPM and Key Sheets</h1>
              <div class="subtitle">
                Paste album or track lists. This tool cleans them, finds BPM / keys via ReccoBeats (see https://reccobeats.com/ - feel free to donate to him too!)
                and lays out printable slips for your records.
              </div>
              <div class="support-note">
                You can post discogs.com track lists in tracks mode if an album is not found - and refresh individual tracks or cards. Where artist names are not present for certain tracks, it infers the artist from the context of the card.
              </div>
              <br><br>
              <h2>1. Paste album or track list</h2>
              <div class="autocomplete-wrapper">
                <textarea id="input" placeholder="Example:
Moodymann - Mahogany Brown
A2 Stan Getz, Charlie Byrd - Desafinado 1:59
B1 Quincy Jones - Soul Bossa Nova 2:43"></textarea>
                <div id="autocompleteDropdown" class="autocomplete-dropdown" style="display: none;"></div>
              </div>
              <div class="row">
                <div class="mode-toggle" id="modeToggle">
                  <button class="mode-btn active" data-mode="albums">Albums</button>
                  <button class="mode-btn" data-mode="tracks">Tracks</button>
                </div>
                <button class="btn" id="runBtn">⚙️ Generate sheets</button>
                <button class="btn btn-outline" id="refreshAllBtn" title="Refresh all tracks in all cards (with confirmation)">⟳ Refresh All</button>
                <button class="btn btn-outline" id="clearBtn">✖ Clear cards</button>
                <button class="btn btn-outline" id="printBtn">🖨 Print</button>
                <button class="btn btn-outline" id="downloadBtn">⬇ Download .txt</button>
                <label class="upload-wrap">
                  <span>Import .txt</span>
                  <input type="file" id="uploadTxt" accept=".txt" />
                </label>
              </div>
              <div class="column-toggles" id="columnToggles">
                <label draggable="true" data-col="bpm"><input type="checkbox" data-col="bpm" checked /> BPM</label>
                <label draggable="true" data-col="key"><input type="checkbox" data-col="key" checked /> Key</label>
                <label draggable="true" data-col="pop"><input type="checkbox" data-col="pop" checked /> Pop</label>
                <label draggable="true" data-col="energy"><input type="checkbox" data-col="energy" checked /> Energy</label>
                <label draggable="true" data-col="danceability"><input type="checkbox" data-col="danceability" /> Dance</label>
                <label draggable="true" data-col="val"><input type="checkbox" data-col="val" /> Valence</label>
                <label draggable="true" data-col="acousticness"><input type="checkbox" data-col="acousticness" /> Acoustic</label>
                <label draggable="true" data-col="speechiness"><input type="checkbox" data-col="speechiness" /> Speech</label>
                <label draggable="true" data-col="dur"><input type="checkbox" data-col="dur" checked /> Dur</label>
                <label draggable="true" data-col="genres"><input type="checkbox" data-col="genres" checked /> Genres</label>
                <label draggable="true" data-col="year"><input type="checkbox" data-col="year" /> Year</label>
                <label draggable="true" data-col="canonical"><input type="checkbox" data-col="canonical" /> Database name</label>
                <label draggable="true" data-col="stars"><input type="checkbox" data-col="stars" checked /> Stars</label>
                <label draggable="true" data-col="notes"><input type="checkbox" data-col="notes" checked /> Notes</label>
              </div>
              <div class="row" style="margin-top: 8px;">
                <label style="display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted);">
                  <input type="checkbox" id="fullWidthCards" checked />
                  Full width cards
                </label>
                <label style="display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); margin-left: 12px;">
                  <input type="checkbox" id="showTrackNameAlert" checked />
                  Show track name replacement alert
                </label>
                <label style="display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); margin-left: 12px;">
                  <input type="checkbox" id="singleDigitScale" />
                  Single digit scale (0-9)
                </label>
              </div>
              <div class="status" id="status">
                Mode: Albums. Cards from each run will be added below. You can also import/export .txt.
              </div>
              <div class="support-bar">
                <div class="support-bar-text">
                  This tool is free for DJs and vinyl heads. If you find it useful, please feel free to buy me a coffee, donate to my new vinyl fund or help cover hosting in order to keep it running.
                  <div class="support-note">
                    Payments handled by Stripe.
                  </div>
                </div>
                <a
                  href="https://buy.stripe.com/14AeVdc4L3BDgII8xb87K00"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="support-btn"
                >
                  ☕ Buy me a coffee
                </a>
              </div>
            </div>

            <div class="panel">
              <h2>2. Print slips</h2>
              <div id="results" class="cards"></div>
            </div>
          </div>

          <script>
            const modeToggle = document.getElementById("modeToggle");
            const runBtn = document.getElementById("runBtn");
            const clearBtn = document.getElementById("clearBtn");
            const printBtn = document.getElementById("printBtn");
            const downloadBtn = document.getElementById("downloadBtn");
            const uploadTxt = document.getElementById("uploadTxt");
            const inputEl = document.getElementById("input");
            const status = document.getElementById("status");
            const results = document.getElementById("results");
            const columnCheckboxes = document.querySelectorAll(".column-toggles input[type='checkbox']");
            const autocompleteDropdown = document.getElementById("autocompleteDropdown");

            let currentMode = "albums";
            let autocompleteTimeout = null;
            let currentAutocompleteResults = [];

            const columnConfig = {
              bpm: true,
              key: true,
              pop: true,
              dur: true,
              energy: true,
              val: false,
              danceability: false,
              acousticness: false,
              speechiness: false,
              genres: true,
              year: true,
              canonical: false, // Off by default
              stars: true,
              notes: true,
            };
            
            let showTrackNameAlert = true; // Track name replacement alert enabled by default
            let singleDigitScale = true; // Single digit scale disabled by default
            
            // Column order (excluding actions which is always first)
            let columnOrder = ["num", "bpm", "key", "pop", "dur", "energy", "val", "track", "stars", "notes"];
            
            // Column widths storage
            let columnWidths = {};
            
            // Initialize column resize functionality
            function initColumnResize() {
              // Add resize handles to all table headers
              document.querySelectorAll("table thead th").forEach(th => {
                // Remove existing listeners to avoid duplicates
                const existingHandle = th.querySelector(".resize-handle");
                if (existingHandle) {
                  const newHandle = existingHandle.cloneNode(true);
                  existingHandle.remove();
                  th.appendChild(newHandle);
                } else {
                  const handle = document.createElement("div");
                  handle.className = "resize-handle";
                  th.appendChild(handle);
                }
              });
              
              // Remove old listeners and add new ones
              document.querySelectorAll("table thead th .resize-handle").forEach(handle => {
                // Clone to remove old listeners
                const newHandle = handle.cloneNode(true);
                handle.parentNode.replaceChild(newHandle, handle);
                newHandle.addEventListener("mousedown", startResize);
              });
            }
            
            let resizing = false;
            let resizeColumn = null;
            let startX = 0;
            let startWidth = 0;
            
            function startResize(e) {
              e.preventDefault();
              e.stopPropagation();
              resizing = true;
              resizeColumn = e.target.parentElement;
              startX = e.pageX;
              startWidth = resizeColumn.offsetWidth;
              
              resizeColumn.classList.add("resizing");
              document.addEventListener("mousemove", doResize);
              document.addEventListener("mouseup", stopResize);
            }
            
            function doResize(e) {
              if (!resizing || !resizeColumn) return;
              
              const diff = e.pageX - startX;
              const newWidth = Math.max(20, startWidth + diff); // Minimum width 20px
              
              // Get column class
              const colClass = Array.from(resizeColumn.classList).find(cls => cls.startsWith("col-"));
              if (colClass) {
                // Update width for all cells with this class
                document.querySelectorAll(`.${colClass}`).forEach(cell => {
                  cell.style.width = newWidth + "px";
                });
                // Store width
                columnWidths[colClass] = newWidth;
              }
            }
            
            function stopResize() {
              if (resizeColumn) {
                resizeColumn.classList.remove("resizing");
              }
              resizing = false;
              resizeColumn = null;
              document.removeEventListener("mousemove", doResize);
              document.removeEventListener("mouseup", stopResize);
            }
            
            // Apply stored column widths
            function applyColumnWidths() {
              Object.keys(columnWidths).forEach(colClass => {
                const width = columnWidths[colClass];
                document.querySelectorAll(`.${colClass}`).forEach(cell => {
                  cell.style.width = width + "px";
                });
              });
            }

            modeToggle.addEventListener("click", (e) => {
              const btn = e.target.closest(".mode-btn");
              if (!btn) return;
              const mode = btn.getAttribute("data-mode");
              currentMode = mode;
              document.querySelectorAll(".mode-btn").forEach(b => b.classList.remove("active"));
              btn.classList.add("active");
              updateStatus();
            });

            clearBtn.addEventListener("click", () => {
              results.innerHTML = "";
              updateStatus("Cleared existing cards. Paste albums or tracks and generate again.");
            });

            printBtn.addEventListener("click", () => {
              window.print();
            });

            runBtn.addEventListener("click", () => {
              runProcess();
            });

            downloadBtn.addEventListener("click", () => {
              downloadTxt();
            });

            const refreshAllBtn = document.getElementById("refreshAllBtn");
            if (refreshAllBtn) {
              refreshAllBtn.addEventListener("click", () => {
                refreshAllTracksGlobally();
              });
            }

            uploadTxt.addEventListener("change", (e) => {
              const file = e.target.files && e.target.files[0];
              if (!file) return;
              const reader = new FileReader();
              reader.onload = (ev) => {
                const text = ev.target.result || "";
                importTxt(text);
              };
              reader.readAsText(file, "utf-8");
              uploadTxt.value = "";
            });

            columnCheckboxes.forEach(cb => {
              const col = cb.dataset.col;
              cb.checked = columnConfig[col];
              cb.addEventListener("change", () => {
                columnConfig[col] = cb.checked;
                syncColumnVisibility();
              });
            });

            // Full width toggle
            const fullWidthCheckbox = document.getElementById("fullWidthCards");
            if (fullWidthCheckbox) {
              // Set default to checked
              if (fullWidthCheckbox.checked) {
                results.classList.add("full-width");
              }
              fullWidthCheckbox.addEventListener("change", () => {
                if (fullWidthCheckbox.checked) {
                  results.classList.add("full-width");
                } else {
                  results.classList.remove("full-width");
                }
              });
            }
            
            // Track name alert toggle
            const trackNameAlertCheckbox = document.getElementById("showTrackNameAlert");
            if (trackNameAlertCheckbox) {
              trackNameAlertCheckbox.addEventListener("change", () => {
                showTrackNameAlert = trackNameAlertCheckbox.checked;
              });
            }
            
            // Single digit scale toggle
            const singleDigitScaleCheckbox = document.getElementById("singleDigitScale");
            if (singleDigitScaleCheckbox) {
              singleDigitScaleCheckbox.addEventListener("change", () => {
                singleDigitScale = singleDigitScaleCheckbox.checked;
                // Update all visible percentage values
                document.querySelectorAll(".col-energy, .col-val, .col-danceability, .col-acousticness, .col-speechiness").forEach(cell => {
                  const text = cell.textContent.trim();
                  if (text && text !== "" && !isNaN(parseFloat(text))) {
                    const currentVal = parseFloat(text);
                    // Determine current scale: if > 10, it's 0-100 scale; otherwise 0-9 scale
                    let normalizedVal;
                    if (currentVal > 10) {
                      // Currently 0-100 scale, normalize to 0-1
                      normalizedVal = currentVal / 100;
                    } else {
                      // Currently 0-9 scale, normalize to 0-1
                      normalizedVal = currentVal / 10;
                    }
                    // Convert to target scale
                    if (singleDigitScale) {
                      cell.textContent = Math.round(normalizedVal * 10);
                    } else {
                      cell.textContent = Math.round(normalizedVal * 100);
                    }
                  }
                });
              });
            }

            // Column toggle reordering
            const columnToggles = document.getElementById("columnToggles");
            let draggedToggle = null;
            
            // Map column names to their display order (excluding actions, num, track which have fixed positions)
            const columnNameMap = {
              "bpm": "bpm",
              "key": "key",
              "pop": "pop",
              "dur": "dur",
              "energy": "energy",
              "val": "val",
              "danceability": "danceability",
              "acousticness": "acousticness",
              "speechiness": "speechiness",
              "genres": "genres",
              "year": "year",
              "canonical": "canonical",
              "stars": "stars",
              "notes": "notes"
            };
            
            function getColumnOrder() {
              const labels = Array.from(columnToggles.querySelectorAll("label"));
              const order = ["actions", "num"]; // Actions and num are always first
              labels.forEach(label => {
                const col = label.dataset.col;
                if (col && columnNameMap[col]) {
                  order.push(col);
                }
              });
              order.push("track", "stars", "notes"); // Track, stars, notes are always at the end (canonical can be before track)
              return order;
            }
            
            function reorderTableColumns() {
              const newOrder = getColumnOrder();
              columnOrder = newOrder;
              
              // Column class mapping
              const colClassMap = {
                "actions": "col-actions",
                "num": "col-num",
                "bpm": "col-bpm",
                "key": "col-key",
                "pop": "col-pop",
                "dur": "col-dur",
                "energy": "col-energy",
                "val": "col-val",
                "danceability": "col-danceability",
                "acousticness": "col-acousticness",
                "speechiness": "col-speechiness",
                "genres": "col-genres",
                "year": "col-year",
                "canonical": "col-canonical",
                "track": null, // special - has track-label
                "stars": "col-stars",
                "notes": "col-notes"
              };
              
              // Update all tables
              document.querySelectorAll("table").forEach(table => {
                const thead = table.querySelector("thead tr");
                const tbody = table.querySelector("tbody");
                if (!thead || !tbody) return;
                
                // Get all header cells
                const headerCells = Array.from(thead.querySelectorAll("th"));
                const headerMap = {};
                headerCells.forEach(cell => {
                  let colName = null;
                  if (cell.classList.contains("col-actions")) colName = "actions";
                  else if (cell.classList.contains("col-num")) colName = "num";
                  else if (cell.querySelector(".track-label") || (!cell.className && cell.textContent.trim() === "Track")) colName = "track";
                  else if (cell.classList.contains("col-stars")) colName = "stars";
                  else if (cell.classList.contains("col-notes")) colName = "notes";
                  else {
                    for (const [name, className] of Object.entries(colClassMap)) {
                      if (className && cell.classList.contains(className)) {
                        colName = name;
                        break;
                      }
                    }
                  }
                  if (colName) headerMap[colName] = cell;
                });
                
                // Reorder headers
                thead.innerHTML = "";
                newOrder.forEach(colName => {
                  if (headerMap[colName]) {
                    thead.appendChild(headerMap[colName]);
                  }
                });
                
                // Reorder all body rows
                const rows = Array.from(tbody.querySelectorAll("tr"));
                rows.forEach(row => {
                  const cells = Array.from(row.querySelectorAll("td"));
                  const cellMap = {};
                  cells.forEach(cell => {
                    let colName = null;
                    if (cell.classList.contains("col-actions")) colName = "actions";
                    else if (cell.classList.contains("col-num")) colName = "num";
                    else if (cell.querySelector(".track-label")) colName = "track";
                    else if (cell.classList.contains("col-stars")) colName = "stars";
                    else if (cell.classList.contains("col-notes")) colName = "notes";
                    else {
                      for (const [name, className] of Object.entries(colClassMap)) {
                        if (className && cell.classList.contains(className)) {
                          colName = name;
                          break;
                        }
                      }
                    }
                    if (colName) cellMap[colName] = cell;
                  });
                  
                  // Reorder cells
                  row.innerHTML = "";
                  newOrder.forEach(colName => {
                    if (cellMap[colName]) {
                      row.appendChild(cellMap[colName]);
                    }
                  });
                });
              });
            }
            
            if (columnToggles) {
              columnToggles.addEventListener("dragstart", (e) => {
                const label = e.target.closest("label");
                if (label) {
                  draggedToggle = label;
                  label.classList.add("dragging");
                  e.dataTransfer.effectAllowed = "move";
                }
              });

              columnToggles.addEventListener("dragend", (e) => {
                if (draggedToggle) {
                  draggedToggle.classList.remove("dragging");
                  draggedToggle = null;
                  // Reorder table columns after drag ends
                  reorderTableColumns();
                }
              });

              columnToggles.addEventListener("dragover", (e) => {
                e.preventDefault();
                const label = e.target.closest("label");
                if (label && draggedToggle && label !== draggedToggle) {
                  const allLabels = Array.from(columnToggles.querySelectorAll("label"));
                  const draggedIndex = allLabels.indexOf(draggedToggle);
                  const targetIndex = allLabels.indexOf(label);
                  
                  if (draggedIndex < targetIndex) {
                    columnToggles.insertBefore(draggedToggle, label.nextSibling);
                  } else {
                    columnToggles.insertBefore(draggedToggle, label);
                  }
                }
              });
            }

            // Autocomplete for main input (albums and tracks mode)
            inputEl.addEventListener("input", (e) => {
              clearTimeout(autocompleteTimeout);
              const value = e.target.value;
              const lines = value.split("\\n");
              const currentLine = lines[lines.length - 1] || "";
              
              if (currentLine.trim().length < 2) {
                autocompleteDropdown.style.display = "none";
                return;
              }
              
              autocompleteTimeout = setTimeout(async () => {
                try {
                  if (currentMode === "albums") {
                    // Album autocomplete
                    let artist = "";
                    let album = currentLine.trim();
                    if (currentLine.includes(" - ")) {
                      const parts = currentLine.split(" - ", 2);
                      artist = parts[0].trim();
                      album = parts[1].trim();
                    }
                    
                    const res = await fetch("/api/search_albums", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ artist, album }),
                    });
                    const data = await res.json();
                    if (data.results && data.results.length > 0) {
                      showAutocomplete(data.results);
                    } else {
                      autocompleteDropdown.style.display = "none";
                    }
                  } else if (currentMode === "tracks") {
                    // Track autocomplete
                    let artist = "";
                    let title = currentLine.trim();
                    if (currentLine.includes(" - ")) {
                      const parts = currentLine.split(" - ", 2);
                      artist = parts[0].trim();
                      title = parts[1].trim();
                    }
                    
                    const res = await fetch("/api/search_tracks", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ artist, title }),
                    });
                    const data = await res.json();
                    if (data.results && data.results.length > 0) {
                      showTrackAutocompleteForInput(data.results, currentLine, lines.length - 1);
                    } else {
                      autocompleteDropdown.style.display = "none";
                    }
                  }
                } catch (err) {
                  console.error("Autocomplete error:", err);
                }
              }, 300);
            });

            // Hide autocomplete when clicking outside
            document.addEventListener("click", (e) => {
              if (!e.target.closest(".autocomplete-wrapper")) {
                autocompleteDropdown.style.display = "none";
              }
            });

            function showAutocomplete(results) {
              currentAutocompleteResults = results;
              autocompleteDropdown.innerHTML = "";
              autocompleteDropdown.style.position = "";
              autocompleteDropdown.style.top = "";
              autocompleteDropdown.style.left = "";
              autocompleteDropdown.style.width = "";
              results.slice(0, 5).forEach((result, idx) => {
                const item = document.createElement("div");
                item.className = "autocomplete-item";
                item.innerHTML = `
                  <div class="autocomplete-item-title">${escapeHtml(result.name)}</div>
                  <div class="autocomplete-item-subtitle">${escapeHtml(result.artists)}${result.year ? " • " + result.year : ""}</div>
                `;
                item.addEventListener("click", () => {
                  const lines = inputEl.value.split("\\n");
                  const currentLineIdx = lines.length - 1;
                  const currentLine = lines[currentLineIdx] || "";
                  // Replace current line with selected album
                  lines[currentLineIdx] = `${result.artists} - ${result.name}`;
                  inputEl.value = lines.join("\\n");
                  autocompleteDropdown.style.display = "none";
                });
                autocompleteDropdown.appendChild(item);
              });
              autocompleteDropdown.style.display = "block";
            }

            function showTrackAutocompleteForInput(results, currentLine, lineIndex) {
              autocompleteDropdown.innerHTML = "";
              autocompleteDropdown.style.position = "";
              autocompleteDropdown.style.top = "";
              autocompleteDropdown.style.left = "";
              autocompleteDropdown.style.width = "";
              results.slice(0, 5).forEach((result) => {
                const item = document.createElement("div");
                item.className = "autocomplete-item";
                item.innerHTML = `
                  <div class="autocomplete-item-title">${escapeHtml(result.name)}</div>
                  <div class="autocomplete-item-subtitle">${escapeHtml(result.artists)}</div>
                `;
                item.addEventListener("click", () => {
                  const lines = inputEl.value.split("\\n");
                  // Replace current line with selected track
                  lines[lineIndex] = `${result.artists} - ${result.name}`;
                  inputEl.value = lines.join("\\n");
                  autocompleteDropdown.style.display = "none";
                });
                autocompleteDropdown.appendChild(item);
              });
              autocompleteDropdown.style.display = "block";
            }

            function showSelectionModal(title, items, onSelect) {
              return new Promise((resolve) => {
                const modal = document.createElement("div");
                modal.className = "selection-modal";
                modal.innerHTML = `
                  <div class="selection-modal-content">
                    <div class="selection-modal-title">${escapeHtml(title)}</div>
                    <div class="selection-list" id="selectionList"></div>
                    <div class="selection-modal-actions">
                      <button class="btn btn-outline" id="cancelSelection">Cancel</button>
                      <button class="btn" id="confirmSelection">Select</button>
                    </div>
                  </div>
                `;
                document.body.appendChild(modal);
                
                const list = modal.querySelector("#selectionList");
                let selectedItem = items[0] || null;
                
                items.forEach((item, idx) => {
                  const itemEl = document.createElement("div");
                  itemEl.className = "selection-item" + (idx === 0 ? " selected" : "");
                  
                const itemTitle = item.name || item.title || "";
                const subtitle = item.artists || item.subtitle || "";
                let details = subtitle;
                
                // Add additional info for tracks
                const infoParts = [];
                if (item.bpm) infoParts.push(`${item.bpm} BPM`);
                if (item.camelot) infoParts.push(`Key: ${item.camelot}`);
                if (item.popularity) infoParts.push(`Pop: ${item.popularity}`);
                if (item.duration_str) infoParts.push(item.duration_str);
                if (item.year) infoParts.push(item.year);
                
                if (infoParts.length > 0) {
                  details = `${subtitle} • ${infoParts.join(" • ")}`;
                } else if (item.year) {
                  details = `${subtitle} • ${item.year}`;
                }
                
                itemEl.innerHTML = `
                  <div class="selection-item-title">${escapeHtml(itemTitle)}</div>
                  <div class="selection-item-details">${escapeHtml(details)}</div>
                `;
                  
                  itemEl.addEventListener("click", () => {
                    list.querySelectorAll(".selection-item").forEach(el => el.classList.remove("selected"));
                    itemEl.classList.add("selected");
                    selectedItem = item;
                  });
                  
                  list.appendChild(itemEl);
                });
                
                const closeModal = () => {
                  document.body.removeChild(modal);
                };
                
                const confirmSelection = () => {
                  if (selectedItem && onSelect) {
                    onSelect(selectedItem);
                  }
                  closeModal();
                  resolve(selectedItem);
                };
                
                modal.querySelector("#cancelSelection").addEventListener("click", () => {
                  closeModal();
                  resolve(null);
                });
                
                modal.querySelector("#confirmSelection").addEventListener("click", confirmSelection);
                
                modal.addEventListener("click", (e) => {
                  if (e.target === modal) {
                    closeModal();
                    resolve(null);
                  }
                });
                
                // Double-click to select
                list.addEventListener("dblclick", confirmSelection);
              });
            }

            // Track name autocomplete when editing in cards
            let trackAutocompleteTimeout = null;
            results.addEventListener("input", (e) => {
              // Check if the event is on a td containing a track-label
              const td = e.target.closest("td");
              if (!td) return;
              
              // Don't trigger if it's the main input
              if (e.target === inputEl || e.target.closest("#input")) return;
              
              // Mark manually edited cells
              if (td.hasAttribute("contenteditable") && td.getAttribute("contenteditable") === "true") {
                td.setAttribute("data-source", "manual");
              }
              
              const trackLabel = td.querySelector(".track-label[data-autocomplete='track']");
              if (trackLabel) {
                handleTrackAutocomplete(trackLabel, td);
              }
            });
            
            // Also listen for keyup on contenteditable elements
            results.addEventListener("keyup", (e) => {
              // Check if the event is on a td containing a track-label
              const td = e.target.closest("td");
              if (!td) return;
              
              // Don't trigger if it's the main input
              if (e.target === inputEl || e.target.closest("#input")) return;
              
              // Mark manually edited cells
              if (td.hasAttribute("contenteditable") && td.getAttribute("contenteditable") === "true") {
                td.setAttribute("data-source", "manual");
              }
              
              const trackLabel = td.querySelector(".track-label[data-autocomplete='track']");
              if (trackLabel) {
                handleTrackAutocomplete(trackLabel, td);
              }
            });
            
            // Track blur events to mark manual edits
            results.addEventListener("blur", (e) => {
              const td = e.target.closest("td");
              if (td && td.hasAttribute("contenteditable") && td.getAttribute("contenteditable") === "true") {
                if (td.textContent.trim()) {
                  td.setAttribute("data-source", "manual");
                }
              }
            }, true);
            
            function handleTrackAutocomplete(trackLabel, td) {
              clearTimeout(trackAutocompleteTimeout);
              // Get text from the td (which is contenteditable) or the trackLabel
              const text = (td && td.textContent) || trackLabel.textContent || "";
              
              if (text.trim().length < 2) {
                autocompleteDropdown.style.display = "none";
                return;
              }
              
              trackAutocompleteTimeout = setTimeout(async () => {
                try {
                  let artist = "";
                  let title = text.trim();
                  
                  if (text.includes(" - ")) {
                    const parts = text.split(" - ", 2);
                    artist = parts[0].trim();
                    title = parts[1].trim();
                  }
                  
                  const res = await fetch("/api/search_tracks", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ artist, title }),
                  });
                  const data = await res.json();
                  if (data.results && data.results.length > 0) {
                    // Position dropdown relative to the td (which is the contenteditable element)
                    const rect = (td || trackLabel).getBoundingClientRect();
                    autocompleteDropdown.style.position = "fixed";
                    autocompleteDropdown.style.top = (rect.bottom + 4) + "px";
                    autocompleteDropdown.style.left = rect.left + "px";
                    autocompleteDropdown.style.width = Math.max(rect.width, 300) + "px";
                    showTrackAutocomplete(data.results, trackLabel, td);
                  } else {
                    autocompleteDropdown.style.display = "none";
                  }
                } catch (err) {
                  console.error("Track autocomplete error:", err);
                }
              }, 300);
            }

            function showTrackAutocomplete(results, trackLabel, td) {
              autocompleteDropdown.innerHTML = "";
              results.slice(0, 5).forEach((result) => {
                const item = document.createElement("div");
                item.className = "autocomplete-item";
                item.innerHTML = `
                  <div class="autocomplete-item-title">${escapeHtml(result.name)}</div>
                  <div class="autocomplete-item-subtitle">${escapeHtml(result.artists)}</div>
                `;
                item.addEventListener("click", () => {
                  const newText = `${result.artists} - ${result.name}`;
                  if (td) {
                    td.textContent = newText;
                  } else {
                    trackLabel.textContent = newText;
                  }
                  autocompleteDropdown.style.display = "none";
                  // Trigger refresh to update BPM/Key/Pop
                  const tr = (td || trackLabel).closest("tr");
                  if (tr) {
                    const refreshBtn = tr.querySelector("[data-action='refresh']");
                    if (refreshBtn) {
                      refreshBtn.click();
                    }
                  }
                });
                autocompleteDropdown.appendChild(item);
              });
              autocompleteDropdown.style.display = "block";
            }

            // Drag and drop for tracks within cards
            let draggedRow = null;
            results.addEventListener("dragstart", (e) => {
              const tr = e.target.closest("tr");
              if (tr && tr.parentElement.tagName === "TBODY") {
                draggedRow = tr;
                tr.classList.add("dragging");
                e.dataTransfer.effectAllowed = "move";
              }
            });

            results.addEventListener("dragend", (e) => {
              if (draggedRow) {
                draggedRow.classList.remove("dragging");
                draggedRow = null;
              }
              document.querySelectorAll("tr.drag-over").forEach(el => el.classList.remove("drag-over"));
            });

            results.addEventListener("dragover", (e) => {
              const tr = e.target.closest("tr");
              if (tr && tr.parentElement.tagName === "TBODY" && draggedRow && tr !== draggedRow) {
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                tr.classList.add("drag-over");
              }
            });

            results.addEventListener("dragleave", (e) => {
              const tr = e.target.closest("tr");
              if (tr) {
                tr.classList.remove("drag-over");
              }
            });

            results.addEventListener("drop", (e) => {
              e.preventDefault();
              const tr = e.target.closest("tr");
              if (tr && draggedRow && tr !== draggedRow && tr.parentElement === draggedRow.parentElement) {
                const tbody = tr.parentElement;
                if (tbody) {
                  const allRows = Array.from(tbody.querySelectorAll("tr"));
                  const draggedIndex = allRows.indexOf(draggedRow);
                  const targetIndex = allRows.indexOf(tr);
                  
                  if (draggedIndex < targetIndex) {
                    tbody.insertBefore(draggedRow, tr.nextSibling);
                  } else {
                    tbody.insertBefore(draggedRow, tr);
                  }
                  
                  // Update track numbers
                  allRows.forEach((row, idx) => {
                    const numCell = row.querySelector(".col-num");
                    if (numCell) {
                      numCell.textContent = idx + 1;
                    }
                  });
                }
              }
              document.querySelectorAll("tr.drag-over").forEach(el => el.classList.remove("drag-over"));
            });

            // Drag and drop for cards
            let draggedCard = null;
            results.addEventListener("dragstart", (e) => {
              const card = e.target.closest(".card");
              if (card && !e.target.closest("table")) {
                draggedCard = card;
                card.classList.add("dragging");
                e.dataTransfer.effectAllowed = "move";
              }
            }, true);

            results.addEventListener("dragend", (e) => {
              if (draggedCard) {
                draggedCard.classList.remove("dragging");
                draggedCard = null;
              }
              document.querySelectorAll(".card.drag-over").forEach(el => el.classList.remove("drag-over"));
            }, true);

            results.addEventListener("dragover", (e) => {
              const card = e.target.closest(".card");
              if (card && draggedCard && card !== draggedCard && !e.target.closest("table")) {
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                card.classList.add("drag-over");
              }
            }, true);

            results.addEventListener("dragleave", (e) => {
              const card = e.target.closest(".card");
              if (card) {
                card.classList.remove("drag-over");
              }
            }, true);

            results.addEventListener("drop", (e) => {
              e.preventDefault();
              const card = e.target.closest(".card");
              if (card && draggedCard && card !== draggedCard && !e.target.closest("table")) {
                const allCards = Array.from(results.querySelectorAll(".card"));
                const draggedIndex = allCards.indexOf(draggedCard);
                const targetIndex = allCards.indexOf(card);
                
                if (draggedIndex < targetIndex) {
                  results.insertBefore(draggedCard, card.nextSibling);
                } else {
                  results.insertBefore(draggedCard, card);
                }
              }
              document.querySelectorAll(".card.drag-over").forEach(el => el.classList.remove("drag-over"));
            }, true);

            // Card- and row-level click handling
            results.addEventListener("click", (e) => {
              const cardAdd = e.target.closest("[data-card-action='add-track']");
              if (cardAdd) {
                const card = cardAdd.closest(".card");
                if (card) {
                  const tbody = card.querySelector("tbody");
                  if (tbody) addEmptyRow(tbody);
                }
                return;
              }

              const cardStrip = e.target.closest("[data-card-action='strip-name']");
              if (cardStrip) {
                const card = cardStrip.closest(".card");
                if (card) {
                  stripNameFromCard(card);
                }
                return;
              }

              const cardRefreshAll = e.target.closest("[data-card-action='refresh-all']");
              if (cardRefreshAll) {
                const card = cardRefreshAll.closest(".card");
                if (card) {
                  refreshAllTracksInCard(card);
                }
                return;
              }

              const cardColumns = e.target.closest("[data-card-action='columns']");
              if (cardColumns) {
                const card = cardColumns.closest(".card");
                if (card) {
                  showCardColumnModal(card);
                }
                return;
              }

              const cardDel = e.target.closest(".del-btn");
              if (cardDel) {
                const card = cardDel.closest(".card");
                if (card) {
                  card.remove();
                  updateStatus("Card removed.");
                }
                return;
              }

              const starEl = e.target.closest(".star");
              if (starEl) {
                const idx = parseInt(starEl.dataset.idx || "0", 10);
                const starsWrap = starEl.closest(".stars");
                if (starsWrap) {
                  setStars(starsWrap, idx + 1);
                }
                return;
              }

              const trackBtn = e.target.closest(".track-btn");
              if (!trackBtn) return;
              const action = trackBtn.getAttribute("data-action");
              const tr = trackBtn.closest("tr");
              if (!tr) return;

              if (action === "delete") {
                tr.remove();
                updateStatus("Track removed.");
              } else if (action === "refresh") {
                refreshRow(tr);
              } else if (action === "clear-api") {
                clearApiData(tr);
              }
            });

            function updateStatus(extra) {
              const modeLabel = currentMode === "albums" ? "Albums" : "Tracks";
              let base = `Mode: ${modeLabel}. Cards from each run will be added below.`;
              if (extra) base += " " + extra;
              status.textContent = base;
            }

            async function runProcess() {
              const raw = inputEl.value || "";
              if (!raw.trim()) {
                status.textContent = "Please paste some album or track lines first.";
                return;
              }
              runBtn.disabled = true;
              status.textContent = "Talking to Spotify for track lists and ReccoBeats for BPM and keys...";

              try {
                // For albums mode, check if we need to show selection modals
                const selectedAlbumIds = {};
                if (currentMode === "albums") {
                  const cleanedLines = raw.split("\\n").map(l => l.trim()).filter(l => l);
                  
                  // Check each line for multiple results
                  for (let idx = 0; idx < cleanedLines.length; idx++) {
                    const line = cleanedLines[idx];
                    let artist = "";
                    let album = line;
                    
                    // Parse artist - album from line
                    if (line.includes(" - ")) {
                      const parts = line.split(" - ", 2);
                      artist = parts[0].trim();
                      album = parts[1].trim();
                    }
                    
                    const searchRes = await fetch("/api/search_albums", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ artist, album }),
                    });
                    const searchData = await searchRes.json();
                    
                    if (searchData.results && searchData.results.length > 1) {
                      // Show selection modal - only show if multiple results
                      status.textContent = `Select album for: ${line}`;
                      const selected = await showSelectionModal(
                        `Multiple albums found for: ${escapeHtml(line)}`,
                        searchData.results,
                        (item) => {} // onSelect callback
                      );
                      if (selected) {
                        selectedAlbumIds[idx] = selected.id;
                      } else {
                        // User cancelled, skip this album
                        continue;
                      }
                    } else if (searchData.results && searchData.results.length === 1) {
                      selectedAlbumIds[idx] = searchData.results[0].id;
                    }
                  }
                  
                  status.textContent = "Processing selected albums...";
                }
                
                const res = await fetch("/api/process", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ 
                    mode: currentMode, 
                    raw,
                    selected_album_ids: currentMode === "albums" ? selectedAlbumIds : {}
                  }),
                });
                const data = await res.json();
                if (!res.ok) {
                  status.textContent = "Backend error: " + (data.error || res.statusText);
                  runBtn.disabled = false;
                  return;
                }
                renderResults(data);
                reorderTableColumns();
                // Initialize column resize after rendering
                setTimeout(() => {
                  initColumnResize();
                  applyColumnWidths();
                }, 50);
                updateStatus("Done. You can edit inline, delete tracks, reorder, add new tracks, re-query tracks, print, or download as text.");
              } catch (err) {
                console.error(err);
                status.textContent = "Network error talking to backend.";
              } finally {
                runBtn.disabled = false;
              }
            }

            function escapeHtml(str) {
              return (str || "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
            }

            function escapeRegExp(string) {
              return string.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
            }

            function stripNameFromCard(card) {
              const defaultName = card.dataset.defaultStripName || "";
              const name = window.prompt("Remove this name from all track labels in this card:", defaultName);
              if (!name || !name.trim()) return;
              const target = name.trim();

              const rows = card.querySelectorAll("tbody tr");
              rows.forEach(row => {
                const labelCell = row.querySelector("td .track-label")?.closest("td");
                if (!labelCell) return;
                const labelDiv = labelCell.querySelector(".track-label") || labelCell;
                let text = labelDiv.textContent || "";
                if (!text) return;

                const re = new RegExp(escapeRegExp(target), "gi");
                text = text.replace(re, "");

                text = text.replace(/\s+-\s+/g, " ");
                text = text.replace(/\s{2,}/g, " ").trim();

                labelDiv.textContent = text;
              });

              status.textContent = `Removed "${target}" from track labels in this card.`;
            }

            function makeDeleteButton(card) {
              const btn = document.createElement("button");
              btn.className = "del-btn";
              btn.type = "button";
              btn.textContent = "✖";
              btn.title = "Remove this card";
              return btn;
            }

            function makeStarsCell(value) {
              const val = parseInt(value || "0", 10);
              let html = '<div class="stars" data-value="' + (isNaN(val) ? 0 : val) + '">';
              for (let i = 0; i < 5; i++) {
                const on = i < val;
                html += '<span class="star ' + (on ? "on" : "") + '" data-idx="' + i + '">★</span>';
              }
              html += "</div>";
              return html;
            }

            function setStars(starsWrap, value) {
              const v = Math.max(0, Math.min(5, value || 0));
              starsWrap.dataset.value = String(v);
              const starEls = starsWrap.querySelectorAll(".star");
              starEls.forEach((s, i) => {
                if (i < v) s.classList.add("on");
                else s.classList.remove("on");
              });
            }

            function addEmptyRow(tbody) {
              const index = tbody.querySelectorAll("tr").length + 1;
              const tr = document.createElement("tr");
              tr.draggable = true;
              tr.dataset.spotifyId = "";
              tr.innerHTML =
                "<td class='col-num' contenteditable='true'>" + index + "</td>" +
                "<td class='col-bpm' contenteditable='true'></td>" +
                "<td class='col-key' contenteditable='true'></td>" +
                "<td class='col-pop pop-val' contenteditable='true'></td>" +
                "<td class='col-dur' contenteditable='true'></td>" +
                "<td class='col-energy' contenteditable='true'></td>" +
                "<td class='col-val' contenteditable='true'></td>" +
                "<td class='col-danceability' contenteditable='true'></td>" +
                "<td class='col-acousticness' contenteditable='true'></td>" +
                "<td class='col-speechiness' contenteditable='true'></td>" +
                "<td class='col-genres' contenteditable='true'></td>" +
                "<td class='col-year' contenteditable='true'></td>" +
                "<td class='col-canonical' contenteditable='false' title='Database name from Spotify'></td>" +
                "<td contenteditable='true'><div class='track-label' data-autocomplete='track'></div></td>" +
                "<td class='col-stars'>" + makeStarsCell("") + "</td>" +
                "<td class='col-notes' contenteditable='true'></td>" +
                "<td class='col-actions'>" +
                "<button type='button' class='track-btn' data-action='refresh' title='Re-query BPM/Key/Pop'>⟳</button>" +
                "<button type='button' class='track-btn' data-action='clear-api' title='Clear API-fetched data'>⌫</button>" +
                "<button type='button' class='track-btn' data-action='up' title='Move up'>↑</button>" +
                "<button type='button' class='track-btn' data-action='down' title='Move down'>↓</button>" +
                "<button type='button' class='track-btn' data-action='delete' title='Remove track'>✖</button>" +
                "</td>";
              tbody.appendChild(tr);
              syncColumnVisibility();
            }

            function renderResults(data) {
              if (data.mode === "albums") {
                (data.items || []).forEach(album => {
                  const card = document.createElement("div");
                  card.className = "card";
                  card.dataset.defaultStripName = album.single_artist || album.artists || "";

                  const header = document.createElement("div");
                  header.className = "card-header";

                  const left = document.createElement("div");
                  left.className = "card-header-main";
                  const title = document.createElement("div");
                  title.className = "card-title";
                  title.textContent = album.title || "";
                  title.setAttribute("contenteditable", "true");
                  const subtitle = document.createElement("div");
                  subtitle.className = "card-subtitle";
                  subtitle.textContent = album.subtitle || "";
                  subtitle.setAttribute("contenteditable", "true");
                  left.appendChild(title);
                  left.appendChild(subtitle);

                  const right = document.createElement("div");
                  right.className = "card-actions";
                  const badge = document.createElement("div");
                  badge.className = "badge";
                  badge.textContent = (album.year || "") + " • " + (album.total_tracks || 0) + " tracks";

                  const addBtn = document.createElement("button");
                  addBtn.type = "button";
                  addBtn.className = "track-btn";
                  addBtn.setAttribute("data-card-action", "add-track");
                  addBtn.title = "Add new empty track row";
                  addBtn.textContent = "+ Row";

                  const stripBtn = document.createElement("button");
                  stripBtn.type = "button";
                  stripBtn.className = "track-btn";
                  stripBtn.setAttribute("data-card-action", "strip-name");
                  stripBtn.title = "Remove a name (for example primary artist) from all track labels in this card";
                  stripBtn.textContent = "🧹 Name";

                  const refreshAllBtn = document.createElement("button");
                  refreshAllBtn.type = "button";
                  refreshAllBtn.className = "track-btn";
                  refreshAllBtn.setAttribute("data-card-action", "refresh-all");
                  refreshAllBtn.title = "Refresh all tracks in this card";
                  refreshAllBtn.textContent = "⟳ All";

                  const columnsBtn = document.createElement("button");
                  columnsBtn.type = "button";
                  columnsBtn.className = "track-btn";
                  columnsBtn.setAttribute("data-card-action", "columns");
                  columnsBtn.title = "Toggle column visibility for this card";
                  columnsBtn.textContent = "📊";

                  const delBtn = makeDeleteButton(card);
                  right.appendChild(badge);
                  right.appendChild(addBtn);
                  right.appendChild(stripBtn);
                  right.appendChild(refreshAllBtn);
                  right.appendChild(columnsBtn);
                  right.appendChild(delBtn);

                  header.appendChild(left);
                  header.appendChild(right);
                  card.appendChild(header);

                  const table = document.createElement("table");
                  const thead = document.createElement("thead");
                  thead.innerHTML = "<tr>" +
                    "<th class='col-actions'></th>" +
                    "<th class='col-num'>#</th>" +
                    "<th class='col-bpm'>BPM</th>" +
                    "<th class='col-key'>Key</th>" +
                    "<th class='col-pop'>Pop</th>" +
                    "<th class='col-dur'>Dur</th>" +
                    "<th class='col-energy'>En</th>" +
                    "<th class='col-val' title='Valence: musical positiveness (0-100%)'>Val</th>" +
                    "<th class='col-danceability'>Dance</th>" +
                    "<th class='col-acousticness'>Acoustic</th>" +
                    "<th class='col-speechiness'>Speech</th>" +
                    "<th class='col-genres'>Genres</th>" +
                    "<th class='col-year'>Year</th>" +
                    "<th class='col-canonical'>Database name</th>" +
                    "<th>Track</th>" +
                    "<th class='col-stars'>★</th>" +
                    "<th class='col-notes'>Notes</th>" +
                    "</tr>";
                  table.appendChild(thead);

                  const tbody = document.createElement("tbody");
                  const singleArtist = (album.single_artist || "").trim();
                  (album.tracks || []).forEach(t => {
                    const tr = document.createElement("tr");
                    tr.draggable = true;
                    const bpm = t.bpm != null ? t.bpm : "";
                    const camelot = t.camelot || "";
                    const pop = t.popularity != null ? t.popularity : "";
                    // Format duration as mm:ss
                    let dur = "";
                    if (t.duration_ms) {
                      const totalSeconds = Math.round((t.duration_ms || 0) / 1000);
                      const minutes = Math.floor(totalSeconds / 60);
                      const seconds = totalSeconds % 60;
                      dur = `${minutes}:${String(seconds).padStart(2, '0')}`;
                    }
                    // Format energy and valence using formatPercentage helper
                    const energy = t.energy != null ? formatPercentage(t.energy) : "";
                    const val = t.valence != null ? formatPercentage(t.valence) : "";
                    const danceability = t.danceability != null ? formatPercentage(t.danceability) : "";
                    const acousticness = t.acousticness != null ? formatPercentage(t.acousticness) : "";
                    const speechiness = t.speechiness != null ? formatPercentage(t.speechiness) : "";
                    const genres = t.genres || "";
                    const releaseYear = t.release_year || "";
                    const canonicalName = (t.artists ? t.artists + " - " : "") + (t.name || "");
                    let label = t.name || "";

                    if (!singleArtist && t.artists && t.artists.trim()) {
                      label = t.artists + " - " + label;
                    }

                    tr.dataset.spotifyId = t.id || "";

                    // Helper to check if value should be marked as API (including 0)
                    const hasApiValue = (val) => val !== null && val !== undefined && val !== "";
                    
                    tr.innerHTML =
                      "<td class='col-actions'>" +
                      "<span class='drag-handle' title='Drag to reorder'>⋮⋮</span>" +
                      "<button type='button' class='track-btn' data-action='refresh' title='Re-query BPM/Key/Pop'>⟳</button>" +
                      "<button type='button' class='track-btn' data-action='clear-api' title='Clear API-fetched data'>⌫</button>" +
                      "<button type='button' class='track-btn' data-action='delete' title='Remove track'>✖</button>" +
                      "</td>" +
                      "<td class='col-num' contenteditable='true'>" + (t.track_number || "") + "</td>" +
                      "<td class='col-bpm' contenteditable='true'" + (hasApiValue(bpm) ? " data-source='api'" : "") + ">" + bpm + "</td>" +
                      "<td class='col-key' contenteditable='true'" + (hasApiValue(camelot) ? " data-source='api'" : "") + ">" + camelot + "</td>" +
                      "<td class='col-pop pop-val' contenteditable='true'" + (hasApiValue(pop) ? " data-source='api'" : "") + ">" + pop + "</td>" +
                      "<td class='col-dur' contenteditable='true'" + (hasApiValue(dur) ? " data-source='api'" : "") + ">" + dur + "</td>" +
                      "<td class='col-energy' contenteditable='true'" + (hasApiValue(energy) ? " data-source='api'" : "") + ">" + energy + "</td>" +
                      "<td class='col-val' contenteditable='true'" + (hasApiValue(val) ? " data-source='api'" : "") + ">" + val + "</td>" +
                      "<td class='col-danceability' contenteditable='true'" + (hasApiValue(danceability) ? " data-source='api'" : "") + ">" + danceability + "</td>" +
                      "<td class='col-acousticness' contenteditable='true'" + (hasApiValue(acousticness) ? " data-source='api'" : "") + ">" + acousticness + "</td>" +
                      "<td class='col-speechiness' contenteditable='true'" + (hasApiValue(speechiness) ? " data-source='api'" : "") + ">" + speechiness + "</td>" +
                      "<td class='col-genres' contenteditable='true'" + (hasApiValue(genres) ? " data-source='api'" : "") + ">" + escapeHtml(genres) + "</td>" +
                      "<td class='col-year' contenteditable='true'" + (hasApiValue(releaseYear) ? " data-source='api'" : "") + ">" + releaseYear + "</td>" +
                      "<td class='col-canonical' contenteditable='false' title='Database name from Spotify'" + (hasApiValue(canonicalName) ? " data-source='api'" : "") + ">" + escapeHtml(canonicalName) + "</td>" +
                      "<td contenteditable='true'><div class='track-label' data-autocomplete='track'>" + escapeHtml(label) + "</div></td>" +
                      "<td class='col-stars'>" + makeStarsCell(t.stars || "") + "</td>" +
                      "<td class='col-notes' contenteditable='true'>" + escapeHtml(t.notes || "") + "</td>";
                    tbody.appendChild(tr);
                  });
                table.appendChild(tbody);
                card.appendChild(table);
                results.appendChild(card);
              });
              
              // Initialize column resize for new tables and apply widths
              setTimeout(() => {
                initColumnResize();
                applyColumnWidths();
              }, 50);
              } else {
                // tracks mode - one big card with all tracks
                const card = document.createElement("div");
                card.className = "card";
                card.draggable = true;
                card.dataset.defaultStripName = "";

                const header = document.createElement("div");
                header.className = "card-header";

                const left = document.createElement("div");
                left.className = "card-header-main";
                const title = document.createElement("div");
                title.className = "card-title";
                title.textContent = "Tracks";
                title.setAttribute("contenteditable", "true");
                const subtitle = document.createElement("div");
                subtitle.className = "card-subtitle";
                subtitle.textContent = "Mixed tracks list";
                subtitle.setAttribute("contenteditable", "true");
                left.appendChild(title);
                left.appendChild(subtitle);

                const right = document.createElement("div");
                right.className = "card-actions";
                const badge = document.createElement("div");
                badge.className = "badge";
                badge.textContent = (data.items || []).length + " tracks";

                const addBtn = document.createElement("button");
                addBtn.type = "button";
                addBtn.className = "track-btn";
                addBtn.setAttribute("data-card-action", "add-track");
                addBtn.title = "Add new empty track row";
                addBtn.textContent = "+ Row";

                const stripBtn = document.createElement("button");
                stripBtn.type = "button";
                stripBtn.className = "track-btn";
                stripBtn.setAttribute("data-card-action", "strip-name");
                stripBtn.title = "Remove a name from all track labels in this card";
                stripBtn.textContent = "🧹 Name";

                const refreshAllBtn = document.createElement("button");
                refreshAllBtn.type = "button";
                refreshAllBtn.className = "track-btn";
                refreshAllBtn.setAttribute("data-card-action", "refresh-all");
                refreshAllBtn.title = "Refresh all tracks in this card";
                refreshAllBtn.textContent = "⟳ All";

                const columnsBtn = document.createElement("button");
                columnsBtn.type = "button";
                columnsBtn.className = "track-btn";
                columnsBtn.setAttribute("data-card-action", "columns");
                columnsBtn.title = "Toggle column visibility for this card";
                columnsBtn.textContent = "📊";

                const delBtn = makeDeleteButton(card);
                right.appendChild(badge);
                right.appendChild(addBtn);
                right.appendChild(stripBtn);
                right.appendChild(refreshAllBtn);
                right.appendChild(columnsBtn);
                right.appendChild(delBtn);

                header.appendChild(left);
                header.appendChild(right);
                card.appendChild(header);

                const table = document.createElement("table");
                const thead = document.createElement("thead");
                thead.innerHTML = "<tr>" +
                  "<th class='col-num'>#</th>" +
                  "<th class='col-bpm'>BPM</th>" +
                  "<th class='col-key'>Key</th>" +
                  "<th class='col-pop'>Pop</th>" +
                  "<th class='col-dur'>Dur</th>" +
                  "<th class='col-energy'>En</th>" +
                  "<th class='col-val' title='Valence: musical positiveness (0-100%)'>Val</th>" +
                  "<th class='col-danceability'>Dance</th>" +
                  "<th class='col-acousticness'>Acoustic</th>" +
                  "<th class='col-speechiness'>Speech</th>" +
                  "<th class='col-genres'>Genres</th>" +
                  "<th class='col-year'>Year</th>" +
                  "<th class='col-canonical'>Database name</th>" +
                  "<th>Track</th>" +
                  "<th class='col-stars'>★</th>" +
                  "<th class='col-notes'>Notes</th>" +
                  "<th class='col-actions'></th>" +
                  "</tr>";
                  table.appendChild(thead);
                  // Add resize handles to headers
                  thead.querySelectorAll("th").forEach(th => {
                    if (!th.querySelector(".resize-handle")) {
                      const handle = document.createElement("div");
                      handle.className = "resize-handle";
                      th.appendChild(handle);
                      handle.addEventListener("mousedown", startResize);
                    }
                  });
                  const tbody = document.createElement("tbody");
                (data.items || []).forEach((t, idx) => {
                  const bpm = t.bpm != null ? t.bpm : "";
                  const camelot = t.camelot || "";
                  const pop = t.popularity != null ? t.popularity : "";
                  // Format duration as mm:ss
                  let dur = "";
                  if (t.duration_ms) {
                    const totalSeconds = Math.round((t.duration_ms || 0) / 1000);
                    const minutes = Math.floor(totalSeconds / 60);
                    const seconds = totalSeconds % 60;
                    dur = `${minutes}:${String(seconds).padStart(2, '0')}`;
                  }
                  // Format energy and valence using formatPercentage helper
                  const energy = t.energy != null ? formatPercentage(t.energy) : "";
                  const val = t.valence != null ? formatPercentage(t.valence) : "";
                  const danceability = t.danceability != null ? formatPercentage(t.danceability) : "";
                  const acousticness = t.acousticness != null ? formatPercentage(t.acousticness) : "";
                  const speechiness = t.speechiness != null ? formatPercentage(t.speechiness) : "";
                  const genres = t.genres || "";
                  const releaseYear = t.release_year || "";
                  const canonicalName = (t.artists ? t.artists + " - " : "") + (t.name || "");
                  let label = t.name || "";
                  if (t.artists && t.artists.trim()) {
                    label = t.artists + " - " + label;
                  }
                  const tr = document.createElement("tr");
                  tr.draggable = true;
                  tr.dataset.spotifyId = t.id || "";
                  // Helper to check if value should be marked as API (including 0)
                  const hasApiValue = (val) => val !== null && val !== undefined && val !== "";
                  
                  tr.innerHTML =
                    "<td class='col-actions'>" +
                    "<span class='drag-handle' title='Drag to reorder'>⋮⋮</span>" +
                    "<button type='button' class='track-btn' data-action='refresh' title='Re-query BPM/Key/Pop'>⟳</button>" +
                    "<button type='button' class='track-btn' data-action='clear-api' title='Clear API-fetched data'>⌫</button>" +
                    "<button type='button' class='track-btn' data-action='delete' title='Remove track'>✖</button>" +
                    "</td>" +
                    "<td class='col-num' contenteditable='true'>" + (idx + 1) + "</td>" +
                    "<td class='col-bpm' contenteditable='true'" + (hasApiValue(bpm) ? " data-source='api'" : "") + ">" + bpm + "</td>" +
                    "<td class='col-key' contenteditable='true'" + (hasApiValue(camelot) ? " data-source='api'" : "") + ">" + camelot + "</td>" +
                    "<td class='col-pop pop-val' contenteditable='true'" + (hasApiValue(pop) ? " data-source='api'" : "") + ">" + pop + "</td>" +
                    "<td class='col-dur' contenteditable='true'" + (hasApiValue(dur) ? " data-source='api'" : "") + ">" + dur + "</td>" +
                    "<td class='col-energy' contenteditable='true'" + (hasApiValue(energy) ? " data-source='api'" : "") + ">" + energy + "</td>" +
                    "<td class='col-val' contenteditable='true'" + (hasApiValue(val) ? " data-source='api'" : "") + ">" + val + "</td>" +
                    "<td class='col-danceability' contenteditable='true'" + (hasApiValue(danceability) ? " data-source='api'" : "") + ">" + danceability + "</td>" +
                    "<td class='col-acousticness' contenteditable='true'" + (hasApiValue(acousticness) ? " data-source='api'" : "") + ">" + acousticness + "</td>" +
                    "<td class='col-speechiness' contenteditable='true'" + (hasApiValue(speechiness) ? " data-source='api'" : "") + ">" + speechiness + "</td>" +
                    "<td class='col-genres' contenteditable='true'" + (hasApiValue(genres) ? " data-source='api'" : "") + ">" + escapeHtml(genres) + "</td>" +
                    "<td class='col-year' contenteditable='true'" + (hasApiValue(releaseYear) ? " data-source='api'" : "") + ">" + releaseYear + "</td>" +
                    "<td class='col-canonical' contenteditable='false' title='Database name from Spotify'" + (hasApiValue(canonicalName) ? " data-source='api'" : "") + ">" + escapeHtml(canonicalName) + "</td>" +
                    "<td contenteditable='true'><div class='track-label' data-autocomplete='track'>" + escapeHtml(label) + "</div></td>" +
                    "<td class='col-stars'>" + makeStarsCell(t.stars || "") + "</td>" +
                    "<td class='col-notes' contenteditable='true'>" + escapeHtml(t.notes || "") + "</td>";
                  tbody.appendChild(tr);
                });
                table.appendChild(tbody);
                card.appendChild(table);
                results.appendChild(card);
              }

              syncColumnVisibility();
            }

            async function refreshAllTracksInCard(card) {
              const rows = card.querySelectorAll("tbody tr");
              if (rows.length === 0) {
                status.textContent = "No tracks to refresh in this card.";
                return;
              }
              
              const totalRows = rows.length;
              
              // Show confirmation with options
              const mode = window.confirm(
                `Refresh ${totalRows} tracks in this card?\n\n` +
                `OK = Fill empty fields only (safe - won't overwrite existing data)\n` +
                `Cancel = Update all fields (may overwrite manually entered data)`
              );
              
              const fillEmptyOnly = mode; // OK = true (fill empty only), Cancel = false (update all)
              
              if (!mode && !window.confirm(
                `Warning: This will update ALL fields, including overwriting any manually entered data.\n\n` +
                `Are you sure you want to proceed?`
              )) {
                status.textContent = "Refresh cancelled.";
                return;
              }
              
              let completed = 0;
              let failed = 0;
              let skipped = 0;
              
              status.textContent = `Refreshing ${totalRows} tracks in card...`;
              
              // Pre-infer artist from card context (once for all tracks in card)
              const inferredArtist = inferArtistFromContext(null, card);
              
              for (const row of Array.from(rows)) {
                try {
                  const result = await refreshRow(row, false, true, fillEmptyOnly, inferredArtist); // false = don't show selection modal, true = bulk operation, inferredArtist from card context
                  if (result === 'skipped') {
                    skipped++;
                  } else {
                    completed++;
                  }
                  status.textContent = `Refreshing ${completed + skipped}/${totalRows} tracks...`;
                } catch (err) {
                  console.error("Error refreshing track:", err);
                  failed++;
                }
              }
              
              status.textContent = `Refreshed ${completed} tracks${skipped > 0 ? `, ${skipped} skipped (already had data)` : ''}${failed > 0 ? `, ${failed} failed` : ''}.`;
            }

            async function refreshAllTracksGlobally() {
              const allRows = document.querySelectorAll(".card tbody tr");
              if (allRows.length === 0) {
                status.textContent = "No tracks to refresh.";
                return;
              }
              
              const totalRows = allRows.length;
              
              // Show confirmation with options
              const mode = window.confirm(
                `Refresh ${totalRows} tracks across all cards?\n\n` +
                `OK = Fill empty fields only (safe - won't overwrite existing data)\n` +
                `Cancel = Update all fields (may overwrite manually entered data)\n\n` +
                `Note: This may take a while and make many API calls.\n` +
                `It's recommended to refresh tracks card by card instead.`
              );
              
              const fillEmptyOnly = mode; // OK = true (fill empty only), Cancel = false (update all)
              
              if (!mode && !window.confirm(
                `Warning: This will update ALL fields for ${totalRows} tracks, including overwriting any manually entered data.\n\n` +
                `Are you sure you want to proceed?`
              )) {
                status.textContent = "Global refresh cancelled.";
                return;
              }
              
              let completed = 0;
              let failed = 0;
              let skipped = 0;
              
              status.textContent = `Refreshing ${totalRows} tracks globally...`;
              
              // Group rows by card for context-aware artist inference
              const rowsByCard = new Map();
              for (const row of Array.from(allRows)) {
                const card = row.closest(".card");
                if (!card) continue;
                if (!rowsByCard.has(card)) {
                  rowsByCard.set(card, []);
                }
                rowsByCard.get(card).push(row);
              }
              
              // Process each card's tracks with context
              for (const [card, cardRows] of rowsByCard.entries()) {
                // Pre-infer artist from card context (once for all tracks in card)
                const inferredArtist = inferArtistFromContext(null, card);
                
                for (const row of cardRows) {
                  try {
                    const result = await refreshRow(row, false, true, fillEmptyOnly, inferredArtist); // false = don't show selection modal, true = bulk operation, inferredArtist from card context
                    if (result === 'skipped') {
                      skipped++;
                    } else {
                      completed++;
                    }
                    if ((completed + skipped) % 10 === 0) {
                      status.textContent = `Refreshing ${completed + skipped}/${totalRows} tracks...`;
                    }
                  } catch (err) {
                    console.error("Error refreshing track:", err);
                    failed++;
                  }
                }
              }
              
              status.textContent = `Refreshed ${completed} tracks${skipped > 0 ? `, ${skipped} skipped (already had data)` : ''}${failed > 0 ? `, ${failed} failed` : ''}.`;
            }

            // Helper function to infer artist from card title and surrounding tracks
            function inferArtistFromContext(tr, card) {
              // First, try to extract artist from card title/subtitle
              if (card) {
                const titleEl = card.querySelector(".card-title");
                const subtitleEl = card.querySelector(".card-subtitle");
                const cardTitle = titleEl ? titleEl.textContent.trim() : "";
                const cardSubtitle = subtitleEl ? subtitleEl.textContent.trim() : "";
                
                // Try to extract artist from card title (format: "Artist - Album" or just "Artist")
                const cardText = cardTitle + (cardSubtitle ? " " + cardSubtitle : "");
                const cardDashIndex = cardText.indexOf(" - ");
                if (cardDashIndex !== -1) {
                  const possibleArtist = cardText.slice(0, cardDashIndex).trim();
                  if (possibleArtist && possibleArtist.length > 0) {
                    return possibleArtist;
                  }
                } else if (cardTitle && cardTitle.length > 0 && cardTitle.length < 50) {
                  // If no dash, might be just artist name (if reasonably short)
                  return cardTitle;
                }
              }
              
              // Second, look at other tracks in the same card to find artist names
              if (card) {
                const allRows = card.querySelectorAll("tbody tr");
                const artistCounts = {};
                
                for (const row of Array.from(allRows)) {
                  if (row === tr) continue; // Skip current track
                  
                  const rowTds = Array.from(row.querySelectorAll("td"));
                  const rowLabelCell = rowTds.find(td => td.querySelector(".track-label"));
                  if (rowLabelCell) {
                    const rowLabelDiv = rowLabelCell.querySelector(".track-label") || rowLabelCell;
                    const rowLabel = (rowLabelDiv.textContent || "").trim();
                    const rowDashIndex = rowLabel.indexOf(" - ");
                    if (rowDashIndex !== -1) {
                      const rowArtist = rowLabel.slice(0, rowDashIndex).trim();
                      if (rowArtist && rowArtist.length > 0) {
                        artistCounts[rowArtist] = (artistCounts[rowArtist] || 0) + 1;
                      }
                    }
                  }
                }
                
                // Return the most common artist found in surrounding tracks
                if (Object.keys(artistCounts).length > 0) {
                  const mostCommonArtist = Object.keys(artistCounts).reduce((a, b) => 
                    artistCounts[a] > artistCounts[b] ? a : b
                  );
                  return mostCommonArtist;
                }
              }
              
              return "";
            }

            async function refreshRow(tr, showModal = true, isBulkOperation = false, fillEmptyOnly = false, inferredArtist = null) {
              const tds = Array.from(tr.querySelectorAll("td"));
              if (tds.length < 9) return;
              
              // Find cells by their class, not index (since columns can be reordered)
              const labelCell = tds.find(td => td.querySelector(".track-label"));
              if (!labelCell) {
                status.textContent = "Track label cell not found.";
                return;
              }
              
              const labelDiv = labelCell.querySelector(".track-label") || labelCell;
              const label = (labelDiv.textContent || "").trim();
              if (!label) {
                status.textContent = "Track label is empty, cannot re-query.";
                return;
              }
              let artists = "";
              let title = label;
              const dashIndex = label.indexOf(" - ");
              if (dashIndex !== -1) {
                artists = label.slice(0, dashIndex).trim();
                title = label.slice(dashIndex + 3).trim();
              } else {
                // No artist in track name - use inferred artist if available
                if (inferredArtist === null) {
                  // Auto-infer from context if not provided
                  const card = tr.closest(".card");
                  inferredArtist = inferArtistFromContext(tr, card);
                }
                if (inferredArtist) {
                  artists = inferredArtist;
                }
              }

              status.textContent = "Searching for track...";

              try {
                // First, search for multiple results
                const searchRes = await fetch("/api/refresh_track", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    spotify_id: null,
                    artists,
                    title,
                    search_mode: true
                  }),
                });
                const searchData = await searchRes.json();
                
                let selectedTrackId = null;
                
                if (searchData.results && searchData.results.length > 1) {
                  if (showModal) {
                    // Show selection modal
                    await new Promise((resolve) => {
                      showSelectionModal(
                        `Multiple tracks found for: ${escapeHtml(label)}`,
                        searchData.results,
                        (selected) => {
                          selectedTrackId = selected.id;
                          resolve();
                        }
                      );
                    });
                  } else {
                    // Use first result when refreshing all
                    selectedTrackId = searchData.results[0].id;
                  }
                } else if (searchData.results && searchData.results.length === 1) {
                  selectedTrackId = searchData.results[0].id;
                } else {
                  if (showModal) {
                    status.textContent = "No tracks found.";
                  }
                  return;
                }
                
                if (!selectedTrackId) {
                  status.textContent = "No track selected.";
                  return;
                }

                status.textContent = "Refreshing track audio features...";

                // Now fetch the selected track's data
                const res = await fetch("/api/refresh_track", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    selected_track_id: selectedTrackId,
                    artists,
                    title
                  }),
                });
                const data = await res.json();
                if (!res.ok || data.error) {
                  status.textContent = "Refresh failed: " + (data.error || res.statusText);
                  return;
                }
                
                // Update all toggled columns based on columnConfig
                const thead = tr.closest("table")?.querySelector("thead tr");
                let anyFieldUpdated = false; // Track if any field was actually updated
                
                if (thead) {
                  const headers = Array.from(thead.querySelectorAll("th"));
                  headers.forEach((header) => {
                    // Find corresponding cell by matching column class
                    const cell = tds.find(td => {
                      // Check if cell has the same column classes as header
                      const headerClasses = Array.from(header.classList);
                      const cellClasses = Array.from(td.classList);
                      
                      // Special case for track column
                      if (header.textContent.trim() === "Track" || (!header.className && header.textContent.trim() === "Track")) {
                        return td.querySelector(".track-label");
                      }
                      
                      // Match by column class
                      return headerClasses.some(cls => cls.startsWith("col-") && cellClasses.includes(cls));
                    });
                    
                    if (!cell) return;
                    
                    // Helper function to check if a cell is empty
                    const isCellEmpty = (cell) => {
                      const text = cell.textContent.trim();
                      return !text || text === "";
                    };
                    
                    // Skip updating if cell has manual data (unless fillEmptyOnly and cell is empty)
                    const hasManualData = cell.getAttribute("data-source") === "manual";
                    if (hasManualData && !(fillEmptyOnly && isCellEmpty(cell))) {
                      return; // Skip this cell - it has manual data
                    }
                    
                    // Update based on column type and toggle state
                    if (header.classList.contains("col-bpm") && columnConfig.bpm) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = data.bpm != null ? data.bpm : "";
                        if (data.bpm != null) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-key") && columnConfig.key) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = data.camelot || "";
                        if (data.camelot) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-pop") && columnConfig.pop) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = data.popularity != null ? data.popularity : "";
                        if (data.popularity != null) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-dur") && columnConfig.dur) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        if (data.duration_ms) {
                          const totalSeconds = Math.round((data.duration_ms || 0) / 1000);
                          const minutes = Math.floor(totalSeconds / 60);
                          const seconds = totalSeconds % 60;
                          cell.textContent = `${minutes}:${String(seconds).padStart(2, '0')}`;
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        } else {
                          if (!fillEmptyOnly) cell.textContent = "";
                        }
                      }
                    } else if (header.classList.contains("col-energy") && columnConfig.energy) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = formatPercentage(data.energy);
                        if (data.energy != null) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-val") && columnConfig.val) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = formatPercentage(data.valence);
                        if (data.valence != null) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-danceability") && columnConfig.danceability) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = formatPercentage(data.danceability);
                        if (data.danceability != null) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-acousticness") && columnConfig.acousticness) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = formatPercentage(data.acousticness);
                        if (data.acousticness != null) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-speechiness") && columnConfig.speechiness) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = formatPercentage(data.speechiness);
                        if (data.speechiness != null) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-genres") && columnConfig.genres) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = data.genres || "";
                        if (data.genres) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-year") && columnConfig.year) {
                      if (!fillEmptyOnly || isCellEmpty(cell)) {
                        cell.textContent = data.release_year || "";
                        if (data.release_year) {
                          anyFieldUpdated = true;
                          cell.setAttribute("data-source", "api");
                        }
                      }
                    } else if (header.classList.contains("col-canonical") && columnConfig.canonical) {
                      // Update canonical name column (read-only, shows database name)
                      let canonicalName = data.name || title;
                      if (data.artists) {
                        canonicalName = data.artists + " - " + canonicalName;
                      }
                      cell.textContent = canonicalName || "";
                      if (canonicalName) {
                        cell.setAttribute("data-source", "api");
                      }
                    } else if ((header.textContent.trim() === "Track" || (!header.className && header.textContent.trim() === "Track")) && cell.querySelector(".track-label")) {
                      // Update track label - but skip during bulk operations to preserve existing labels
                      if (!isBulkOperation) {
                        const currentLabel = label;
                        let newLabel = data.name || title;
                        if (data.artists) {
                          newLabel = data.artists + " - " + newLabel;
                        }
                        if (currentLabel && currentLabel.trim() && currentLabel.trim() !== newLabel.trim()) {
                          // Check if alert is enabled
                          if (showTrackNameAlert) {
                            const replace = window.confirm(
                              "Replace track label with canonical name from database?\\n\\n" +
                              "Current: " + currentLabel + "\\n" +
                              "New:     " + newLabel
                            );
                            if (replace) {
                              cell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(newLabel) + "</div>";
                            } else {
                              cell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(currentLabel) + "</div>";
                            }
                          } else {
                            // Alert disabled - keep existing label
                            cell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(currentLabel) + "</div>";
                          }
                        } else {
                          cell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(newLabel) + "</div>";
                        }
                      }
                      // During bulk operations, keep existing label unchanged
                    } else if (header.classList.contains("col-stars") && columnConfig.stars) {
                      // Skip updating stars during refresh (user field)
                    } else if (header.classList.contains("col-notes") && columnConfig.notes) {
                      // Skip updating notes during refresh (user field)
                    }
                  });
                } else {
                  // Fallback: find cells by class
                  const bpmCell = tds.find(td => td.classList.contains("col-bpm"));
                  const keyCell = tds.find(td => td.classList.contains("col-key"));
                  const popCell = tds.find(td => td.classList.contains("col-pop"));
                  
                  // Helper function to check if a cell is empty
                  const isCellEmpty = (cell) => {
                    if (!cell) return false;
                    const text = cell.textContent.trim();
                    return !text || text === "";
                  };
                  
                  if (bpmCell && columnConfig.bpm) {
                    if (!fillEmptyOnly || isCellEmpty(bpmCell)) {
                      bpmCell.textContent = data.bpm != null ? data.bpm : "";
                      if (data.bpm != null) anyFieldUpdated = true;
                    }
                  }
                  if (keyCell && columnConfig.key) {
                    if (!fillEmptyOnly || isCellEmpty(keyCell)) {
                      keyCell.textContent = data.camelot || "";
                      if (data.camelot) anyFieldUpdated = true;
                    }
                  }
                  if (popCell && columnConfig.pop) {
                    if (!fillEmptyOnly || isCellEmpty(popCell)) {
                      popCell.textContent = data.popularity != null ? data.popularity : "";
                      if (data.popularity != null) anyFieldUpdated = true;
                    }
                  }
                  
                  // Update canonical column if visible
                  const canonicalCell = tds.find(td => td.classList.contains("col-canonical"));
                  if (canonicalCell && columnConfig.canonical) {
                    let canonicalName = data.name || title;
                    if (data.artists) {
                      canonicalName = data.artists + " - " + canonicalName;
                    }
                    canonicalCell.textContent = canonicalName || "";
                  }
                  
                  // Update track label - but skip during bulk operations to preserve existing labels
                  if (!isBulkOperation) {
                    const currentLabel = label;
                    let newLabel = data.name || title;
                    if (data.artists) {
                      newLabel = data.artists + " - " + newLabel;
                    }
                    if (currentLabel && currentLabel.trim() && currentLabel.trim() !== newLabel.trim()) {
                      // Check if alert is enabled
                      if (showTrackNameAlert) {
                        const replace = window.confirm(
                          "Replace track label with canonical name from database?\\n\\n" +
                          "Current: " + currentLabel + "\\n" +
                          "New:     " + newLabel
                        );
                        if (replace) {
                          labelCell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(newLabel) + "</div>";
                        } else {
                          labelCell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(currentLabel) + "</div>";
                        }
                      } else {
                        // Alert disabled - keep existing label
                        labelCell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(currentLabel) + "</div>";
                      }
                    } else {
                      labelCell.innerHTML = "<div class='track-label' data-autocomplete='track'>" + escapeHtml(newLabel) + "</div>";
                    }
                  }
                  // During bulk operations, keep existing label unchanged
                }

                if (fillEmptyOnly && !anyFieldUpdated) {
                  return 'skipped'; // Track already had all data, nothing to update
                }
                
                status.textContent = "Track updated from database.";
                return 'updated';
              } catch (err) {
                console.error(err);
                status.textContent = "Network error refreshing track.";
              }
            }
            
            // Clear API data function
            function clearApiData(tr) {
              const tds = Array.from(tr.querySelectorAll("td"));
              const apiColumns = ["col-bpm", "col-key", "col-pop", "col-dur", "col-energy", "col-val", 
                                  "col-danceability", "col-acousticness", "col-speechiness", "col-genres", 
                                  "col-year", "col-canonical"];
              
              let clearedCount = 0;
              tds.forEach(td => {
                const hasApiSource = td.getAttribute("data-source") === "api";
                const isApiColumn = apiColumns.some(col => td.classList.contains(col));
                
                if (hasApiSource && isApiColumn) {
                  td.textContent = "";
                  td.removeAttribute("data-source");
                  clearedCount++;
                }
              });
              
              if (clearedCount > 0) {
                status.textContent = `Cleared ${clearedCount} API-fetched field(s).`;
              } else {
                status.textContent = "No API-fetched data to clear.";
              }
            }

            // Helper function to format percentage values (0-1 to 0-100 or 0-9)
            function formatPercentage(value) {
              if (value == null || value === "") return "";
              const num = typeof value === "number" ? value : parseFloat(value);
              if (isNaN(num)) return "";
              // If value is already in 0-100 range (from API), normalize to 0-1 first
              const normalized = num > 1 ? num / 100 : num;
              if (singleDigitScale) {
                return Math.round(normalized * 10); // 0-1 scale to 0-9
              } else {
                return Math.round(normalized * 100); // 0-1 scale to 0-100
              }
            }

            // Per-card column visibility storage
            const cardColumnConfigs = new Map();
            
            function getCardColumnConfig(card) {
              if (!cardColumnConfigs.has(card)) {
                // Initialize with global config
                cardColumnConfigs.set(card, {...columnConfig});
              }
              return cardColumnConfigs.get(card);
            }
            
            function syncColumnVisibility(card = null) {
              // If card is specified, sync only that card; otherwise sync all cards with global config
              const cards = card ? [card] : document.querySelectorAll(".card");
              
              cards.forEach(c => {
                const cardConfig = card ? getCardColumnConfig(c) : columnConfig;
                
                const showBpm = cardConfig.bpm;
                const showKey = cardConfig.key;
                const showPop = cardConfig.pop;
                const showDur = cardConfig.dur;
                const showEnergy = cardConfig.energy;
                const showVal = cardConfig.val;
                const showDanceability = cardConfig.danceability;
                const showAcousticness = cardConfig.acousticness;
                const showSpeechiness = cardConfig.speechiness;
                const showGenres = cardConfig.genres;
                const showYear = cardConfig.year;
                const showCanonical = cardConfig.canonical;
                const showStars = cardConfig.stars;
                const showNotes = cardConfig.notes;

                const toggleCol = (selector, show) => {
                  c.querySelectorAll(selector).forEach(el => {
                    el.style.display = show ? "" : "none";
                  });
                };

                toggleCol(".col-bpm", showBpm);
                toggleCol(".col-key", showKey);
                toggleCol(".col-pop", showPop);
                toggleCol(".col-dur", showDur);
                toggleCol(".col-energy", showEnergy);
                toggleCol(".col-val", showVal);
                toggleCol(".col-danceability", showDanceability);
                toggleCol(".col-acousticness", showAcousticness);
                toggleCol(".col-speechiness", showSpeechiness);
                toggleCol(".col-genres", showGenres);
                toggleCol(".col-year", showYear);
                toggleCol(".col-canonical", showCanonical);
                toggleCol(".col-stars", showStars);
                toggleCol(".col-notes", showNotes);
              });
            }
            
            function showCardColumnModal(card) {
              const cardConfig = getCardColumnConfig(card);
              
              const modal = document.createElement("div");
              modal.className = "card-columns-modal";
              modal.innerHTML = `
                <div class="card-columns-modal-content">
                  <h3>Column Visibility</h3>
                  <div class="card-columns-list">
                    <label><input type="checkbox" data-col="bpm" ${cardConfig.bpm ? 'checked' : ''} /> BPM</label>
                    <label><input type="checkbox" data-col="key" ${cardConfig.key ? 'checked' : ''} /> Key</label>
                    <label><input type="checkbox" data-col="pop" ${cardConfig.pop ? 'checked' : ''} /> Pop</label>
                    <label><input type="checkbox" data-col="dur" ${cardConfig.dur ? 'checked' : ''} /> Dur</label>
                    <label><input type="checkbox" data-col="energy" ${cardConfig.energy ? 'checked' : ''} /> Energy</label>
                    <label><input type="checkbox" data-col="val" ${cardConfig.val ? 'checked' : ''} /> Valence</label>
                    <label><input type="checkbox" data-col="danceability" ${cardConfig.danceability ? 'checked' : ''} /> Dance</label>
                    <label><input type="checkbox" data-col="acousticness" ${cardConfig.acousticness ? 'checked' : ''} /> Acoustic</label>
                    <label><input type="checkbox" data-col="speechiness" ${cardConfig.speechiness ? 'checked' : ''} /> Speech</label>
                    <label><input type="checkbox" data-col="genres" ${cardConfig.genres ? 'checked' : ''} /> Genres</label>
                    <label><input type="checkbox" data-col="year" ${cardConfig.year ? 'checked' : ''} /> Year</label>
                    <label><input type="checkbox" data-col="canonical" ${cardConfig.canonical ? 'checked' : ''} /> Database name</label>
                    <label><input type="checkbox" data-col="stars" ${cardConfig.stars ? 'checked' : ''} /> Stars</label>
                    <label><input type="checkbox" data-col="notes" ${cardConfig.notes ? 'checked' : ''} /> Notes</label>
                  </div>
                  <div class="card-columns-modal-actions">
                    <button class="btn btn-outline" id="cardColumnsReset">Reset to Global</button>
                    <button class="btn" id="cardColumnsClose">Close</button>
                  </div>
                </div>
              `;
              
              document.body.appendChild(modal);
              
              const checkboxes = modal.querySelectorAll("input[type='checkbox']");
              checkboxes.forEach(cb => {
                cb.addEventListener("change", () => {
                  const col = cb.dataset.col;
                  cardConfig[col] = cb.checked;
                  syncColumnVisibility(card);
                });
              });
              
              modal.querySelector("#cardColumnsReset").addEventListener("click", () => {
                // Reset to global config
                Object.keys(columnConfig).forEach(key => {
                  cardConfig[key] = columnConfig[key];
                });
                cardColumnConfigs.set(card, {...cardConfig});
                checkboxes.forEach(cb => {
                  const col = cb.dataset.col;
                  cb.checked = cardConfig[col];
                });
                syncColumnVisibility(card);
              });
              
              modal.querySelector("#cardColumnsClose").addEventListener("click", () => {
                document.body.removeChild(modal);
              });
              
              modal.addEventListener("click", (e) => {
                if (e.target === modal) {
                  document.body.removeChild(modal);
                }
              });
            }

            function downloadTxt() {
              const cards = document.querySelectorAll(".card");
              if (!cards.length) {
                status.textContent = "Nothing to export. Generate some cards first.";
                return;
              }
              
              // Get column order from first card's header
              const firstCard = cards[0];
              const firstTable = firstCard.querySelector("table");
              if (!firstTable) {
                status.textContent = "No table found in cards.";
                return;
              }
              
              const thead = firstTable.querySelector("thead tr");
              if (!thead) {
                status.textContent = "No header row found.";
                return;
              }
              
              // Build column mapping from header order (excluding actions)
              const headerCells = Array.from(thead.querySelectorAll("th"));
              const columnOrder = [];
              const columnHeaderMap = {};
              
              headerCells.forEach((th, idx) => {
                // Skip actions column
                if (th.classList.contains("col-actions")) return;
                
                let colName = null;
                if (th.classList.contains("col-num")) colName = "num";
                else if (th.classList.contains("col-bpm")) colName = "bpm";
                else if (th.classList.contains("col-key")) colName = "key";
                else if (th.classList.contains("col-pop")) colName = "pop";
                else if (th.classList.contains("col-dur")) colName = "dur";
                else if (th.classList.contains("col-energy")) colName = "energy";
                else if (th.classList.contains("col-val")) colName = "val";
                else if (th.classList.contains("col-danceability")) colName = "danceability";
                else if (th.classList.contains("col-acousticness")) colName = "acousticness";
                else if (th.classList.contains("col-speechiness")) colName = "speechiness";
                else if (th.classList.contains("col-genres")) colName = "genres";
                else if (th.classList.contains("col-year")) colName = "year";
                else if (th.classList.contains("col-canonical")) colName = "canonical";
                else if (th.classList.contains("col-stars")) colName = "stars";
                else if (th.classList.contains("col-notes")) colName = "notes";
                else if (th.textContent.trim() === "Track" || (!th.className && th.textContent.trim() === "Track")) colName = "track";
                
                if (colName) {
                  columnOrder.push(colName);
                  columnHeaderMap[colName] = th.textContent.trim();
                }
              });
              
              // Build header row based on column order and visibility
              const headerNames = {
                "num": "#",
                "bpm": "BPM",
                "key": "Key",
                "pop": "Pop",
                "dur": "Dur",
                "energy": "Energy",
                "val": "Valence",
                "danceability": "Dance",
                "acousticness": "Acoustic",
                "speechiness": "Speech",
                "genres": "Genres",
                "year": "Year",
                "canonical": "Database name",
                "track": "Track",
                "stars": "Stars",
                "notes": "Notes"
              };
              
              let lines = [];
              cards.forEach((card) => {
                const titleEl = card.querySelector(".card-title");
                const subtitleEl = card.querySelector(".card-subtitle");
                const title = titleEl ? titleEl.textContent.trim() : "";
                const subtitle = subtitleEl ? subtitleEl.textContent.trim() : "";
                if (title) {
                  if (lines.length) lines.push("");
                  let header = title;
                  if (subtitle) header += " - " + subtitle;
                  lines.push(header);
                  
                  // Build header row based on visible columns in order
                  const headerRow = columnOrder
                    .filter(col => {
                      // Always include num, track, stars, notes
                      if (col === "num" || col === "track" || col === "stars" || col === "notes") return true;
                      // Include other columns if they're visible (check columnConfig)
                      return columnConfig[col] === true;
                    })
                    .map(col => headerNames[col] || col)
                    .join("\\t");
                  lines.push(headerRow);
                }
                
                const rows = card.querySelectorAll("tbody tr");
                rows.forEach(row => {
                  const cols = Array.from(row.querySelectorAll("td"));
                  if (!cols.length) return;
                  
                  // Find cells by class instead of index
                  const getCellByClass = (className) => {
                    return cols.find(td => td.classList.contains(className));
                  };
                  
                  const getTrackLabel = () => {
                    const trackCell = cols.find(td => td.querySelector(".track-label"));
                    if (!trackCell) return "";
                    const labelDiv = trackCell.querySelector(".track-label") || trackCell;
                    return (labelDiv.textContent || "").trim();
                  };
                  
                  const getStars = () => {
                    const starsCell = getCellByClass("col-stars");
                    if (!starsCell) return "";
                    const starsWrap = starsCell.querySelector(".stars");
                    return starsWrap ? (starsWrap.dataset.value || "").trim() : "";
                  };
                  
                  // Build row data based on column order
                  const rowData = [];
                  columnOrder.forEach(col => {
                    // Always include num, track, stars, notes
                    if (col === "num" || col === "track" || col === "stars" || col === "notes") {
                      if (col === "num") {
                        const cell = getCellByClass("col-num");
                        rowData.push(cell ? (cell.textContent || "").trim() : "");
                      } else if (col === "track") {
                        rowData.push(getTrackLabel());
                      } else if (col === "stars") {
                        rowData.push(getStars());
                      } else if (col === "notes") {
                        const cell = getCellByClass("col-notes");
                        rowData.push(cell ? (cell.textContent || "").trim() : "");
                      }
                    } else if (columnConfig[col] === true) {
                      // Include other columns if visible
                      const classMap = {
                        "bpm": "col-bpm",
                        "key": "col-key",
                        "pop": "col-pop",
                        "dur": "col-dur",
                        "energy": "col-energy",
                        "val": "col-val",
                        "danceability": "col-danceability",
                        "acousticness": "col-acousticness",
                        "speechiness": "col-speechiness",
                        "genres": "col-genres",
                        "year": "col-year",
                        "canonical": "col-canonical"
                      };
                      const className = classMap[col];
                      if (className) {
                        const cell = getCellByClass(className);
                        rowData.push(cell ? (cell.textContent || "").trim() : "");
                      }
                    }
                  });
                  
                  const label = getTrackLabel();
                  if (!label) return;
                  lines.push(rowData.join("\\t"));
                });
              });
              
              const blob = new Blob([lines.join("\\n")], { type: "text/plain;charset=utf-8" });
              const url = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = url;
              a.download = "vinyl_sheets.txt";
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);
              URL.revokeObjectURL(url);
            }

            function importTxt(text) {
              text = text || "";
              if (!text.trim()) return;
              const lines = text.split(/\\r?\\n/);
              if (!lines.length) return;

              let currentCard = null;
              let currentTable = null;
              let currentTbody = null;
              let currentIndex = 0;
              let headerSeen = false;
              let format = null; // 'full' = exported with Dur/Energy/Val/Stars/Notes, 'short' = #,BPM,Key,Pop,Track

              // List of metadata words that should not be treated as track names
              const metadataWords = new Set(["BPM", "Key", "Pop", "Dur", "Duration", "Energy", "Val", "Valence", "Track", "Stars", "Notes", "Popularity", "#", "Num", "Number"]);

              lines.forEach(line => {
                line = line.trim();
                if (!line) {
                  // Empty line might indicate end of a card, reset headerSeen for next card
                  if (headerSeen) {
                    headerSeen = false;
                  }
                  return;
                }

                // Check if this is a header line (handle both escaped \t and actual tabs)
                const hasTabs = line.includes("\\t") || line.includes("\t");
                if (hasTabs && (line.includes("BPM") || line.includes("Key") || line.includes("Track"))) {
                  // Check if this looks like a header row - split on tabs or commas
                  const parts = line.split(/\t|,|\\t/);
                  const upperParts = parts.map(p => p.trim().toUpperCase());
                  const isHeader = upperParts.some(p => metadataWords.has(p.toUpperCase()));
                  if (isHeader && upperParts.length >= 3) {
                    if (!headerSeen) {
                      headerSeen = true;
                    }
                    return;
                  }
                }

                // Also check for header patterns like "#\tBPM\tKey" (escaped or actual tabs)
                if (!headerSeen && (line.includes("#\\tBPM\\tKey") || line.includes("#\tBPM\tKey") || line.match(/^#?[\s\t]*BPM[\s\t]/i))) {
                  headerSeen = true;
                  return;
                }

                // Check if this is a card header (title - subtitle format, no tabs, not a data row)
                if (!hasTabs && !headerSeen && !line.match(/^#/)) {
                  // This looks like a card header: "Title - Subtitle" or just "Title"
                  const dashIndex = line.indexOf(" - ");
                  let cardTitle = "";
                  let cardSubtitle = "";
                  
                  if (dashIndex !== -1) {
                    cardTitle = line.slice(0, dashIndex).trim();
                    cardSubtitle = line.slice(dashIndex + 3).trim();
                  } else {
                    cardTitle = line.trim();
                  }
                  
                  // Only treat as card header if it doesn't look like a track name with metadata
                  if (cardTitle && !metadataWords.has(cardTitle.toUpperCase())) {
                    // Start a new card with this title/subtitle
                    currentCard = document.createElement("div");
                    currentCard.className = "card";
                    currentCard.draggable = true;
                    currentCard.dataset.defaultStripName = "";

                    const header = document.createElement("div");
                    header.className = "card-header";

                    const left = document.createElement("div");
                    left.className = "card-header-main";
                    const title = document.createElement("div");
                    title.className = "card-title";
                    title.textContent = cardTitle;
                    title.setAttribute("contenteditable", "true");
                    const subtitle = document.createElement("div");
                    subtitle.className = "card-subtitle";
                    subtitle.textContent = cardSubtitle || "";
                    subtitle.setAttribute("contenteditable", "true");
                    left.appendChild(title);
                    left.appendChild(subtitle);

                    const right = document.createElement("div");
                    right.className = "card-actions";
                    const badge = document.createElement("div");
                    badge.className = "badge";
                    badge.textContent = "";
                    
                    const addBtn = document.createElement("button");
                    addBtn.type = "button";
                    addBtn.className = "track-btn";
                    addBtn.setAttribute("data-card-action", "add-track");
                    addBtn.title = "Add new empty track row";
                    addBtn.textContent = "+ Row";
                    
                    const stripBtn = document.createElement("button");
                    stripBtn.type = "button";
                    stripBtn.className = "track-btn";
                    stripBtn.setAttribute("data-card-action", "strip-name");
                    stripBtn.title = "Remove a name (for example primary artist) from all track labels in this card";
                    stripBtn.textContent = "🧹 Name";
                    
                    const refreshAllBtn = document.createElement("button");
                    refreshAllBtn.type = "button";
                    refreshAllBtn.className = "track-btn";
                    refreshAllBtn.setAttribute("data-card-action", "refresh-all");
                    refreshAllBtn.title = "Refresh all tracks in this card";
                    refreshAllBtn.textContent = "⟳ All";
                    
                    const columnsBtn = document.createElement("button");
                    columnsBtn.type = "button";
                    columnsBtn.className = "track-btn";
                    columnsBtn.setAttribute("data-card-action", "columns");
                    columnsBtn.title = "Toggle column visibility for this card";
                    columnsBtn.textContent = "📊";
                    
                    const delBtn = makeDeleteButton(currentCard);
                    right.appendChild(badge);
                    right.appendChild(addBtn);
                    right.appendChild(stripBtn);
                    right.appendChild(refreshAllBtn);
                    right.appendChild(columnsBtn);
                    right.appendChild(delBtn);

                    header.appendChild(left);
                    header.appendChild(right);
                    currentCard.appendChild(header);

                    currentTable = document.createElement("table");
                    const thead = document.createElement("thead");
                    thead.innerHTML = "<tr>" +
                      "<th class='col-actions'></th>" +
                      "<th class='col-num'>#</th>" +
                      "<th class='col-bpm'>BPM</th>" +
                      "<th class='col-key'>Key</th>" +
                      "<th class='col-pop'>Pop</th>" +
                      "<th class='col-dur'>Dur</th>" +
                      "<th class='col-energy'>En</th>" +
                      "<th class='col-val'>Val</th>" +
                      "<th class='col-danceability'>Dance</th>" +
                      "<th class='col-acousticness'>Acoustic</th>" +
                      "<th class='col-speechiness'>Speech</th>" +
                      "<th class='col-genres'>Genres</th>" +
                      "<th class='col-year'>Year</th>" +
                      "<th class='col-canonical'>Database name</th>" +
                      "<th>Track</th>" +
                      "<th class='col-stars'>★</th>" +
                      "<th class='col-notes'>Notes</th>" +
                      "</tr>";
                    currentTable.appendChild(thead);
                    // Add resize handles to headers
                    thead.querySelectorAll("th").forEach(th => {
                      const handle = document.createElement("div");
                      handle.className = "resize-handle";
                      th.appendChild(handle);
                      handle.addEventListener("mousedown", startResize);
                    });
                    // Add resize handles to headers
                    thead.querySelectorAll("th").forEach(th => {
                      const handle = document.createElement("div");
                      handle.className = "resize-handle";
                      th.appendChild(handle);
                      handle.addEventListener("mousedown", startResize);
                    });
                    currentTbody = document.createElement("tbody");
                    currentTable.appendChild(currentTbody);
                    currentCard.appendChild(currentTable);
                    results.appendChild(currentCard);
                    currentIndex = 0;
                    return; // Skip processing this line as data
                  }
                }

                if (headerSeen) {
                  // parse data line (handle both escaped \t and actual tabs)
                  const parts = line.includes("\t") ? line.split("\t") : line.split("\\t");
                  if (!format) {
                    format = parts.length >= 8 ? 'full' : 'short';
                  }

                  let num = parts[0] ? parts[0].trim() : "";
                  let bpm = "";
                  let key = "";
                  let pop = "";
                  let dur = "";
                  let energy = "";
                  let val = "";
                  let stars = "";
                  let notes = "";
                  let label = "";

                  if (format === 'full') {
                    if (parts.length < 8) return;
                    bpm = (parts[1] || '').trim();
                    key = (parts[2] || '').trim();
                    pop = (parts[3] || '').trim();
                    dur = (parts[4] || '').trim();
                    energy = (parts[5] || '').trim().replace(/%/g, '');
                    val = (parts[6] || '').trim().replace(/%/g, '');
                    label = (parts[7] || '').trim();
                    stars = (parts[8] || '').trim();
                    notes = (parts[9] || '').trim();
                  } else {
                    // short format: #, BPM, Key, Pop, Track
                    if (parts.length < 5) return;
                    bpm = (parts[1] || '').trim();
                    key = (parts[2] || '').trim();
                    pop = (parts[3] || '').trim();
                    label = (parts[4] || '').trim();
                  }

                  if (!label) return;

                  // Skip lines where the label is just a metadata word
                  const labelUpper = label.toUpperCase().trim();
                  if (metadataWords.has(labelUpper) || labelUpper.match(/^(BPM|KEY|POP|DUR|ENERGY|VAL|TRACK|STARS|NOTES)$/i)) {
                    return;
                  }

                  if (!currentCard || (num == "1" && currentIndex !== 0)) {
                    // start new card (fallback if no card header was found)
                    currentCard = document.createElement("div");
                    currentCard.className = "card";
                    currentCard.draggable = true;
                    currentCard.dataset.defaultStripName = "";

                    const header = document.createElement("div");
                    header.className = "card-header";

                    const left = document.createElement("div");
                    left.className = "card-header-main";
                    const title = document.createElement("div");
                    title.className = "card-title";
                    title.textContent = "Imported";
                    title.setAttribute("contenteditable", "true");
                    const subtitle = document.createElement("div");
                    subtitle.className = "card-subtitle";
                    subtitle.textContent = "Imported from .txt";
                    subtitle.setAttribute("contenteditable", "true");
                    left.appendChild(title);
                    left.appendChild(subtitle);

                    const right = document.createElement("div");
                    right.className = "card-actions";
                    const badge = document.createElement("div");
                    badge.className = "badge";
                    badge.textContent = "";
                    
                    const addBtn = document.createElement("button");
                    addBtn.type = "button";
                    addBtn.className = "track-btn";
                    addBtn.setAttribute("data-card-action", "add-track");
                    addBtn.title = "Add new empty track row";
                    addBtn.textContent = "+ Row";
                    
                    const stripBtn = document.createElement("button");
                    stripBtn.type = "button";
                    stripBtn.className = "track-btn";
                    stripBtn.setAttribute("data-card-action", "strip-name");
                    stripBtn.title = "Remove a name (for example primary artist) from all track labels in this card";
                    stripBtn.textContent = "🧹 Name";
                    
                    const refreshAllBtn = document.createElement("button");
                    refreshAllBtn.type = "button";
                    refreshAllBtn.className = "track-btn";
                    refreshAllBtn.setAttribute("data-card-action", "refresh-all");
                    refreshAllBtn.title = "Refresh all tracks in this card";
                    refreshAllBtn.textContent = "⟳ All";
                    
                    const columnsBtn = document.createElement("button");
                    columnsBtn.type = "button";
                    columnsBtn.className = "track-btn";
                    columnsBtn.setAttribute("data-card-action", "columns");
                    columnsBtn.title = "Toggle column visibility for this card";
                    columnsBtn.textContent = "📊";
                    
                    const delBtn = makeDeleteButton(currentCard);
                    right.appendChild(badge);
                    right.appendChild(addBtn);
                    right.appendChild(stripBtn);
                    right.appendChild(refreshAllBtn);
                    right.appendChild(columnsBtn);
                    right.appendChild(delBtn);

                    header.appendChild(left);
                    header.appendChild(right);
                    currentCard.appendChild(header);

                    currentTable = document.createElement("table");
                    const thead = document.createElement("thead");
                    thead.innerHTML = "<tr>" +
                      "<th class='col-actions'></th>" +
                      "<th class='col-num'>#</th>" +
                      "<th class='col-bpm'>BPM</th>" +
                      "<th class='col-key'>Key</th>" +
                      "<th class='col-pop'>Pop</th>" +
                      "<th class='col-dur'>Dur</th>" +
                      "<th class='col-energy'>En</th>" +
                      "<th class='col-val'>Val</th>" +
                      "<th>Track</th>" +
                      "<th class='col-stars'>★</th>" +
                      "<th class='col-notes'>Notes</th>" +
                      "</tr>";
                    currentTable.appendChild(thead);
                    // Add resize handles to headers
                    thead.querySelectorAll("th").forEach(th => {
                      const handle = document.createElement("div");
                      handle.className = "resize-handle";
                      th.appendChild(handle);
                      handle.addEventListener("mousedown", startResize);
                    });
                    currentTbody = document.createElement("tbody");
                    currentTable.appendChild(currentTbody);
                    currentCard.appendChild(currentTable);
                    results.appendChild(currentCard);
                    currentIndex = 0;
                  }

                  currentIndex += 1;
                  const tr = document.createElement("tr");
                  tr.draggable = true;
                  tr.dataset.spotifyId = "";
                  tr.innerHTML =
                    "<td class='col-actions'>" +
                    "<span class='drag-handle' title='Drag to reorder'>⋮⋮</span>" +
                    "<button type='button' class='track-btn' data-action='refresh' title='Re-query BPM/Key/Pop'>⟳</button>" +
                    "<button type='button' class='track-btn' data-action='clear-api' title='Clear API-fetched data'>⌫</button>" +
                    "<button type='button' class='track-btn' data-action='delete' title='Remove track'>✖</button>" +
                    "</td>" +
                    "<td class='col-num' contenteditable='true'>" + (num || currentIndex) + "</td>" +
                    "<td class='col-bpm' contenteditable='true'" + (bpm ? " data-source='manual'" : "") + ">" + bpm + "</td>" +
                    "<td class='col-key' contenteditable='true'" + (key ? " data-source='manual'" : "") + ">" + key + "</td>" +
                    "<td class='col-pop pop-val' contenteditable='true'" + (pop ? " data-source='manual'" : "") + ">" + pop + "</td>" +
                    "<td class='col-dur' contenteditable='true'" + (dur ? " data-source='manual'" : "") + ">" + dur + "</td>" +
                    "<td class='col-energy' contenteditable='true'" + (energy ? " data-source='manual'" : "") + ">" + energy + "</td>" +
                    "<td class='col-val' contenteditable='true'" + (val ? " data-source='manual'" : "") + ">" + val + "</td>" +
                    "<td class='col-danceability' contenteditable='true'></td>" +
                    "<td class='col-acousticness' contenteditable='true'></td>" +
                    "<td class='col-speechiness' contenteditable='true'></td>" +
                    "<td class='col-genres' contenteditable='true'></td>" +
                    "<td class='col-year' contenteditable='true'></td>" +
                    "<td class='col-canonical' contenteditable='false' title='Database name from Spotify'></td>" +
                    "<td contenteditable='true'><div class='track-label' data-autocomplete='track'>" + escapeHtml(label) + "</div></td>" +
                    "<td class='col-stars'>" + makeStarsCell(stars) + "</td>" +
                    "<td class='col-notes' contenteditable='true'" + (notes ? " data-source='manual'" : "") + ">" + escapeHtml(notes) + "</td>";
                  if (currentTbody) currentTbody.appendChild(tr);
                  return;
                }

                // new header (album title) if not headerSeen
                // Skip if this line is just a metadata word
                const labelUpper = line.toUpperCase().trim();
                if (metadataWords.has(labelUpper) || labelUpper.match(/^(BPM|KEY|POP|DUR|ENERGY|VAL|TRACK|STARS|NOTES)$/i)) {
                  return;
                }
                const cleaned = line.replace(/^#+[\s]*/, "");
                if (!cleaned) return;

                // Parse title and subtitle from "Title - Subtitle" format
                const dashIndex = cleaned.indexOf(" - ");
                let cardTitle = "";
                let cardSubtitle = "";
                
                if (dashIndex !== -1) {
                  cardTitle = cleaned.slice(0, dashIndex).trim();
                  cardSubtitle = cleaned.slice(dashIndex + 3).trim();
                } else {
                  cardTitle = cleaned.trim();
                }

                currentCard = document.createElement("div");
                currentCard.className = "card";
                currentCard.draggable = true;
                currentCard.dataset.defaultStripName = "";

                const header2 = document.createElement("div");
                header2.className = "card-header";

                const left2 = document.createElement("div");
                left2.className = "card-header-main";
                const title2 = document.createElement("div");
                title2.className = "card-title";
                title2.textContent = cardTitle;
                title2.setAttribute("contenteditable", "true");
                const subtitle2 = document.createElement("div");
                subtitle2.className = "card-subtitle";
                subtitle2.textContent = cardSubtitle || "";
                subtitle2.setAttribute("contenteditable", "true");
                left2.appendChild(title2);
                left2.appendChild(subtitle2);

                const right2 = document.createElement("div");
                right2.className = "card-actions";
                const badge2 = document.createElement("div");
                badge2.className = "badge";
                badge2.textContent = "";
                
                const addBtn2 = document.createElement("button");
                addBtn2.type = "button";
                addBtn2.className = "track-btn";
                addBtn2.setAttribute("data-card-action", "add-track");
                addBtn2.title = "Add new empty track row";
                addBtn2.textContent = "+ Row";
                
                const stripBtn2 = document.createElement("button");
                stripBtn2.type = "button";
                stripBtn2.className = "track-btn";
                stripBtn2.setAttribute("data-card-action", "strip-name");
                stripBtn2.title = "Remove a name (for example primary artist) from all track labels in this card";
                stripBtn2.textContent = "🧹 Name";
                
                const refreshAllBtn2 = document.createElement("button");
                refreshAllBtn2.type = "button";
                refreshAllBtn2.className = "track-btn";
                refreshAllBtn2.setAttribute("data-card-action", "refresh-all");
                refreshAllBtn2.title = "Refresh all tracks in this card";
                refreshAllBtn2.textContent = "⟳ All";
                
                const columnsBtn2 = document.createElement("button");
                columnsBtn2.type = "button";
                columnsBtn2.className = "track-btn";
                columnsBtn2.setAttribute("data-card-action", "columns");
                columnsBtn2.title = "Toggle column visibility for this card";
                columnsBtn2.textContent = "📊";
                
                const delBtn2 = makeDeleteButton(currentCard);
                right2.appendChild(badge2);
                right2.appendChild(addBtn2);
                right2.appendChild(stripBtn2);
                right2.appendChild(refreshAllBtn2);
                right2.appendChild(columnsBtn2);
                right2.appendChild(delBtn2);

                header2.appendChild(left2);
                header2.appendChild(right2);
                currentCard.appendChild(header2);

                currentTable = document.createElement("table");
                const thead2 = document.createElement("thead");
                thead2.innerHTML = "<tr>" +
                  "<th class='col-actions'></th>" +
                  "<th class='col-num'>#</th>" +
                  "<th class='col-bpm'>BPM</th>" +
                  "<th class='col-key'>Key</th>" +
                  "<th class='col-pop'>Pop</th>" +
                  "<th class='col-dur'>Dur</th>" +
                  "<th class='col-energy'>En</th>" +
                  "<th class='col-val'>Val</th>" +
                  "<th class='col-danceability'>Dance</th>" +
                  "<th class='col-acousticness'>Acoustic</th>" +
                  "<th class='col-speechiness'>Speech</th>" +
                  "<th class='col-genres'>Genres</th>" +
                  "<th class='col-year'>Year</th>" +
                  "<th class='col-canonical'>Database name</th>" +
                  "<th>Track</th>" +
                  "<th class='col-stars'>★</th>" +
                  "<th class='col-notes'>Notes</th>" +
                  "</tr>";
                currentTable.appendChild(thead2);
                // Add resize handles to headers
                thead2.querySelectorAll("th").forEach(th => {
                  const handle = document.createElement("div");
                  handle.className = "resize-handle";
                  th.appendChild(handle);
                  handle.addEventListener("mousedown", startResize);
                });
                currentTbody = document.createElement("tbody");
                currentTable.appendChild(currentTbody);
                currentCard.appendChild(currentTable);
                results.appendChild(currentCard);
                currentIndex = 0;
              });

              syncColumnVisibility();
              reorderTableColumns();
              
              // Initialize column resize for imported tables
              setTimeout(() => {
                initColumnResize();
                applyColumnWidths();
              }, 50);
              
              // Update badge with track counts for all imported cards
              document.querySelectorAll(".card").forEach(card => {
                const badge = card.querySelector(".badge");
                if (badge) {
                  const tbody = card.querySelector("tbody");
                  if (tbody) {
                    const trackCount = tbody.querySelectorAll("tr").length;
                    if (trackCount > 0) {
                      badge.textContent = trackCount + " track" + (trackCount !== 1 ? "s" : "");
                    }
                  }
                }
              });
              
              status.textContent = "Imported cards from .txt file.";
            }

            // initialise status
            updateStatus();
            syncColumnVisibility();
            // Apply initial column order
            if (typeof reorderTableColumns === 'function') {
              reorderTableColumns();
            }
          </script>
        </body>
        </html>
        """
    )
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------
#  API ENDPOINTS
# ---------------------------------------------------------

@app.route("/api/search_albums", methods=["POST"])
def api_search_albums():
    """Search for albums and return multiple results for selection."""
    try:
        data = request.get_json(force=True)
        artist = (data.get("artist") or "").strip()
        album = (data.get("album") or "").strip()
        
        if not album:
            return jsonify({"error": "Album name required"}), 400
        
        results = search_albums_multiple(artist, album, limit=10)
        
        formatted_results = []
        for album_obj in results:
            artists = ", ".join(a["name"] for a in album_obj.get("artists", []))
            release_date = album_obj.get("release_date", "") or ""
            year = release_date.split("-")[0] if release_date else ""
            formatted_results.append({
                "id": album_obj["id"],
                "name": album_obj["name"],
                "artists": artists,
                "year": year,
                "total_tracks": album_obj.get("total_tracks", 0),
                "popularity": album_obj.get("popularity", 0),
                "spotify_url": album_obj.get("external_urls", {}).get("spotify", ""),
            })
        
        return jsonify({"results": formatted_results, "error": None})
    except Exception as e:
        print("Error in /api/search_albums:", e)
        return jsonify({"results": [], "error": str(e)}), 500


@app.route("/api/search_tracks", methods=["POST"])
def api_search_tracks():
    """Search for tracks and return multiple results for selection."""
    try:
        data = request.get_json(force=True)
        artist = (data.get("artist") or "").strip()
        title = (data.get("title") or "").strip()
        duration_sec = data.get("duration_sec")
        
        if not title:
            return jsonify({"error": "Track title required"}), 400
        
        results = search_tracks_multiple(artist, title, duration_sec, limit=10)
        
        if not results:
            return jsonify({"results": [], "error": None})
        
        # Fetch audio features for all tracks to prioritize those with BPM/Key data
        track_ids = [track_obj["id"] for track_obj, _ in results]
        features_by_id = get_reccobeats_features_for_spotify_ids(track_ids)
        
        # Extract BPM/Key data and calculate consensus scores
        tracks_with_features = []
        bpm_counts = {}
        key_counts = {}
        
        for track_obj, base_score in results:
            tid = track_obj["id"]
            feat = features_by_id.get(tid, {})
            
            tempo = feat.get("tempo")
            key = feat.get("key")
            mode_val = feat.get("mode")
            bpm = None
            camelot = None
            
            if tempo is not None and key is not None and mode_val is not None:
                camelot, _ = key_mode_to_camelot(int(key), int(mode_val))
                bpm = round(tempo)
                
                # Count BPM and Key occurrences for consensus scoring
                if bpm:
                    bpm_counts[bpm] = bpm_counts.get(bpm, 0) + 1
                if camelot:
                    key_counts[camelot] = key_counts.get(camelot, 0) + 1
            
            tracks_with_features.append({
                "track_obj": track_obj,
                "base_score": base_score,
                "bpm": bpm,
                "camelot": camelot,
                "has_features": bpm is not None and camelot is not None
            })
        
        # Calculate enhanced scores with prioritization
        enhanced_results = []
        for item in tracks_with_features:
            enhanced_score = item["base_score"]
            
            # Bonus for having BPM/Key data
            if item["has_features"]:
                enhanced_score += 0.3
            
            # Consensus bonus: if BPM/Key matches other tracks, it's more likely correct
            consensus_bonus = 0.0
            if item["bpm"] and item["camelot"]:
                # Check how many other tracks share the same BPM
                bpm_matches = bpm_counts.get(item["bpm"], 0)
                key_matches = key_counts.get(item["camelot"], 0)
                
                # Bonus increases with more matches (but cap it)
                if bpm_matches > 1:
                    consensus_bonus += min(0.2, (bpm_matches - 1) * 0.05)
                if key_matches > 1:
                    consensus_bonus += min(0.2, (key_matches - 1) * 0.05)
            
            enhanced_score += consensus_bonus
            
            enhanced_results.append({
                "track_obj": item["track_obj"],
                "score": enhanced_score,
                "bpm": item["bpm"],
                "camelot": item["camelot"]
            })
        
        # Sort by enhanced score (descending)
        enhanced_results.sort(key=lambda x: x["score"], reverse=True)
        
        # Format results
        formatted_results = []
        for item in enhanced_results:
            track_obj = item["track_obj"]
            track_artists = ", ".join(a["name"] for a in track_obj.get("artists", []))
            
            duration_ms = track_obj.get("duration_ms")
            duration_str = ""
            if duration_ms:
                total_seconds = round(duration_ms / 1000)
                minutes = total_seconds // 60
                seconds = total_seconds % 60
                duration_str = f"{minutes}:{seconds:02d}"
            
            formatted_results.append({
                "id": track_obj["id"],
                "name": track_obj["name"],
                "artists": track_artists,
                "popularity": track_obj.get("popularity", 0),
                "duration_ms": duration_ms,
                "duration_str": duration_str,
                "bpm": item["bpm"],
                "camelot": item["camelot"],
                "score": round(item["score"], 2),
                "spotify_url": track_obj.get("external_urls", {}).get("spotify", ""),
            })
        
        return jsonify({"results": formatted_results, "error": None})
    except Exception as e:
        print("Error in /api/search_tracks:", e)
        return jsonify({"results": [], "error": str(e)}), 500


@app.route("/api/process", methods=["POST"])
def api_process():
    try:
        data = request.get_json(force=True)
        mode = data.get("mode", "albums")
        raw = data.get("raw", "")
        # Support both raw text and selected album IDs
        selected_album_ids = data.get("selected_album_ids", {})  # dict: line_index -> album_id
        
        cleaned_lines = clean_and_normalize(raw)
        cleaned_lines = [ln for ln in cleaned_lines if ln.strip()]

        if not cleaned_lines:
            return jsonify({"mode": mode, "items": [], "error": None})

        if mode == "albums":
            items = []
            # Parse lines with artist inference
            parsed_lines = parse_lines_with_artist_inference(cleaned_lines)
            for idx, (artist, album_title, _) in enumerate(parsed_lines):
                
                # Check if a specific album was selected for this line
                album_obj = None
                if str(idx) in selected_album_ids:
                    album_id = selected_album_ids[str(idx)]
                    try:
                        album_obj = sp.album(album_id)
                    except Exception as e:
                        print(f"Error fetching album {album_id}:", e)
                
                # Fallback to search if no selection or selection failed
                if not album_obj:
                    album_obj = search_album(artist, album_title)
                
                if not album_obj:
                    continue

                album_id = album_obj["id"]
                album_name = album_obj["name"]
                artists = ", ".join(a["name"] for a in album_obj.get("artists", []))
                release_date = album_obj.get("release_date", "") or ""
                year = release_date.split("-")[0] if release_date else ""
                album_url = album_obj.get("external_urls", {}).get("spotify", "")

                # get album tracks
                tracks = []
                results_sp = sp.album_tracks(album_id, limit=50)
                tracks.extend(results_sp.get("items", []))
                while results_sp.get("next"):
                    results_sp = sp.next(results_sp)
                    tracks.extend(results_sp.get("items", []))

                track_ids = [t.get("id") for t in tracks if t.get("id")]
                full_by_id: Dict[str, dict] = {}
                for chunk in chunk_list(track_ids, 50):
                    res = sp.tracks(chunk)
                    for t in res.get("tracks", []):
                        if t and t.get("id"):
                            full_by_id[t["id"]] = t

                features_by_id = get_reccobeats_features_for_spotify_ids(track_ids)

                track_list = []
                for t in tracks:
                    tid = t.get("id")
                    if not tid:
                        continue
                    feat = features_by_id.get(tid, {})
                    full = full_by_id.get(tid, t)

                    tempo = feat.get("tempo")
                    key = feat.get("key")
                    mode_val = feat.get("mode")

                    if tempo is not None and key is not None and mode_val is not None:
                        camelot, key_name = key_mode_to_camelot(int(key), int(mode_val))
                        bpm = round(tempo)
                    else:
                        camelot, key_name, bpm = None, None, None

                    popularity = full.get("popularity")
                    track_artists = ", ".join(a["name"] for a in full.get("artists", []))
                    track_name = t.get("name", "")
                    spotify_url = full.get("external_urls", {}).get("spotify", "")
                    duration_ms = full.get("duration_ms")
                    energy = feat.get("energy")
                    valence = feat.get("valence")
                    
                    # New audio features
                    danceability = feat.get("danceability")
                    acousticness = feat.get("acousticness")
                    speechiness = feat.get("speechiness")
                    
                    # Get genres from artists
                    all_genres = []
                    for artist in full.get("artists", []):
                        try:
                            artist_obj = sp.artist(artist["id"])
                            all_genres.extend(artist_obj.get("genres", []))
                        except:
                            pass
                    from collections import Counter
                    genre_counts = Counter(all_genres)
                    top_genres = [g for g, _ in genre_counts.most_common(2)]
                    genres_str = ", ".join(top_genres) if top_genres else ""

                    track_list.append({
                        "id": tid,
                        "disc": t.get("disc_number", 1),
                        "track_number": t.get("track_number", 0),
                        "name": track_name,
                        "artists": track_artists,
                        "bpm": bpm,
                        "camelot": camelot,
                        "key_name": key_name,
                        "popularity": popularity,
                        "spotify_url": spotify_url,
                        "duration_ms": duration_ms,
                        "energy": energy,
                        "valence": valence,
                        "danceability": danceability,
                        "acousticness": acousticness,
                        "speechiness": speechiness,
                        "genres": genres_str,
                        "release_year": year,
                        "stars": None,
                        "notes": "",
                    })

                track_list.sort(key=lambda x: (x["disc"], x["track_number"]))

                artist_set = {t["artists"].strip() for t in track_list if t.get("artists")}
                single_artist = artist_set.pop() if len(artist_set) == 1 else ""

                items.append({
                    "title": f"{album_name}",
                    "subtitle": f"{artists} • {year}" if year else artists,
                    "year": year,
                    "total_tracks": len(track_list),
                    "spotify_url": album_url,
                    "tracks": track_list,
                    "single_artist": single_artist,
                    "artists": artists,
                })

            return jsonify({"mode": "albums", "items": items, "error": None})

        else:
            # tracks mode
            track_entries = []
            # Parse lines with artist inference
            parsed_lines = parse_lines_with_artist_inference(cleaned_lines)
            
            # First, get all search results
            all_search_results = []
            for artist, title, dur in parsed_lines:
                results = search_tracks_multiple(artist, title, dur, limit=10)
                if results:
                    all_search_results.append((artist, title, dur, results))
            
            if not all_search_results:
                return jsonify({"mode": "tracks", "items": [], "error": None})
            
            # Collect all track IDs from all search results
            all_track_ids = []
            for artist, title, dur, results in all_search_results:
                all_track_ids.extend([track_obj["id"] for track_obj, _ in results])
            
            # Fetch features for all tracks to prioritize those with BPM/Key data
            all_features_by_id = get_reccobeats_features_for_spotify_ids(all_track_ids)
            
            # Prioritize tracks with BPM/Key data for each search
            for artist, title, dur, results in all_search_results:
                # Extract BPM/Key data and calculate consensus scores
                tracks_with_features = []
                bpm_counts = {}
                key_counts = {}
                
                for track_obj, base_score in results:
                    tid = track_obj["id"]
                    feat = all_features_by_id.get(tid, {})
                    
                    tempo = feat.get("tempo")
                    key = feat.get("key")
                    mode_val = feat.get("mode")
                    bpm = None
                    camelot = None
                    
                    if tempo is not None and key is not None and mode_val is not None:
                        camelot, _ = key_mode_to_camelot(int(key), int(mode_val))
                        bpm = round(tempo)
                        
                        # Count BPM and Key occurrences for consensus scoring
                        if bpm:
                            bpm_counts[bpm] = bpm_counts.get(bpm, 0) + 1
                        if camelot:
                            key_counts[camelot] = key_counts.get(camelot, 0) + 1
                    
                    tracks_with_features.append({
                        "track_obj": track_obj,
                        "base_score": base_score,
                        "bpm": bpm,
                        "camelot": camelot,
                        "has_features": bpm is not None and camelot is not None
                    })
                
                # Calculate enhanced scores with prioritization
                enhanced_results = []
                for item in tracks_with_features:
                    enhanced_score = item["base_score"]
                    
                    # Bonus for having BPM/Key data
                    if item["has_features"]:
                        enhanced_score += 0.3
                    
                    # Consensus bonus: if BPM/Key matches other tracks, it's more likely correct
                    consensus_bonus = 0.0
                    if item["bpm"] and item["camelot"]:
                        bpm_matches = bpm_counts.get(item["bpm"], 0)
                        key_matches = key_counts.get(item["camelot"], 0)
                        
                        if bpm_matches > 1:
                            consensus_bonus += min(0.2, (bpm_matches - 1) * 0.05)
                        if key_matches > 1:
                            consensus_bonus += min(0.2, (key_matches - 1) * 0.05)
                    
                    enhanced_score += consensus_bonus
                    enhanced_results.append({
                        "track_obj": item["track_obj"],
                        "score": enhanced_score
                    })
                
                # Sort by enhanced score and pick the best one
                enhanced_results.sort(key=lambda x: x["score"], reverse=True)
                if enhanced_results:
                    best_track = enhanced_results[0]["track_obj"]
                    track_entries.append((artist, title, dur, best_track))

            if not track_entries:
                return jsonify({"mode": "tracks", "items": [], "error": None})

            track_ids = [t[3]["id"] for t in track_entries if t[3].get("id")]

            full_by_id: Dict[str, dict] = {}
            for chunk in chunk_list(track_ids, 50):
                res = sp.tracks(chunk)
                for t in res.get("tracks", []):
                    if t and t.get("id"):
                        full_by_id[t["id"]] = t

            # Re-fetch features for the selected tracks (we already have some, but ensure we have all)
            features_by_id = get_reccobeats_features_for_spotify_ids(track_ids)

            items = []
            for idx, (_req_artist, _req_title, _req_dur, t) in enumerate(track_entries, start=1):
                tid = t.get("id")
                if not tid:
                    continue
                full = full_by_id.get(tid, t)
                feat = features_by_id.get(tid, {})

                tempo = feat.get("tempo")
                key = feat.get("key")
                mode_val = feat.get("mode")

                if tempo is not None and key is not None and mode_val is not None:
                    camelot, key_name = key_mode_to_camelot(int(key), int(mode_val))
                    bpm = round(tempo)
                else:
                    camelot, key_name, bpm = None, None, None

                popularity = full.get("popularity")
                track_artists = ", ".join(a["name"] for a in full.get("artists", []))
                track_name = full.get("name", "")
                spotify_url = full.get("external_urls", {}).get("spotify", "")
                duration_ms = full.get("duration_ms")
                energy = feat.get("energy")
                valence = feat.get("valence")
                
                # New audio features
                danceability = feat.get("danceability")
                acousticness = feat.get("acousticness")
                speechiness = feat.get("speechiness")
                
                # Get genres from artists
                all_genres = []
                for artist in full.get("artists", []):
                    try:
                        artist_obj = sp.artist(artist["id"])
                        all_genres.extend(artist_obj.get("genres", []))
                    except:
                        pass
                from collections import Counter
                genre_counts = Counter(all_genres)
                top_genres = [g for g, _ in genre_counts.most_common(2)]
                genres_str = ", ".join(top_genres) if top_genres else ""
                
                # Get release year from album
                release_year = ""
                album_id = full.get("album", {}).get("id") if isinstance(full.get("album"), dict) else None
                if album_id:
                    try:
                        album_obj = sp.album(album_id)
                        release_date = album_obj.get("release_date", "") or ""
                        release_year = release_date.split("-")[0] if release_date else ""
                    except:
                        pass
                if not release_year:
                    album_data = full.get("album", {})
                    if isinstance(album_data, dict):
                        release_date = album_data.get("release_date", "") or ""
                        release_year = release_date.split("-")[0] if release_date else ""

                items.append({
                    "id": tid,
                    "index": idx,
                    "name": track_name,
                    "artists": track_artists,
                    "bpm": bpm,
                    "camelot": camelot,
                    "key_name": key_name,
                    "popularity": popularity,
                    "spotify_url": spotify_url,
                    "duration_ms": duration_ms,
                    "energy": energy,
                    "valence": valence,
                    "danceability": danceability,
                    "acousticness": acousticness,
                    "speechiness": speechiness,
                    "genres": genres_str,
                    "release_year": release_year,
                    "stars": None,
                    "notes": "",
                })

            return jsonify({"mode": "tracks", "items": items, "error": None})

    except Exception as e:
        print("Unexpected error in /api/process:", e)
        return jsonify({"mode": None, "items": [], "error": str(e)}), 500


@app.route("/api/refresh_track", methods=["POST"])
def refresh_track():
    """
    Re-query a single track by spotify_id (preferred) or by artists + title.
    If search_mode=true, returns multiple results for selection.
    Otherwise returns bpm, camelot, popularity, spotify_id, artists, name.
    """
    try:
        data = request.get_json(force=True)
        spotify_id = data.get("spotify_id") or None
        artists = (data.get("artists") or "").strip()
        title = (data.get("title") or "").strip()
        search_mode = data.get("search_mode", False)  # If true, return multiple results
        selected_track_id = data.get("selected_track_id")  # If provided, use this specific track

        # If a specific track ID was selected, use it
        if selected_track_id:
            try:
                full = sp.track(selected_track_id)
                tid = selected_track_id
            except Exception as e:
                print("Error fetching selected track:", e)
                return jsonify({"error": f"Could not fetch track {selected_track_id}"}), 404
        elif spotify_id:
            try:
                full = sp.track(spotify_id)
                tid = spotify_id
            except Exception as e:
                print("Error fetching track by spotify_id, will fallback to search:", e)
                full = None
                tid = None
        else:
            full = None
            tid = None

        # If search_mode, return multiple results
        if search_mode and not selected_track_id:
            if not title:
                return jsonify({"error": "Missing title for search"}), 400
            results = search_tracks_multiple(artists, title, None, limit=10)
            
            if not results:
                return jsonify({"results": [], "error": None})
            
            # Get features for all tracks to prioritize those with BPM/Key data
            track_ids = [track_obj["id"] for track_obj, _ in results]
            features_by_id = get_reccobeats_features_for_spotify_ids(track_ids)
            
            # Extract BPM/Key data and calculate consensus scores
            tracks_with_features = []
            bpm_counts = {}
            key_counts = {}
            
            for track_obj, base_score in results:
                tid = track_obj["id"]
                feat = features_by_id.get(tid, {})
                
                tempo = feat.get("tempo")
                key = feat.get("key")
                mode_val = feat.get("mode")
                bpm = None
                camelot = None
                
                if tempo is not None and key is not None and mode_val is not None:
                    camelot, _ = key_mode_to_camelot(int(key), int(mode_val))
                    bpm = round(tempo)
                    
                    # Count BPM and Key occurrences for consensus scoring
                    if bpm:
                        bpm_counts[bpm] = bpm_counts.get(bpm, 0) + 1
                    if camelot:
                        key_counts[camelot] = key_counts.get(camelot, 0) + 1
                
                tracks_with_features.append({
                    "track_obj": track_obj,
                    "base_score": base_score,
                    "bpm": bpm,
                    "camelot": camelot,
                    "has_features": bpm is not None and camelot is not None
                })
            
            # Calculate enhanced scores with prioritization
            enhanced_results = []
            for item in tracks_with_features:
                enhanced_score = item["base_score"]
                
                # Bonus for having BPM/Key data
                if item["has_features"]:
                    enhanced_score += 0.3
                
                # Consensus bonus: if BPM/Key matches other tracks, it's more likely correct
                consensus_bonus = 0.0
                if item["bpm"] and item["camelot"]:
                    # Check how many other tracks share the same BPM
                    bpm_matches = bpm_counts.get(item["bpm"], 0)
                    key_matches = key_counts.get(item["camelot"], 0)
                    
                    # Bonus increases with more matches (but cap it)
                    if bpm_matches > 1:
                        consensus_bonus += min(0.2, (bpm_matches - 1) * 0.05)
                    if key_matches > 1:
                        consensus_bonus += min(0.2, (key_matches - 1) * 0.05)
                
                enhanced_score += consensus_bonus
                
                enhanced_results.append({
                    "track_obj": item["track_obj"],
                    "score": enhanced_score,
                    "bpm": item["bpm"],
                    "camelot": item["camelot"]
                })
            
            # Sort by enhanced score (descending)
            enhanced_results.sort(key=lambda x: x["score"], reverse=True)
            
            # Format enhanced results (already sorted by score)
            formatted_results = []
            for enhanced_item in enhanced_results:
                track_obj = enhanced_item["track_obj"]
                tid = track_obj["id"]
                track_artists = ", ".join(a["name"] for a in track_obj.get("artists", []))
                
                duration_ms = track_obj.get("duration_ms")
                duration_str = ""
                if duration_ms:
                    total_seconds = round(duration_ms / 1000)
                    minutes = total_seconds // 60
                    seconds = total_seconds % 60
                    duration_str = f"{minutes}:{seconds:02d}"
                
                formatted_results.append({
                    "id": tid,
                    "name": track_obj["name"],
                    "artists": track_artists,
                    "popularity": track_obj.get("popularity", 0),
                    "duration_ms": duration_ms,
                    "duration_str": duration_str,
                    "bpm": enhanced_item["bpm"],
                    "camelot": enhanced_item["camelot"],
                    "score": round(enhanced_item["score"], 2),
                    "spotify_url": track_obj.get("external_urls", {}).get("spotify", ""),
                })
            
            return jsonify({"results": formatted_results, "error": None})

        # Otherwise, return single result (existing behavior)
        if full is None:
            if not title:
                return jsonify({"error": "Missing title for search"}), 400
            track_obj = search_track(artists, title, None)
            if not track_obj:
                return jsonify({"error": "Track not found in search"}), 404
            tid = track_obj["id"]
            full = track_obj

        if not tid:
            return jsonify({"error": "No track id resolved"}), 500

        # get features from ReccoBeats
        features_by_id = get_reccobeats_features_for_spotify_ids([tid])
        feat = features_by_id.get(tid, {})

        tempo = feat.get("tempo")
        key = feat.get("key")
        mode_val = feat.get("mode")

        if tempo is not None and key is not None and mode_val is not None:
            camelot, key_name = key_mode_to_camelot(int(key), int(mode_val))
            bpm = round(tempo)
        else:
            camelot, key_name, bpm = None, None, None

        popularity = full.get("popularity")
        track_name = full.get("name", "")
        track_artists = ", ".join(a["name"] for a in full.get("artists", []))
        duration_ms = full.get("duration_ms")
        energy = feat.get("energy")
        valence = feat.get("valence")
        
        # New audio features from ReccoBeats
        danceability = feat.get("danceability")
        acousticness = feat.get("acousticness")
        liveness = feat.get("liveness")
        speechiness = feat.get("speechiness")
        
        # Get genres from artists (most common genres across all artists)
        all_genres = []
        for artist in full.get("artists", []):
            try:
                artist_obj = sp.artist(artist["id"])
                all_genres.extend(artist_obj.get("genres", []))
            except:
                pass
        
        # Get most common genres (limit to 2)
        from collections import Counter
        genre_counts = Counter(all_genres)
        top_genres = [g for g, _ in genre_counts.most_common(2)]
        genres_str = ", ".join(top_genres) if top_genres else ""
        
        # Get release year from album
        release_year = ""
        album_id = full.get("album", {}).get("id") if isinstance(full.get("album"), dict) else None
        if album_id:
            try:
                album_obj = sp.album(album_id)
                release_date = album_obj.get("release_date", "") or ""
                release_year = release_date.split("-")[0] if release_date else ""
            except:
                pass
        
        # If no year from album, try from track's album field
        if not release_year:
            album_data = full.get("album", {})
            if isinstance(album_data, dict):
                release_date = album_data.get("release_date", "") or ""
                release_year = release_date.split("-")[0] if release_date else ""

        return jsonify({
            "spotify_id": tid,
            "bpm": bpm,
            "camelot": camelot,
            "key_name": key_name,
            "popularity": popularity,
            "name": track_name,
            "artists": track_artists,
            "duration_ms": duration_ms,
            "energy": energy,
            "valence": valence,
            "danceability": danceability,
            "acousticness": acousticness,
            "liveness": liveness,
            "speechiness": speechiness,
            "genres": genres_str,
            "release_year": release_year,
            "error": None,
        })

    except Exception as e:
        print("Unexpected error in /api/refresh_track:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
