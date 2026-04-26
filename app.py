"""
YouTube Audio → Apple Podcasts bridge.

Run:
    python app.py

Then open http://localhost:5000 in your browser.
On first run you will be prompted to enter your Internet Archive credentials
and podcast name. After that every downloaded video is:
  1. Downloaded from YouTube as MP3
  2. Uploaded to Internet Archive (free, permanent hosting)
  3. Added to an RSS feed at /feed.xml
  4. Subscribe to that URL once in Apple Podcasts — new episodes appear automatically.
"""

import json
import threading
import uuid
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, abort

from downloader import download_audio, DOWNLOADS_DIR
from uploader import upload_to_archive
from feed import build_rss, save_episode, load_episodes

app  = Flask(__name__)
PORT = int(os.environ.get("PORT", 5000))
HOST = os.environ.get("HOST_URL", f"http://localhost:{PORT}")

CONFIG_FILE = Path("config.json")

# In-memory job store
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def config_complete() -> bool:
    cfg = load_config()
    return bool(cfg.get("ia_access_key") and cfg.get("ia_secret_key") and cfg.get("podcast_id"))


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:60]


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_job(job_id: str, url: str) -> None:
    cfg = load_config()

    def on_progress(info: dict) -> None:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(info)

    try:
        # Phase 1 & 2: download + convert to MP3
        result = download_audio(url, on_progress=on_progress)
        mp3_path = DOWNLOADS_DIR / result.filename

        # Phase 3: upload to Internet Archive
        video_id   = _extract_video_id(url)
        identifier = f"{cfg['podcast_id']}-{video_id or uuid.uuid4().hex[:8]}"

        ia_url = upload_to_archive(
            identifier  = identifier,
            file_path   = mp3_path,
            title       = result.title,
            access_key  = cfg["ia_access_key"],
            secret_key  = cfg["ia_secret_key"],
            on_progress = on_progress,
        )

        # Phase 4: save to episode store
        save_episode({
            "title":       result.title,
            "filename":    result.filename,
            "ia_url":      ia_url,
            "duration":    result.duration,
            "uploader":    result.uploader,
            "thumbnail":   result.thumbnail,
            "description": result.title,
            "published":   datetime.now(timezone.utc).isoformat(),
        })

        with jobs_lock:
            jobs[job_id] = {
                "status":   "done",
                "title":    result.title,
                "filename": result.filename,
                "ia_url":   ia_url,
                "duration": result.duration,
                "uploader": result.uploader,
                "thumbnail":result.thumbnail,
            }

    except Exception as exc:
        with jobs_lock:
            jobs[job_id] = {"status": "error", "error": str(exc)}


def _extract_video_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/setup")
def api_setup():
    data = request.get_json(force=True, silent=True) or {}
    required = ["ia_access_key", "ia_secret_key", "podcast_name", "podcast_id"]
    for key in required:
        if not data.get(key, "").strip():
            return jsonify({"error": f"Missing field: {key}"}), 400
    save_config({k: data[k].strip() for k in required})
    return jsonify({"ok": True})


@app.post("/api/download")
def start_download():
    if not config_complete():
        return jsonify({"error": "Setup not complete"}), 400

    data   = request.get_json(force=True, silent=True) or {}
    url    = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "pending"}

    threading.Thread(target=_run_job, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id}), 202


@app.get("/api/status/<job_id>")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


@app.get("/api/episodes")
def api_episodes():
    return jsonify(load_episodes())


@app.get("/api/config")
def api_config():
    cfg = load_config()
    # Never expose keys to the frontend
    return jsonify({
        "configured":        config_complete(),
        "podcast_name":      cfg.get("podcast_name", ""),
        "podcast_id":        cfg.get("podcast_id", ""),
        "ia_access_key_set": bool(cfg.get("ia_access_key")),
    })


@app.get("/feed.xml")
def rss_feed():
    cfg = load_config()
    rss = build_rss(
        host_url            = HOST,
        podcast_name        = cfg.get("podcast_name", "My YouTube Podcast"),
        podcast_description = cfg.get("podcast_description", "Audio from YouTube."),
    )
    return Response(rss, mimetype="application/rss+xml")


@app.get("/api/file/<path:filename>")
def serve_file(filename: str):
    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists():
        abort(404)
    return send_from_directory(str(DOWNLOADS_DIR.resolve()), filename,
                               as_attachment=True, download_name=filename)


# ---------------------------------------------------------------------------
# Single-page UI
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>YouTube → Podcast</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f0f0f;
      color: #e8e8e8;
      min-height: 100vh;
      padding: 2rem 1rem 4rem;
      display: flex;
      flex-direction: column;
      align-items: center;
    }

    .wrap { width: 100%; max-width: 600px; }

    /* ── Header ── */
    .header {
      display: flex;
      align-items: center;
      gap: .75rem;
      margin-bottom: 1.75rem;
    }

    h1 { font-size: 1.35rem; font-weight: 700; color: #fff; letter-spacing: -.02em; }
    h1 span { color: #ff4444; }

    /* ── Cards ── */
    .card {
      background: #1a1a1a;
      border: 1px solid #2e2e2e;
      border-radius: 14px;
      padding: 1.5rem;
      margin-bottom: 1.25rem;
    }

    .card-title {
      font-size: .72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: #666;
      margin-bottom: 1rem;
    }

    /* ── Feed URL ── */
    .feed-row {
      display: flex;
      align-items: center;
      gap: .5rem;
      background: #111;
      border: 1px solid #2a2a2a;
      border-radius: 10px;
      padding: .55rem .75rem;
    }

    .feed-row span {
      flex: 1;
      font-family: monospace;
      font-size: .82rem;
      color: #aaa;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .copy-btn {
      background: #2a2a2a;
      border: none;
      border-radius: 6px;
      color: #ccc;
      cursor: pointer;
      font-size: .75rem;
      padding: .3rem .65rem;
      white-space: nowrap;
      transition: background .15s;
    }

    .copy-btn:hover { background: #333; color: #fff; }

    .feed-hint {
      font-size: .75rem;
      color: #555;
      margin-top: .6rem;
    }

    /* ── Input row ── */
    label {
      display: block;
      font-size: .72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: #666;
      margin-bottom: .5rem;
    }

    .input-row { display: flex; gap: .5rem; }

    input[type=text], input[type=password] {
      flex: 1;
      background: #111;
      border: 1px solid #2e2e2e;
      border-radius: 10px;
      color: #e8e8e8;
      font-size: .9rem;
      padding: .6rem .9rem;
      outline: none;
      transition: border-color .15s;
    }

    input:focus { border-color: #ff4444; }
    input::placeholder { color: #444; }

    .btn-red {
      background: #ff4444;
      border: none;
      border-radius: 10px;
      color: #fff;
      cursor: pointer;
      font-size: .9rem;
      font-weight: 600;
      padding: .6rem 1.3rem;
      transition: background .15s, opacity .15s;
      white-space: nowrap;
    }

    .btn-red:hover:not(:disabled) { background: #e03333; }
    .btn-red:disabled { opacity: .45; cursor: not-allowed; }

    /* ── Progress ── */
    #progressCard { display: none; }

    .phase-label {
      font-size: .72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: #ff4444;
      margin-bottom: .5rem;
    }

    .pct-row {
      display: flex;
      align-items: flex-end;
      gap: .3rem;
      margin-bottom: .65rem;
    }

    .pct-num {
      font-size: 2.4rem;
      font-weight: 800;
      line-height: 1;
      color: #fff;
      font-variant-numeric: tabular-nums;
    }

    .pct-sym { font-size: 1rem; color: #555; padding-bottom: .25rem; }

    .bar-track {
      background: #222;
      border-radius: 999px;
      height: 10px;
      overflow: hidden;
      margin-bottom: .75rem;
    }

    .bar-fill {
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #c0392b, #ff4444, #ff6b6b);
      width: 0%;
      transition: width .35s cubic-bezier(.4,0,.2,1);
      position: relative;
    }

    .bar-fill::after {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.18), transparent);
      animation: shimmer 1.6s linear infinite;
    }

    @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }

    .bar-fill.spin { width: 35% !important; animation: slide 1.4s ease-in-out infinite; }
    .bar-fill.spin::after { display: none; }
    @keyframes slide { 0% { transform: translateX(-120%); } 100% { transform: translateX(360%); } }

    .pills { display: flex; gap: .4rem; flex-wrap: wrap; }

    .pill {
      display: flex;
      align-items: center;
      gap: .3rem;
      background: #1c1c1c;
      border: 1px solid #2e2e2e;
      border-radius: 999px;
      padding: .25rem .65rem;
      font-size: .75rem;
      color: #bbb;
    }

    .pill strong { color: #fff; font-variant-numeric: tabular-nums; }

    /* ── Result ── */
    #resultCard { display: none; }

    .thumb-row { display: flex; gap: 1rem; align-items: center; margin-bottom: 1rem; }

    .thumb-row img {
      width: 64px; height: 64px;
      object-fit: cover;
      border-radius: 8px;
      background: #222;
      flex-shrink: 0;
    }

    .ep-meta strong { display: block; font-size: .95rem; color: #fff; }
    .ep-meta span { font-size: .78rem; color: #666; }

    .result-btns { display: flex; gap: .5rem; }

    .btn-green {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: .4rem;
      background: #1db954;
      border-radius: 10px;
      color: #fff;
      font-weight: 600;
      font-size: .85rem;
      padding: .6rem .9rem;
      text-decoration: none;
      transition: background .15s;
    }

    .btn-green:hover { background: #17a349; }

    /* ── Error ── */
    .error-box {
      display: none;
      margin-top: 1rem;
      background: #2a1111;
      border: 1px solid #5c2020;
      border-radius: 10px;
      color: #ff7070;
      font-size: .82rem;
      padding: .7rem 1rem;
    }

    /* ── Episodes list ── */
    .ep-row {
      display: flex;
      align-items: center;
      gap: .75rem;
      padding: .65rem 0;
      border-bottom: 1px solid #222;
    }

    .ep-row:last-child { border-bottom: none; }

    .ep-row img {
      width: 44px; height: 44px;
      border-radius: 6px;
      object-fit: cover;
      background: #222;
      flex-shrink: 0;
    }

    .ep-row .info { flex: 1; overflow: hidden; }
    .ep-row .info strong { display: block; font-size: .85rem; color: #e0e0e0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .ep-row .info span { font-size: .73rem; color: #555; }

    .ep-row a {
      font-size: .72rem;
      color: #ff4444;
      text-decoration: none;
      white-space: nowrap;
    }

    .empty { color: #444; font-size: .85rem; text-align: center; padding: 1.5rem 0; }

    /* ── Setup overlay ── */
    #setupCard { display: none; }

    .setup-field { margin-bottom: 1rem; }
    .setup-field label { margin-bottom: .4rem; }

    .setup-hint {
      font-size: .72rem;
      color: #555;
      margin-top: .35rem;
    }
  </style>
</head>
<body>
<div class="wrap">

  <!-- Header -->
  <div class="header">
    <svg width="34" height="34" viewBox="0 0 34 34" fill="none">
      <rect width="34" height="34" rx="9" fill="#ff4444"/>
      <path d="M13 11l10 6-10 6V11z" fill="white"/>
    </svg>
    <h1><span>YouTube</span> → Apple Podcasts</h1>
  </div>

  <!-- Setup card (shown when not configured) -->
  <div class="card" id="setupCard">
    <div class="card-title">First-time Setup</div>
    <p style="font-size:.83rem;color:#777;margin-bottom:1.25rem">
      Get your free Internet Archive S3 keys at
      <a href="https://archive.org/account/s3.php" target="_blank" style="color:#ff4444">archive.org/account/s3.php</a>
    </p>

    <div class="setup-field">
      <label>Podcast Name</label>
      <input type="text" id="cfgName" placeholder="My YouTube Podcast"/>
    </div>

    <div class="setup-field">
      <label>Podcast ID <small style="text-transform:none;letter-spacing:0;font-weight:400;color:#555">(unique slug, no spaces)</small></label>
      <input type="text" id="cfgId" placeholder="my-youtube-podcast"/>
      <div class="setup-hint">Used as the Archive.org item prefix. Must be globally unique.</div>
    </div>

    <div class="setup-field">
      <label>Archive.org Access Key</label>
      <input type="text" id="cfgAccess" placeholder="Access key from archive.org/account/s3.php"/>
    </div>

    <div class="setup-field">
      <label>Archive.org Secret Key</label>
      <input type="password" id="cfgSecret" placeholder="Secret key"/>
    </div>

    <button class="btn-red" id="saveSetupBtn">Save & Continue</button>
    <div class="error-box" id="setupError"></div>
  </div>

  <!-- Feed URL card -->
  <div class="card" id="feedCard" style="display:none">
    <div class="card-title">Your Podcast Feed</div>
    <div class="feed-row">
      <span id="feedUrl"></span>
      <button class="copy-btn" onclick="copyFeed()">Copy</button>
    </div>
    <p class="feed-hint">Open Apple Podcasts → File → Follow a Show by URL → paste this URL.</p>
  </div>

  <!-- Download card -->
  <div class="card" id="downloadCard" style="display:none">
    <div class="card-title">Add Episode from YouTube</div>
    <label for="urlInput">YouTube URL</label>
    <div class="input-row">
      <input type="text" id="urlInput" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" spellcheck="false"/>
      <button class="btn-red" id="dlBtn">Download</button>
    </div>
    <div class="error-box" id="dlError"></div>
  </div>

  <!-- Progress card -->
  <div class="card" id="progressCard">
    <div class="phase-label" id="phaseLabel">Starting…</div>
    <div class="pct-row">
      <span class="pct-num" id="pctNum">0</span>
      <span class="pct-sym">%</span>
    </div>
    <div class="bar-track">
      <div class="bar-fill spin" id="barFill"></div>
    </div>
    <div class="pills" id="pills"></div>
  </div>

  <!-- Result card -->
  <div class="card" id="resultCard">
    <div class="card-title">Added to Podcast Feed</div>
    <div class="thumb-row">
      <img id="rThumb" src="" alt=""/>
      <div class="ep-meta">
        <strong id="rTitle"></strong>
        <span id="rMeta"></span>
      </div>
    </div>
    <div class="result-btns">
      <a class="btn-green" id="rIaLink" href="#" target="_blank">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 1v8m0 0L4 6m3 3 3-3M1 12h12" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
        Open on Archive.org
      </a>
    </div>
  </div>

  <!-- Episodes list -->
  <div class="card" id="episodesCard" style="display:none">
    <div class="card-title">All Episodes</div>
    <div id="epList"></div>
  </div>

</div><!-- /wrap -->

<script>
const $ = id => document.getElementById(id);

// ── Init ────────────────────────────────────────────────────────────────────
async function init() {
  const res  = await fetch('/api/config');
  const cfg  = await res.json();

  if (!cfg.configured) {
    $('setupCard').style.display = 'block';
  } else {
    showApp(cfg.podcast_name);
  }
}

function showApp(podcastName) {
  const feedUrl = `${location.origin}/feed.xml`;
  $('feedUrl').textContent = feedUrl;
  $('feedCard').style.display    = 'block';
  $('downloadCard').style.display = 'block';
  loadEpisodes();
}

// ── Setup ───────────────────────────────────────────────────────────────────
$('cfgName').addEventListener('input', () => {
  if (!$('cfgId').value) {
    $('cfgId').value = $('cfgName').value.toLowerCase()
      .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 50);
  }
});

$('saveSetupBtn').addEventListener('click', async () => {
  const err = $('setupError');
  err.style.display = 'none';

  const body = {
    podcast_name:  $('cfgName').value.trim(),
    podcast_id:    $('cfgId').value.trim(),
    ia_access_key: $('cfgAccess').value.trim(),
    ia_secret_key: $('cfgSecret').value.trim(),
  };

  if (!body.podcast_name || !body.podcast_id || !body.ia_access_key || !body.ia_secret_key) {
    err.textContent = 'All fields are required.';
    err.style.display = 'block';
    return;
  }

  const res = await fetch('/api/setup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();

  if (!res.ok) { err.textContent = data.error; err.style.display = 'block'; return; }
  $('setupCard').style.display = 'none';
  showApp(body.podcast_name);
});

// ── Feed URL copy ────────────────────────────────────────────────────────────
function copyFeed() {
  navigator.clipboard.writeText($('feedUrl').textContent);
  const btn = document.querySelector('.copy-btn');
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy', 2000);
}

// ── Download flow ────────────────────────────────────────────────────────────
$('dlBtn').addEventListener('click', startDownload);
$('urlInput').addEventListener('keydown', e => { if (e.key === 'Enter') startDownload(); });

async function startDownload() {
  const url = $('urlInput').value.trim();
  if (!url) { $('urlInput').focus(); return; }

  $('dlError').style.display = 'none';
  $('resultCard').style.display = 'none';
  $('dlBtn').disabled = true;
  setProgress(0, 'Fetching video info…', '', '');
  $('progressCard').style.display = 'block';

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (!res.ok) { stopProgress(data.error); return; }
    poll(data.job_id);
  } catch (e) {
    stopProgress('Could not reach server: ' + e.message);
  }
}

function stopProgress(errMsg) {
  $('dlBtn').disabled = false;
  $('progressCard').style.display = 'none';
  if (errMsg) { $('dlError').textContent = errMsg; $('dlError').style.display = 'block'; }
}

// ── Polling ──────────────────────────────────────────────────────────────────
async function poll(jobId) {
  const iv = setInterval(async () => {
    try {
      const res = await fetch('/api/status/' + jobId);
      const job = await res.json();

      if (job.status === 'done') {
        clearInterval(iv);
        stopProgress(null);
        showResult(job);
        loadEpisodes();
      } else if (job.status === 'error') {
        clearInterval(iv);
        stopProgress('Error: ' + job.error);
      } else if (job.phase === 'downloading') {
        setProgress(job.percent || 0, 'Downloading audio…', job.speed || '', job.eta || '');
      } else if (job.phase === 'converting') {
        setProgress(job.percent || 0, 'Converting to MP3…', '', '');
      } else if (job.phase === 'uploading') {
        setProgress(job.percent || 0, 'Uploading to Archive.org…', '', '');
      } else {
        setProgress(0, 'Fetching video info…', '', '');
      }
    } catch {
      clearInterval(iv);
      stopProgress('Lost connection to server.');
    }
  }, 500);
}

// ── Progress bar ─────────────────────────────────────────────────────────────
function setProgress(pct, phase, speed, eta) {
  const bar = $('barFill');
  if (pct > 0) {
    bar.classList.remove('spin');
    bar.style.width = pct + '%';
    $('pctNum').textContent = Math.floor(pct);
  } else {
    bar.classList.add('spin');
    bar.style.width = '';
    $('pctNum').textContent = '0';
  }
  $('phaseLabel').textContent = phase;

  const pills = $('pills');
  pills.innerHTML = '';
  if (speed) pills.innerHTML += `<div class="pill">Speed <strong>${speed}</strong></div>`;
  if (eta)   pills.innerHTML += `<div class="pill">ETA <strong>${eta}</strong></div>`;
}

// ── Result ───────────────────────────────────────────────────────────────────
function showResult(job) {
  $('rThumb').src = job.thumbnail || '';
  $('rTitle').textContent = job.title;
  const m = Math.floor(job.duration / 60), s = String(job.duration % 60).padStart(2,'0');
  $('rMeta').textContent = `${job.uploader}  ·  ${m}:${s}`;
  $('rIaLink').href = job.ia_url;
  $('resultCard').style.display = 'block';
  $('urlInput').value = '';
}

// ── Episodes list ─────────────────────────────────────────────────────────────
async function loadEpisodes() {
  const res = await fetch('/api/episodes');
  const eps = await res.json();

  if (!eps.length) return;
  $('episodesCard').style.display = 'block';

  $('epList').innerHTML = [...eps].reverse().map(ep => {
    const m = Math.floor(ep.duration / 60), s = String(ep.duration % 60).padStart(2,'0');
    return `<div class="ep-row">
      <img src="${ep.thumbnail}" alt="" onerror="this.style.display='none'"/>
      <div class="info">
        <strong>${ep.title}</strong>
        <span>${ep.uploader} · ${m}:${s}</span>
      </div>
      <a href="${ep.ia_url}" target="_blank">Archive.org ↗</a>
    </div>`;
  }).join('');
}

init();
</script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"Starting server at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
