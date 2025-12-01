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

SPOTIFY_CLIENT_ID = "fe3625e239524a37b7d2b319d3019c5d"
SPOTIFY_CLIENT_SECRET = "e330b5ce2f524f9ba8eccf6546388000"

if SPOTIFY_CLIENT_ID.startswith("YOUR_") or SPOTIFY_CLIENT_SECRET.startswith("YOUR_"):
    print("WARNING: You must set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET at the top of app.py")

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

def parse_artist_title_duration(line: str) -> Tuple[str, str, Optional[int]]:
    """
    Returns (artist, title, duration_seconds or None).

    Handles:
      'Stan Getz, Charlie Byrd - Desafinado 1:59'
      'João Gilberto – Bim Bom'
      'Sérgio Mendes– Oba-La-La 2:26'
    """
    import re

    line = normalize_separators(line)

    # split artist vs title part
    artist = ""
    title_part = line.strip()

    if " - " in line:
        parts = line.split(" - ", 1)
        artist = parts[0].strip()
        title_part = parts[1].strip()
    elif "-" in line:
        parts = line.split("-", 1)
        artist = parts[0].strip()
        title_part = parts[1].strip()
    elif ":" in line:
        parts = line.split(":", 1)
        artist = parts[0].strip()
        title_part = parts[1].strip()

    # Discogs-style artist asterisk
    artist = artist.rstrip("*").strip()

    # strip trailing duration "m:ss"
    duration_sec: Optional[int] = None
    m = re.search(r"(\d{1,2}):(\d{2})\s*$", title_part)
    if m:
        mins = int(m.group(1))
        secs = int(m.group(2))
        duration_sec = mins * 60 + secs
        title_part = title_part[: m.start()].rstrip()

    title = title_part.strip()

    if not artist:
        # no clear artist -> whole line treated as title
        return "", title, duration_sec

    return artist, title, duration_sec


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
    parts = [p.strip() for p in re_split_multi(requested, [",", "&", "feat.", "ft.", "featuring"]) if p.strip()]
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
              grid-template-columns: repeat(var(--cols, 2), minmax(0, 1fr));
              gap: 10px;
              page-break-inside: avoid;
            }
            .cards[data-cols="1"] { --cols: 1; }
            .cards[data-cols="2"] { --cols: 2; }
            .cards[data-cols="4"] { --cols: 2; }
            .cards[data-cols="8"] { --cols: 4; }
            .cards[data-cols="16"] { --cols: 4; }
            .card {
              border-radius: 10px;
              background: #111;
              border: 1px solid #2b2b2b;
              padding: 10px 10px 8px;
              display: flex;
              flex-direction: column;
              gap: 6px;
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
            }
            th, td {
              padding: 2px 4px;
              text-align: left;
            }
            th {
              color: var(--muted);
              font-weight: 500;
              border-bottom: 1px solid #222;
            }
            td {
              border-bottom: none;
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
            .col-actions {
              width: 80px;
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
              .col-actions {
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
                Paste album or track lists. This tool cleans them, finds BPM and keys via ReccoBeats
                and lays out printable slips for your records.
              </div>
              <h2>1. Paste album or track list</h2>
              <textarea id="input" placeholder="Example:
Moodymann - Mahogany Brown
A2 Stan Getz, Charlie Byrd - Desafinado 1:59
B1 Quincy Jones - Soul Bossa Nova 2:43"></textarea>
              <div class="row">
                <div class="mode-toggle" id="modeToggle">
                  <button class="mode-btn active" data-mode="albums">Albums</button>
                  <button class="mode-btn" data-mode="tracks">Tracks</button>
                </div>
                <div class="select-group">
                  <span>Print layout</span>
                  <select id="colsSelect">
                    <option value="1">1 card / page</option>
                    <option value="2">2 cards / page</option>
                    <option value="4" selected>4 cards / page</option>
                    <option value="8">8 cards / page</option>
                    <option value="16">16 cards / page</option>
                  </select>
                </div>
                <button class="btn" id="runBtn">⚙️ Generate sheets</button>
                <button class="btn btn-outline" id="clearBtn">✖ Clear cards</button>
                <button class="btn btn-outline" id="printBtn">🖨 Print</button>
                <button class="btn btn-outline" id="downloadBtn">⬇ Download .txt</button>
              </div>
              <div class="status" id="status">
                Mode: Albums. Layout: 4 cards per page. Cards from each run will be added below.
              </div>
            </div>

            <div class="panel">
              <h2>2. Print slips</h2>
              <div id="results" class="cards" data-cols="4"></div>
            </div>
          </div>

          <script>
            const modeToggle = document.getElementById("modeToggle");
            const runBtn = document.getElementById("runBtn");
            const clearBtn = document.getElementById("clearBtn");
            const printBtn = document.getElementElementById?.("printBtn") || document.getElementById("printBtn");
            const downloadBtn = document.getElementById("downloadBtn");
            const inputEl = document.getElementById("input");
            const status = document.getElementById("status");
            const results = document.getElementById("results");
            const colsSelect = document.getElementById("colsSelect");

            let currentMode = "albums";

            modeToggle.addEventListener("click", (e) => {
              const btn = e.target.closest(".mode-btn");
              if (!btn) return;
              const mode = btn.getAttribute("data-mode");
              currentMode = mode;
              document.querySelectorAll(".mode-btn").forEach(b => b.classList.remove("active"));
              btn.classList.add("active");
              updateStatus();
            });

            colsSelect.addEventListener("change", () => {
              const val = colsSelect.value;
              results.setAttribute("data-cols", val);
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

            // Card- and row-level click handling
            results.addEventListener("click", (e) => {
              // Card level strip name
              const cardStrip = e.target.closest("[data-card-action='strip-name']");
              if (cardStrip) {
                const card = cardStrip.closest(".card");
                if (card) {
                  stripNameFromCard(card);
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

              const trackBtn = e.target.closest(".track-btn");
              if (!trackBtn) return;
              const action = trackBtn.getAttribute("data-action");
              const tr = trackBtn.closest("tr");
              if (!tr) return;

              if (action === "delete") {
                tr.remove();
                updateStatus("Track removed.");
              } else if (action === "up") {
                const tbody = tr.parentElement;
                const prev = tr.previousElementSibling;
                if (tbody && prev) {
                  tbody.insertBefore(tr, prev);
                }
              } else if (action === "down") {
                const tbody = tr.parentElement;
                const next = tr.nextElementSibling;
                if (tbody && next) {
                  tbody.insertBefore(tr, next.nextSibling);
                }
              } else if (action === "refresh") {
                refreshRow(tr);
              }
            });

            function updateStatus(extra) {
              const cols = colsSelect.value;
              const modeLabel = currentMode === "albums" ? "Albums" : "Tracks";
              let base = `Mode: ${modeLabel}. Layout: ${cols} cards per page. Cards from each run will be added below.`;
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
                const res = await fetch("/api/process", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ mode: currentMode, raw }),
                });
                const data = await res.json();
                if (!res.ok) {
                  status.textContent = "Backend error: " + (data.error || res.statusText);
                  runBtn.disabled = false;
                  return;
                }
                renderResults(data);
                updateStatus("Done. You can edit inline, delete tracks, reorder, re-query tracks, print, or download as text.");
              } catch (err) {
                console.error(err);
                status.textContent = "Network error talking to backend.";
              } finally {
                runBtn.disabled = false;
              }
            }

            function escapeHtml(str) {
              return str
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
                const labelCell = row.querySelector("td:nth-child(5)");
                if (!labelCell) return;
                const labelDiv = labelCell.querySelector(".track-label") || labelCell;
                let text = labelDiv.textContent || "";
                if (!text) return;

                // remove the name anywhere
                const re = new RegExp(escapeRegExp(target), "gi");
                text = text.replace(re, "");

                // clean up stray " - " and extra spaces
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

            function renderResults(data) {
              const cols = colsSelect.value;
              results.setAttribute("data-cols", cols);

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

                  const stripBtn = document.createElement("button");
                  stripBtn.type = "button";
                  stripBtn.className = "track-btn";
                  stripBtn.setAttribute("data-card-action", "strip-name");
                  stripBtn.title = "Remove a name (for example primary artist) from all track labels in this card";
                  stripBtn.textContent = "🧹 Strip name";

                  const delBtn = makeDeleteButton(card);
                  right.appendChild(badge);
                  right.appendChild(stripBtn);
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
                    "<th>Track</th>" +
                    "<th class='col-actions'></th>" +
                    "</tr>";
                  table.appendChild(thead);

                  const tbody = document.createElement("tbody");
                  const singleArtist = (album.single_artist || "").trim();
                  (album.tracks || []).forEach(t => {
                    const tr = document.createElement("tr");
                    const bpm = t.bpm != null ? t.bpm : "";
                    const camelot = t.camelot || "";
                    const pop = t.popularity != null ? t.popularity : "";
                    let label = t.name || "";

                    if (!singleArtist && t.artists && t.artists.trim()) {
                      label = t.artists + " - " + label;
                    }

                    tr.dataset.spotifyId = t.id || "";

                    tr.innerHTML =
                      "<td class='col-num' contenteditable='true'>" + (t.track_number || "") + "</td>" +
                      "<td class='col-bpm' contenteditable='true'>" + bpm + "</td>" +
                      "<td class='col-key' contenteditable='true'>" + camelot + "</td>" +
                      "<td class='col-pop pop-val' contenteditable='true'>" + pop + "</td>" +
                      "<td contenteditable='true'><div class='track-label'>" + escapeHtml(label) + "</div></td>" +
                      "<td class='col-actions'>" +
                      "<button type='button' class='track-btn' data-action='refresh' title='Re-query BPM/Key/Pop'>⟳</button>" +
                      "<button type='button' class='track-btn' data-action='up' title='Move up'>↑</button>" +
                      "<button type='button' class='track-btn' data-action='down' title='Move down'>↓</button>" +
                      "<button type='button' class='track-btn' data-action='delete' title='Remove track'>✖</button>" +
                      "</td>";
                    tbody.appendChild(tr);
                  });
                  table.appendChild(tbody);
                  card.appendChild(table);
                  results.appendChild(card);
                });
              } else {
                // tracks mode - one big card with all tracks
                const card = document.createElement("div");
                card.className = "card";
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

                const stripBtn = document.createElement("button");
                stripBtn.type = "button";
                stripBtn.className = "track-btn";
                stripBtn.setAttribute("data-card-action", "strip-name");
                stripBtn.title = "Remove a name from all track labels in this card";
                stripBtn.textContent = "🧹 Strip name";

                const delBtn = makeDeleteButton(card);
                right.appendChild(badge);
                right.appendChild(stripBtn);
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
                  "<th>Track</th>" +
                  "<th class='col-actions'></th>" +
                  "</tr>";
                table.appendChild(thead);
                const tbody = document.createElement("tbody");
                (data.items || []).forEach((t, idx) => {
                  const bpm = t.bpm != null ? t.bpm : "";
                  const camelot = t.camelot || "";
                  const pop = t.popularity != null ? t.popularity : "";
                  let label = t.name || "";
                  if (t.artists && t.artists.trim()) {
                    label = t.artists + " - " + label;
                  }
                  const tr = document.createElement("tr");
                  tr.dataset.spotifyId = t.id || "";
                  tr.innerHTML =
                    "<td class='col-num' contenteditable='true'>" + (idx + 1) + "</td>" +
                    "<td class='col-bpm' contenteditable='true'>" + bpm + "</td>" +
                    "<td class='col-key' contenteditable='true'>" + camelot + "</td>" +
                    "<td class='col-pop pop-val' contenteditable='true'>" + pop + "</td>" +
                    "<td contenteditable='true'><div class='track-label'>" + escapeHtml(label) + "</div></td>" +
                    "<td class='col-actions'>" +
                    "<button type='button' class='track-btn' data-action='refresh' title='Re-query BPM/Key/Pop'>⟳</button>" +
                    "<button type='button' class='track-btn' data-action='up' title='Move up'>↑</button>" +
                    "<button type='button' class='track-btn' data-action='down' title='Move down'>↓</button>" +
                    "<button type='button' class='track-btn' data-action='delete' title='Remove track'>✖</button>" +
                    "</td>";
                  tbody.appendChild(tr);
                });
                table.appendChild(tbody);
                card.appendChild(table);
                results.appendChild(card);
              }
            }

            async function refreshRow(tr) {
              const tds = tr.querySelectorAll("td");
              if (tds.length < 5) return;
              const bpmCell = tds[1];
              const keyCell = tds[2];
              const popCell = tds[3];
              const labelCell = tds[4];
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
              }

              const spotifyId = tr.dataset.spotifyId || "";

              status.textContent = "Refreshing track audio features...";

              try {
                const res = await fetch("/api/refresh_track", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    spotify_id: spotifyId || null,
                    artists,
                    title
                  }),
                });
                const data = await res.json();
                if (!res.ok || data.error) {
                  status.textContent = "Refresh failed: " + (data.error || res.statusText);
                  return;
                }
                bpmCell.textContent = data.bpm != null ? data.bpm : "";
                keyCell.textContent = data.camelot || "";
                popCell.textContent = data.popularity != null ? data.popularity : "";

                // update label to canonical "artists - title"
                let newLabel = data.name || title;
                if (data.artists) {
                  newLabel = data.artists + " - " + newLabel;
                }
                labelCell.innerHTML = "<div class='track-label'>" + escapeHtml(newLabel) + "</div>";

                if (data.spotify_id) {
                  tr.dataset.spotifyId = data.spotify_id;
                }
                status.textContent = "Track updated from database.";
              } catch (err) {
                console.error(err);
                status.textContent = "Network error refreshing track.";
              }
            }

            function downloadTxt() {
              const cards = document.querySelectorAll(".card");
              if (!cards.length) {
                status.textContent = "Nothing to export. Generate some cards first.";
                return;
              }
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
                  lines.push("#\\tBPM\\tKey\\tPop\\tTrack");
                }
                const rows = card.querySelectorAll("tbody tr");
                rows.forEach(row => {
                  const cols = row.querySelectorAll("td");
                  if (!cols.length) return;
                  const num = (cols[0].textContent || "").trim();
                  const bpm = (cols[1].textContent || "").trim();
                  const key = (cols[2].textContent || "").trim();
                  const pop = (cols[3].textContent || "").trim();
                  const labelCell = cols[4];
                  const labelDiv = labelCell.querySelector(".track-label") || labelCell;
                  const label = (labelDiv.textContent || "").trim();
                  if (!label) return;
                  lines.push([num, bpm, key, pop, label].join("\\t"));
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

            // initialise status
            updateStatus();
          </script>
        </body>
        </html>
        """
    )
    return Response(html, mimetype="text/html")


# ---------------------------------------------------------
#  API ENDPOINTS
# ---------------------------------------------------------

@app.route("/api/process", methods=["POST"])
def api_process():
    try:
        data = request.get_json(force=True)
        mode = data.get("mode", "albums")
        raw = data.get("raw", "")
        cleaned_lines = clean_and_normalize(raw)
        cleaned_lines = [ln for ln in cleaned_lines if ln.strip()]

        if not cleaned_lines:
            return jsonify({"mode": mode, "items": [], "error": None})

        if mode == "albums":
            items = []
            for line in cleaned_lines:
                artist, album_title, _ = parse_artist_title_duration(line)
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
            for line in cleaned_lines:
                artist, title, dur = parse_artist_title_duration(line)
                track_obj = search_track(artist, title, dur)
                if not track_obj:
                    continue
                track_entries.append((artist, title, dur, track_obj))

            if not track_entries:
                return jsonify({"mode": "tracks", "items": [], "error": None})

            track_ids = [t[3]["id"] for t in track_entries if t[3].get("id")]

            full_by_id: Dict[str, dict] = {}
            for chunk in chunk_list(track_ids, 50):
                res = sp.tracks(chunk)
                for t in res.get("tracks", []):
                    if t and t.get("id"):
                        full_by_id[t["id"]] = t

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
                })

            return jsonify({"mode": "tracks", "items": items, "error": None})

    except Exception as e:
        print("Unexpected error in /api/process:", e)
        return jsonify({"mode": None, "items": [], "error": str(e)}), 500


@app.route("/api/refresh_track", methods=["POST"])
def refresh_track():
    """
    Re-query a single track by spotify_id (preferred) or by artists + title.
    Returns bpm, camelot, popularity, spotify_id, artists, name.
    """
    try:
        data = request.get_json(force=True)
        spotify_id = data.get("spotify_id") or None
        artists = (data.get("artists") or "").strip()
        title = (data.get("title") or "").strip()

        full = None
        tid = None

        if spotify_id:
            try:
                full = sp.track(spotify_id)
                tid = spotify_id
            except Exception as e:
                print("Error fetching track by spotify_id, will fallback to search:", e)

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

        return jsonify({
            "spotify_id": tid,
            "bpm": bpm,
            "camelot": camelot,
            "key_name": key_name,
            "popularity": popularity,
            "name": track_name,
            "artists": track_artists,
            "error": None,
        })

    except Exception as e:
        print("Unexpected error in /api/refresh_track:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
