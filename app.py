import os, re, logging, requests, sqlite3, json
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
import myjdapi

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

RD_API_KEY = os.environ.get("RD_API_KEY", "")
MYJ_EMAIL = os.environ.get("MYJ_EMAIL", "")
MYJ_PASSWORD = os.environ.get("MYJ_PASSWORD", "")
MYJ_DEVICE = os.environ.get("MYJ_DEVICE", "JDownloader@Docker")
MAX_SIZE_GB_MOVIE = float(os.environ.get("MAX_SIZE_GB_MOVIE", "30"))
MAX_SIZE_GB_TV = float(os.environ.get("MAX_SIZE_GB_TV", "6"))
MIN_QUALITY_MOVIE = os.environ.get("MIN_QUALITY_MOVIE", "2160p")
MIN_QUALITY_TV = os.environ.get("MIN_QUALITY_TV", "1080p")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/data/rd-automator.db")

QUALITY_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}

TORRENTIO_BASE = "https://torrentio.strem.fun"
TORRENTIO_OPTS = "providers=yts,eztv,rarbg,1337x,thepiratebay,kickasstorrents,torrentgalaxy,magnetdl,rutor,rutracker|sort=qualitysize|qualityfilter=scr,cam"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            year TEXT,
            imdb_id TEXT,
            content_type TEXT,
            season INTEGER,
            episode INTEGER,
            resolution TEXT,
            hdr_type TEXT,
            audio TEXT,
            source TEXT,
            size_gb REAL,
            status TEXT,
            error_message TEXT,
            poster_url TEXT,
            backdrop_url TEXT,
            tmdb_id TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

def db_insert(record):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.utcnow().isoformat()
    c.execute('''
        INSERT INTO downloads (title, year, imdb_id, content_type, season, episode,
            resolution, hdr_type, audio, source, size_gb, status, error_message,
            poster_url, backdrop_url, tmdb_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        record.get('title'), record.get('year'), record.get('imdb_id'),
        record.get('content_type'), record.get('season'), record.get('episode'),
        record.get('resolution'), record.get('hdr_type'), record.get('audio'),
        record.get('source'), record.get('size_gb'), record.get('status'),
        record.get('error_message'), record.get('poster_url'), record.get('backdrop_url'),
        record.get('tmdb_id'), now, now
    ))
    row_id = c.lastrowid
    conn.commit()
    conn.close()
    return row_id

def db_update(row_id, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    kwargs['updated_at'] = datetime.utcnow().isoformat()
    set_clause = ', '.join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [row_id]
    c.execute(f"UPDATE downloads SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()

def db_get_all(limit=100):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM downloads ORDER BY created_at DESC LIMIT ?', (limit,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def db_get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM downloads WHERE status='success'")
    success = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM downloads WHERE status='failed'")
    failed = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM downloads WHERE status='processing'")
    processing = c.fetchone()[0]
    c.execute("SELECT SUM(size_gb) FROM downloads WHERE status='success'")
    total_gb = c.fetchone()[0] or 0
    conn.close()
    return {'success': success, 'failed': failed, 'processing': processing, 'total_gb': round(total_gb, 1)}

# ─── TMDB ─────────────────────────────────────────────────────────────────────

def get_tmdb_info(imdb_id, content_type="movie"):
    if not TMDB_API_KEY:
        return None, None, None
    try:
        find_url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"
        resp = requests.get(find_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = data.get('movie_results', []) if content_type == 'movie' else data.get('tv_results', [])
        if not results:
            return None, None, None
        item = results[0]
        tmdb_id = str(item.get('id', ''))
        poster = item.get('poster_path')
        backdrop = item.get('backdrop_path')
        poster_url = f"https://image.tmdb.org/t/p/w185{poster}" if poster else None
        backdrop_url = f"https://image.tmdb.org/t/p/w780{backdrop}" if backdrop else None
        return tmdb_id, poster_url, backdrop_url
    except Exception as e:
        log.warning(f"TMDB lookup failed for {imdb_id}: {e}")
        return None, None, None

# ─── Badge Parsing ────────────────────────────────────────────────────────────

def parse_resolution(title):
    t = title.lower()
    if "2160p" in t or "4k" in t or "uhd" in t:
        return "4K UHD"
    if "1080p" in t:
        return "1080p"
    if "720p" in t:
        return "720p"
    if "480p" in t:
        return "480p"
    return "Unknown"

def parse_hdr(title):
    t = title.lower()
    if "dolby vision" in t or " dv " in t or ".dv." in t or "dovi" in t:
        if "hdr10+" in t or "hdr10 plus" in t:
            return "DV + HDR10+"
        if "hdr10" in t:
            return "DV + HDR10"
        return "Dolby Vision"
    if "hdr10+" in t or "hdr10plus" in t:
        return "HDR10+"
    if "hdr10" in t:
        return "HDR10"
    if "hlg" in t:
        return "HLG"
    if "hdr" in t:
        return "HDR"
    if "sdr" in t:
        return "SDR"
    return "SDR"

def parse_audio(title):
    t = title.lower()
    if "atmos" in t or "truehd atmos" in t:
        return "Atmos"
    if "dts-x" in t or "dtsx" in t:
        return "DTS-X"
    if "dts-hd" in t or "dts hd" in t or "dtshd" in t:
        return "DTS-HD MA"
    if "truehd" in t or "true hd" in t:
        return "TrueHD"
    if "dts" in t:
        return "DTS"
    if "eac3" in t or "e-ac-3" in t or "dd+" in t or "ddp" in t:
        return "DD+"
    if "ac3" in t or "dd " in t or " dd." in t:
        return "DD"
    if "aac" in t:
        return "AAC"
    if "flac" in t:
        return "FLAC"
    if "opus" in t:
        return "Opus"
    return "Unknown"

def parse_source(title):
    t = title.lower()
    if "remux" in t:
        return "Remux"
    if "bluray" in t or "blu-ray" in t or "bdrip" in t or "bluray" in t:
        return "BluRay"
    if "web-dl" in t or "webdl" in t:
        return "WEB-DL"
    if "webrip" in t or "web-rip" in t:
        return "WEBRip"
    if "hdtv" in t:
        return "HDTV"
    if "dvdrip" in t or "dvd" in t:
        return "DVD"
    return "WEB"

# ─── Core Logic ───────────────────────────────────────────────────────────────

def get_jd_device():
    try:
        jd = myjdapi.Myjdapi()
        jd.set_app_key("rd-automator")
        jd.connect(MYJ_EMAIL, MYJ_PASSWORD)
        jd.update_devices()
        device = jd.get_device(MYJ_DEVICE)
        return device
    except Exception as e:
        log.error(f"MyJD connect error: {e}")
        return None

def validate_rd_url(url, expected_size_gb, min_size_gb=0.05):
    try:
        resp = requests.head(url, timeout=15, allow_redirects=True, headers=HEADERS)
        content_length = resp.headers.get("content-length", 0)
        actual_size_gb = int(content_length) / (1024**3)
        log.info(f"RD URL resolved to {actual_size_gb:.2f}GB (expected ~{expected_size_gb:.2f}GB)")
        if actual_size_gb < min_size_gb:
            log.warning(f"RD file too small ({actual_size_gb:.2f}GB)")
            return False
        if expected_size_gb > 0 and actual_size_gb < (expected_size_gb * 0.5):
            log.warning(f"RD file ({actual_size_gb:.2f}GB) much smaller than expected ({expected_size_gb:.2f}GB)")
            return False
        return True
    except Exception as e:
        log.warning(f"Could not validate RD URL: {e}")
        return True

def send_to_jdownloader(url, title):
    try:
        device = get_jd_device()
        if not device:
            return False
        device.linkgrabber.add_links([{
            "autostart": True,
            "links": url,
            "packageName": title,
            "destinationFolder": "/output",
            "overwritePackagizerRules": True
        }])
        log.info(f"Sent to JDownloader: {title}")
        return True
    except Exception as e:
        log.error(f"JDownloader send failed: {e}")
        return False

def get_quality_rank(title):
    t = title.lower()
    if "2160p" in t or "4k" in t or "uhd" in t:
        return 4
    if "1080p" in t:
        return 3
    if "720p" in t:
        return 2
    if "480p" in t:
        return 1
    return 0

def get_size_gb(title):
    match = re.search(r'([\d.]+)\s*(GB|MB)', title, re.IGNORECASE)
    if match:
        size = float(match.group(1))
        unit = match.group(2).upper()
        return size if unit == "GB" else size / 1024
    return 0

def get_streams(imdb_id, content_type="movie", season=None, episode=None):
    opts = f"{TORRENTIO_OPTS}|realdebrid={RD_API_KEY}"
    if content_type == "movie":
        endpoint = f"stream/movie/{imdb_id}.json"
    else:
        endpoint = f"stream/series/{imdb_id}:{season}:{episode}.json"
    url = f"{TORRENTIO_BASE}/{opts}/{endpoint}"
    log.info(f"Querying Torrentio for {imdb_id} ({content_type})")
    try:
        resp = requests.get(url, timeout=30, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        streams = data.get("streams", [])
        log.info(f"Got {len(streams)} streams")
        return streams
    except Exception as e:
        log.error(f"Torrentio query failed: {e}")
        return []

def is_hdr(title):
    t = title.lower()
    return any(x in t for x in ["hdr", "hdr10", "dolby vision", "dv", "hlg", "dovi"])

def is_sdr(title):
    t = title.lower()
    return "sdr" in t and not is_hdr(t)

def pick_best_stream(streams, max_size_gb=30, min_quality="2160p"):
    min_rank = QUALITY_RANK.get(min_quality, 3)
    hdr_candidates = []
    sdr_candidates = []
    for stream in streams:
        title = stream.get("title", "") or stream.get("name", "")
        url = stream.get("url", "")
        if not url or not url.startswith("http"):
            continue
        quality = get_quality_rank(title)
        size_gb = get_size_gb(title)
        if quality < min_rank:
            continue
        if size_gb > max_size_gb and size_gb > 0:
            continue
        if size_gb < 0.1 and size_gb > 0:
            log.info(f"Skipping too small ({size_gb:.2f}GB): {title[:60]}")
            continue
        entry = {"stream": stream, "url": url, "title": title, "quality": quality, "size_gb": size_gb}
        if is_sdr(title):
            sdr_candidates.append(entry)
        else:
            hdr_candidates.append(entry)
    if not hdr_candidates and not sdr_candidates:
        return None
    for label, pool in [("HDR", hdr_candidates), ("SDR", sdr_candidates)]:
        pool.sort(key=lambda x: (x["quality"], x["size_gb"]), reverse=True)
        for candidate in pool:
            log.info(f"Validating {label} stream: {candidate['title'][:60]}")
            if validate_rd_url(candidate["url"], candidate["size_gb"]):
                log.info(f"Best ({label}): {candidate['title'][:100]} ({candidate['size_gb']:.1f}GB)")
                candidate["hdr"] = label == "HDR"
                return candidate
            log.warning(f"Skipping unvalidated stream, trying next...")
    log.warning("No validated streams found")
    return None

# ─── Webhooks ─────────────────────────────────────────────────────────────────

@app.route("/webhook/radarr", methods=["POST"])
def radarr_webhook():
    data = request.json
    event = data.get("eventType", "")
    log.info(f"Radarr event: {event}")
    if event not in ("Grab", "MovieAdded"):
        return jsonify({"status": "ignored", "event": event})
    movie = data.get("movie", {})
    imdb_id = movie.get("imdbId", "")
    title = movie.get("title", "Unknown")
    year = str(movie.get("year", ""))
    if not imdb_id:
        return jsonify({"status": "error", "message": "No IMDB ID"}), 400
    log.info(f"Processing: {title} ({year}) [{imdb_id}]")

    tmdb_id, poster_url, backdrop_url = get_tmdb_info(imdb_id, "movie")
    row_id = db_insert({
        'title': title, 'year': year, 'imdb_id': imdb_id, 'content_type': 'movie',
        'status': 'processing', 'poster_url': poster_url, 'backdrop_url': backdrop_url, 'tmdb_id': tmdb_id
    })

    streams = get_streams(imdb_id, "movie")
    if not streams:
        db_update(row_id, status='failed', error_message='No streams found')
        return jsonify({"status": "no_streams"}), 404

    best = pick_best_stream(streams, MAX_SIZE_GB_MOVIE, MIN_QUALITY_MOVIE)
    if not best:
        db_update(row_id, status='failed', error_message='No suitable stream found')
        return jsonify({"status": "no_suitable_stream"}), 404

    stream_title = best["title"]
    db_update(row_id,
        resolution=parse_resolution(stream_title),
        hdr_type=parse_hdr(stream_title),
        audio=parse_audio(stream_title),
        source=parse_source(stream_title),
        size_gb=best["size_gb"]
    )

    success = send_to_jdownloader(best["url"], f"{title} ({year})")
    db_update(row_id, status='success' if success else 'failed',
              error_message=None if success else 'JDownloader send failed')

    return jsonify({"status": "success" if success else "jd_failed", "movie": title,
                    "quality": best["quality"], "size_gb": best["size_gb"], "hdr": best.get("hdr", False)})


@app.route("/webhook/sonarr", methods=["POST"])
@app.route("/webhook/sonarr-anime", methods=["POST"])
def sonarr_webhook():
    data = request.json
    event = data.get("eventType", "")
    log.info(f"Sonarr event: {event}")
    if event != "Grab":
        return jsonify({"status": "ignored", "event": event})
    series = data.get("series", {})
    episodes = data.get("episodes", [])
    imdb_id = series.get("imdbId", "")
    series_title = series.get("title", "Unknown")
    year = str(series.get("year", ""))
    if not imdb_id or not episodes:
        return jsonify({"status": "error", "message": "Missing data"}), 400

    tmdb_id, poster_url, backdrop_url = get_tmdb_info(imdb_id, "tv")
    results = []

    for ep in episodes:
        season = ep.get("seasonNumber", 1)
        episode = ep.get("episodeNumber", 1)
        ep_title = f"{series_title} S{season:02d}E{episode:02d}"

        row_id = db_insert({
            'title': series_title, 'year': year, 'imdb_id': imdb_id,
            'content_type': 'tv', 'season': season, 'episode': episode,
            'status': 'processing', 'poster_url': poster_url,
            'backdrop_url': backdrop_url, 'tmdb_id': tmdb_id
        })

        streams = get_streams(imdb_id, "series", season, episode)
        if not streams:
            db_update(row_id, status='failed', error_message='No streams found')
            results.append({"episode": ep_title, "status": "no_streams"})
            continue

        best = pick_best_stream(streams, MAX_SIZE_GB_TV, MIN_QUALITY_TV)
        if not best:
            db_update(row_id, status='failed', error_message='No suitable stream found')
            results.append({"episode": ep_title, "status": "no_suitable_stream"})
            continue

        stream_title = best["title"]
        db_update(row_id,
            resolution=parse_resolution(stream_title),
            hdr_type=parse_hdr(stream_title),
            audio=parse_audio(stream_title),
            source=parse_source(stream_title),
            size_gb=best["size_gb"]
        )

        success = send_to_jdownloader(best["url"], ep_title)
        db_update(row_id, status='success' if success else 'failed',
                  error_message=None if success else 'JDownloader send failed')
        results.append({"episode": ep_title, "status": "success" if success else "jd_failed",
                        "size_gb": best["size_gb"]})

    return jsonify({"status": "processed", "results": results})


# ─── Status UI ────────────────────────────────────────────────────────────────

STATUS_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RD Automator</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0c10;
    --surface: #111318;
    --surface2: #1a1d24;
    --border: #2a2d35;
    --text: #e8eaf0;
    --muted: #6b7280;
    --accent: #e5383b;
    --accent2: #f4a261;

    --badge-res: #1d4ed8;
    --badge-dv: #7c3aed;
    --badge-hdr10p: #b45309;
    --badge-hdr10: #92400e;
    --badge-hdr: #78350f;
    --badge-sdr: #374151;
    --badge-atmos: #065f46;
    --badge-dtsx: #064e3b;
    --badge-dtshd: #065f46;
    --badge-truehd: #166534;
    --badge-dts: #14532d;
    --badge-ddp: #1e3a5f;
    --badge-dd: #1e3a5f;
    --badge-aac: #374151;
    --badge-remux: #581c87;
    --badge-bluray: #1e40af;
    --badge-webdl: #155e75;
    --badge-webrip: #164e63;
    --badge-web: #1f2937;
    --badge-success: #14532d;
    --badge-failed: #7f1d1d;
    --badge-processing: #1e3a5f;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    min-height: 100vh;
  }

  /* Noise texture overlay */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.03'/%3E%3C/svg%3E");
    pointer-events: none;
    z-index: 0;
  }

  header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: rgba(10, 12, 16, 0.92);
    backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--border);
    padding: 0 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 64px;
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .logo-icon {
    width: 36px;
    height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .logo-text {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 1.6rem;
    letter-spacing: 2px;
    color: var(--text);
  }

  .logo-text span { color: var(--accent); }

  .header-right {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .refresh-btn {
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 0.4rem 1rem;
    border-radius: 6px;
    cursor: pointer;
    font-family: 'DM Sans', sans-serif;
    font-size: 0.8rem;
    transition: all 0.2s;
  }

  .refresh-btn:hover { border-color: var(--accent); color: var(--text); }

  main {
    max-width: 1400px;
    margin: 0 auto;
    padding: 2rem;
    position: relative;
    z-index: 1;
  }

  /* Stats Bar */
  .stats-bar {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1rem;
    margin-bottom: 2rem;
  }

  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1.2rem 1.5rem;
    position: relative;
    overflow: hidden;
  }

  .stat-card::after {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
  }

  .stat-card.success::after { background: #22c55e; }
  .stat-card.failed::after { background: var(--accent); }
  .stat-card.processing::after { background: #3b82f6; }
  .stat-card.storage::after { background: var(--accent2); }

  .stat-label {
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 0.4rem;
  }

  .stat-value {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 2.2rem;
    letter-spacing: 1px;
    line-height: 1;
  }

  .stat-card.success .stat-value { color: #22c55e; }
  .stat-card.failed .stat-value { color: var(--accent); }
  .stat-card.processing .stat-value { color: #3b82f6; }
  .stat-card.storage .stat-value { color: var(--accent2); }

  /* Filters */
  .filters {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1.5rem;
    flex-wrap: wrap;
  }

  .filter-btn {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 0.4rem 1rem;
    border-radius: 20px;
    cursor: pointer;
    font-family: 'DM Sans', sans-serif;
    font-size: 0.8rem;
    transition: all 0.2s;
  }

  .filter-btn:hover, .filter-btn.active {
    border-color: var(--accent);
    color: var(--text);
    background: rgba(229, 56, 59, 0.1);
  }

  /* Cards Grid */
  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 1rem;
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    transition: transform 0.2s, border-color 0.2s;
    animation: fadeIn 0.3s ease forwards;
    opacity: 0;
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .card:hover {
    transform: translateY(-2px);
    border-color: #3a3d45;
  }

  .card.status-failed { border-left: 3px solid var(--accent); }
  .card.status-success { border-left: 3px solid #22c55e; }
  .card.status-processing { border-left: 3px solid #3b82f6; }

  .card-poster {
    width: 80px;
    min-width: 80px;
    background: var(--surface2);
    position: relative;
    overflow: hidden;
  }

  .card-poster img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }

  .card-poster-placeholder {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 2rem;
    color: var(--border);
    min-height: 120px;
  }

  .card-body {
    flex: 1;
    padding: 0.9rem 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
  }

  .card-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .card-title {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text);
    line-height: 1.3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .card-meta {
    font-size: 0.72rem;
    color: var(--muted);
    white-space: nowrap;
  }

  .card-episode {
    font-size: 0.75rem;
    color: var(--accent2);
    font-weight: 500;
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .badge {
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.5px;
    padding: 2px 6px;
    border-radius: 4px;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .badge-4k { background: var(--badge-res); color: #93c5fd; }
  .badge-1080p { background: #1e3a5f; color: #93c5fd; }
  .badge-720p { background: #1f2937; color: #9ca3af; }
  .badge-480p { background: #1f2937; color: #6b7280; }

  .badge-dv { background: var(--badge-dv); color: #c4b5fd; }
  .badge-dvhdr10p { background: var(--badge-dv); color: #c4b5fd; }
  .badge-dvhdr10 { background: var(--badge-dv); color: #c4b5fd; }
  .badge-hdr10p { background: var(--badge-hdr10p); color: #fcd34d; }
  .badge-hdr10 { background: var(--badge-hdr10); color: #fcd34d; }
  .badge-hlg { background: #78350f; color: #fcd34d; }
  .badge-hdr { background: var(--badge-hdr); color: #fbbf24; }
  .badge-sdr { background: var(--badge-sdr); color: #9ca3af; }

  .badge-atmos { background: var(--badge-atmos); color: #6ee7b7; }
  .badge-dtsx { background: var(--badge-dtsx); color: #6ee7b7; }
  .badge-dtshd { background: var(--badge-dtshd); color: #6ee7b7; }
  .badge-truehd { background: var(--badge-truehd); color: #86efac; }
  .badge-dts { background: var(--badge-dts); color: #86efac; }
  .badge-ddp { background: var(--badge-ddp); color: #93c5fd; }
  .badge-dd { background: var(--badge-dd); color: #93c5fd; }
  .badge-aac { background: var(--badge-aac); color: #9ca3af; }
  .badge-flac { background: #1c3a2e; color: #6ee7b7; }
  .badge-opus { background: #1f2937; color: #9ca3af; }

  .badge-remux { background: var(--badge-remux); color: #d8b4fe; }
  .badge-bluray { background: var(--badge-bluray); color: #93c5fd; }
  .badge-webdl { background: var(--badge-webdl); color: #67e8f9; }
  .badge-webrip { background: var(--badge-webrip); color: #67e8f9; }
  .badge-web { background: var(--badge-web); color: #9ca3af; }
  .badge-hdtv { background: #1f2937; color: #9ca3af; }

  .badge-success { background: var(--badge-success); color: #86efac; }
  .badge-failed { background: var(--badge-failed); color: #fca5a5; }
  .badge-processing { background: var(--badge-processing); color: #93c5fd; }

  .card-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: auto;
  }

  .card-size {
    font-size: 0.72rem;
    color: var(--muted);
  }

  .card-time {
    font-size: 0.68rem;
    color: var(--muted);
  }

  .error-msg {
    font-size: 0.7rem;
    color: #f87171;
    font-style: italic;
  }

  .empty-state {
    grid-column: 1/-1;
    text-align: center;
    padding: 4rem;
    color: var(--muted);
  }

  .empty-state .icon { font-size: 3rem; margin-bottom: 1rem; }
  .empty-state p { font-size: 0.9rem; }

  /* Processing pulse */
  .card.status-processing .card-poster {
    animation: pulse-border 2s ease-in-out infinite;
  }

  @keyframes pulse-border {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }

  @media (max-width: 600px) {
    .stats-bar { grid-template-columns: repeat(2, 1fr); }
    .cards-grid { grid-template-columns: 1fr; }
    main { padding: 1rem; }
  }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="rdGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" style="stop-color:#7ecba1;stop-opacity:1" />
            <stop offset="50%" style="stop-color:#6ab8d4;stop-opacity:1" />
            <stop offset="100%" style="stop-color:#8ab4d8;stop-opacity:1" />
          </linearGradient>
        </defs>
        <circle cx="18" cy="18" r="13" stroke="url(#rdGrad)" stroke-width="3.5" fill="none" stroke-linecap="round" stroke-dasharray="68 14" stroke-dashoffset="10"/>
      </svg>
    </div>
    <div class="logo-text">RD<span>.</span>AUTOMATOR</div>
  </div>
  <div class="header-right">
    <span style="font-size:0.75rem;color:var(--muted)" id="last-updated"></span>
    <button class="refresh-btn" onclick="loadData()">↻ Refresh</button>
  </div>
</header>

<main>
  <div class="stats-bar">
    <div class="stat-card success">
      <div class="stat-label">Completed</div>
      <div class="stat-value" id="stat-success">—</div>
    </div>
    <div class="stat-card processing">
      <div class="stat-label">Processing</div>
      <div class="stat-value" id="stat-processing">—</div>
    </div>
    <div class="stat-card failed">
      <div class="stat-label">Failed</div>
      <div class="stat-value" id="stat-failed">—</div>
    </div>
    <div class="stat-card storage">
      <div class="stat-label">Total Downloaded</div>
      <div class="stat-value" id="stat-storage">—</div>
    </div>
  </div>

  <div class="filters">
    <button class="filter-btn active" onclick="setFilter('all', this)">All</button>
    <button class="filter-btn" onclick="setFilter('movie', this)">Movies</button>
    <button class="filter-btn" onclick="setFilter('tv', this)">TV</button>
    <button class="filter-btn" onclick="setFilter('success', this)">Success</button>
    <button class="filter-btn" onclick="setFilter('failed', this)">Failed</button>
    <button class="filter-btn" onclick="setFilter('processing', this)">Processing</button>
  </div>

  <div class="cards-grid" id="cards-grid">
    <div class="empty-state"><div class="icon">📡</div><p>Loading...</p></div>
  </div>
</main>

<script>
let allData = [];
let currentFilter = 'all';

function badgeClass(type, value) {
  if (!value || value === 'Unknown') return '';
  const map = {
    resolution: {
      '4K UHD': 'badge-4k', '1080p': 'badge-1080p',
      '720p': 'badge-720p', '480p': 'badge-480p'
    },
    hdr: {
      'Dolby Vision': 'badge-dv', 'DV + HDR10+': 'badge-dvhdr10p',
      'DV + HDR10': 'badge-dvhdr10', 'HDR10+': 'badge-hdr10p',
      'HDR10': 'badge-hdr10', 'HLG': 'badge-hlg',
      'HDR': 'badge-hdr', 'SDR': 'badge-sdr'
    },
    audio: {
      'Atmos': 'badge-atmos', 'DTS-X': 'badge-dtsx',
      'DTS-HD MA': 'badge-dtshd', 'TrueHD': 'badge-truehd',
      'DTS': 'badge-dts', 'DD+': 'badge-ddp',
      'DD': 'badge-dd', 'AAC': 'badge-aac',
      'FLAC': 'badge-flac', 'Opus': 'badge-opus'
    },
    source: {
      'Remux': 'badge-remux', 'BluRay': 'badge-bluray',
      'WEB-DL': 'badge-webdl', 'WEBRip': 'badge-webrip',
      'WEB': 'badge-web', 'HDTV': 'badge-hdtv', 'DVD': 'badge-web'
    },
    status: {
      'success': 'badge-success', 'failed': 'badge-failed', 'processing': 'badge-processing'
    }
  };
  return (map[type] && map[type][value]) || '';
}

function badge(type, value) {
  if (!value || value === 'Unknown') return '';
  const cls = badgeClass(type, value);
  if (!cls) return '';
  return `<span class="badge ${cls}">${value}</span>`;
}

function timeAgo(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso + 'Z').getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return `${Math.floor(diff/86400)}d ago`;
}

function renderCard(item, index) {
  const delay = Math.min(index * 0.04, 0.5);
  const episodeLabel = item.content_type === 'tv' && item.season
    ? `<div class="card-episode">S${String(item.season).padStart(2,'0')}E${String(item.episode).padStart(2,'0')}</div>` : '';
  const poster = item.poster_url
    ? `<img src="${item.poster_url}" alt="${item.title}" loading="lazy" onerror="this.parentElement.innerHTML='<div class=card-poster-placeholder>🎬</div>'">`
    : `<div class="card-poster-placeholder">🎬</div>`;
  const size = item.size_gb ? `${item.size_gb.toFixed(1)} GB` : '';
  const errorMsg = item.error_message ? `<div class="error-msg">${item.error_message}</div>` : '';

  return `
    <div class="card status-${item.status}" style="animation-delay:${delay}s">
      <div class="card-poster">${poster}</div>
      <div class="card-body">
        <div class="card-header">
          <div>
            <div class="card-title">${item.title}${item.year ? ' ('+item.year+')' : ''}</div>
            ${episodeLabel}
          </div>
          <div class="card-meta">${badge('status', item.status)}</div>
        </div>
        <div class="badges">
          ${badge('resolution', item.resolution)}
          ${badge('hdr', item.hdr_type)}
          ${badge('audio', item.audio)}
          ${badge('source', item.source)}
        </div>
        ${errorMsg}
        <div class="card-footer">
          <span class="card-size">${size}</span>
          <span class="card-time">${timeAgo(item.created_at)}</span>
        </div>
      </div>
    </div>`;
}

function setFilter(filter, btn) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderCards();
}

function renderCards() {
  const grid = document.getElementById('cards-grid');
  let filtered = allData;
  if (currentFilter === 'movie') filtered = allData.filter(d => d.content_type === 'movie');
  else if (currentFilter === 'tv') filtered = allData.filter(d => d.content_type === 'tv');
  else if (['success','failed','processing'].includes(currentFilter))
    filtered = allData.filter(d => d.status === currentFilter);

  if (filtered.length === 0) {
    grid.innerHTML = `<div class="empty-state"><div class="icon">📭</div><p>No items found</p></div>`;
    return;
  }
  grid.innerHTML = filtered.map((item, i) => renderCard(item, i)).join('');
}

async function loadData() {
  try {
    const [dataRes, statsRes] = await Promise.all([
      fetch('/api/history'),
      fetch('/api/stats')
    ]);
    allData = await dataRes.json();
    const stats = await statsRes.json();
    document.getElementById('stat-success').textContent = stats.success;
    document.getElementById('stat-failed').textContent = stats.failed;
    document.getElementById('stat-processing').textContent = stats.processing;
    document.getElementById('stat-storage').textContent = stats.total_gb.toFixed(1) + ' GB';
    document.getElementById('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
    renderCards();
  } catch(e) {
    console.error('Failed to load data:', e);
  }
}

loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>'''

@app.route("/")
@app.route("/status")
def status_page():
    return render_template_string(STATUS_HTML)

@app.route("/api/history")
def api_history():
    limit = request.args.get('limit', 100, type=int)
    return jsonify(db_get_all(limit))

@app.route("/api/stats")
def api_stats():
    return jsonify(db_get_stats())

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "rd_key_set": bool(RD_API_KEY),
        "myj_configured": bool(MYJ_EMAIL and MYJ_PASSWORD),
        "tmdb_configured": bool(TMDB_API_KEY),
        "movie": {"min_quality": MIN_QUALITY_MOVIE, "max_size_gb": MAX_SIZE_GB_MOVIE},
        "tv": {"min_quality": MIN_QUALITY_TV, "max_size_gb": MAX_SIZE_GB_TV}
    })

@app.route("/test/<imdb_id>", methods=["GET"])
def test_movie(imdb_id):
    streams = get_streams(imdb_id, "movie")
    best = pick_best_stream(streams, MAX_SIZE_GB_MOVIE, MIN_QUALITY_MOVIE)
    return jsonify({
        "imdb_id": imdb_id,
        "total_streams": len(streams),
        "best": {
            "title": best["title"][:100],
            "size_gb": best["size_gb"],
            "quality": best["quality"],
            "resolution": parse_resolution(best["title"]),
            "hdr": parse_hdr(best["title"]),
            "audio": parse_audio(best["title"]),
            "source": parse_source(best["title"]),
            "url_preview": best["url"][:60] + "..."
        } if best else None
    })

@app.route("/test/send/<imdb_id>", methods=["GET"])
def test_send(imdb_id):
    streams = get_streams(imdb_id, "movie")
    best = pick_best_stream(streams, MAX_SIZE_GB_MOVIE, MIN_QUALITY_MOVIE)
    if not best:
        return jsonify({"status": "no_suitable_stream"})
    success = send_to_jdownloader(best["url"], f"Test [{imdb_id}]")
    return jsonify({"status": "success" if success else "failed", "title": best["title"][:100], "size_gb": best["size_gb"]})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8888, debug=False)
