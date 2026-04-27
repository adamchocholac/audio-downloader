"""
Local YouTube → Podcast downloader.

Run:  python app.py
Open: http://localhost:5000

On first run, a setup form asks for:
  - Internet Archive S3 keys  (archive.org/account/s3.php — free)
  - GitHub Personal Access Token (github.com/settings/tokens — needs Contents: Write)
  - GitHub repo  (e.g. adamchocholac/audio-downloader)

Flow for each video:
  1. Download audio from YouTube
  2. Convert to MP3
  3. Upload to Internet Archive (free, permanent hosting)
  4. Commit episodes.json + feed.xml to GitHub → live in Apple Podcasts via GitHub Pages
"""

import base64
import json
import re
import threading
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path

import requests as http_requests
from flask import Flask, jsonify, request, send_from_directory, abort

from downloader import download_audio, DOWNLOADS_DIR
from uploader import upload_to_archive, delete_from_archive
from feed import build_rss

app  = Flask(__name__)
PORT = int(os.environ.get("PORT", 5000))

CONFIG_FILE = Path("config.json")

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    # Environment variables override / supplement config.json (used in Docker/K8s)
    env_map = {
        "IA_ACCESS_KEY":  "ia_access_key",
        "IA_SECRET_KEY":  "ia_secret_key",
        "GITHUB_TOKEN":   "github_token",
        "GITHUB_REPO":    "github_repo",
        "PODCAST_ID":     "podcast_id",
        "PODCAST_NAME":   "podcast_name",
        "PODCAST_DESC":   "podcast_desc",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key, "").strip()
        if val:
            cfg[cfg_key] = val
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def config_ok() -> bool:
    c = load_config()
    return bool(
        c.get("ia_access_key") and c.get("ia_secret_key")
        and c.get("github_token") and c.get("github_repo")
        and c.get("podcast_id")
    )


def _pages_url(github_repo: str) -> str:
    owner, repo = github_repo.split("/", 1)
    return f"https://{owner}.github.io/{repo}"


def _extract_video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _gh_headers(token: str) -> dict:
    return {
        "Authorization":        f"Bearer {token}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get_file(base_url: str, path: str, token: str):
    """Returns (parsed_json_content, sha) or (None, None) if not found."""
    r = http_requests.get(f"{base_url}/{path}", headers=_gh_headers(token), timeout=15)
    if r.status_code == 200:
        data = r.json()
        content = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        return content, data["sha"]
    return None, None


def _gh_get_sha(base_url: str, path: str, token: str) -> str | None:
    """Returns just the SHA of a file (works for any file type, not just JSON)."""
    r = http_requests.get(f"{base_url}/{path}", headers=_gh_headers(token), timeout=15)
    if r.status_code == 200:
        return r.json()["sha"]
    return None


def _gh_put_file(base_url: str, path: str, token: str, message: str, content_bytes: bytes, sha: str | None):
    payload: dict = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode(),
    }
    if sha:
        payload["sha"] = sha
    http_requests.put(
        f"{base_url}/{path}",
        json=payload, headers=_gh_headers(token), timeout=15,
    ).raise_for_status()


def _push_to_github(episodes: list[dict], commit_msg: str, cfg: dict) -> None:
    owner, repo  = cfg["github_repo"].split("/", 1)
    token        = cfg["github_token"]
    base_url     = f"https://api.github.com/repos/{owner}/{repo}/contents"
    feed_url     = f"{_pages_url(cfg['github_repo'])}/feed.xml"

    # Active episodes only in the RSS feed
    active = [e for e in episodes if not e.get("deleted")]
    rss_bytes = build_rss(
        feed_url     = feed_url,
        podcast_name = cfg.get("podcast_name") or "My YouTube Podcast",
        podcast_desc = cfg.get("podcast_desc") or "Audio downloaded from YouTube.",
        episodes     = active,
    )

    _, ep_sha = _gh_get_file(base_url, "episodes.json", token)
    feed_sha  = _gh_get_sha(base_url, "feed.xml", token)

    _gh_put_file(base_url, "episodes.json", token, commit_msg,
                 json.dumps(episodes, indent=2, ensure_ascii=False).encode(), ep_sha)
    _gh_put_file(base_url, "feed.xml", token, commit_msg, rss_bytes, feed_sha)


def _load_episodes_from_github(cfg: dict) -> list[dict]:
    owner, repo = cfg["github_repo"].split("/", 1)
    base_url    = f"https://api.github.com/repos/{owner}/{repo}/contents"
    episodes, _ = _gh_get_file(base_url, "episodes.json", cfg["github_token"])
    return episodes or []


# ---------------------------------------------------------------------------
# Background job — add episode
# ---------------------------------------------------------------------------

def _run_job(job_id: str, url: str) -> None:
    cfg = load_config()

    def progress(info: dict) -> None:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(info)

    try:
        # Phase 1 + 2: download & convert
        result   = download_audio(url, on_progress=progress)
        mp3_path = DOWNLOADS_DIR / result.filename

        # Phase 3: upload to Internet Archive
        video_id   = _extract_video_id(url)
        identifier = f"{cfg['podcast_id']}-{video_id}"

        ia_url = upload_to_archive(
            identifier  = identifier,
            file_path   = mp3_path,
            title       = result.title,
            access_key  = cfg["ia_access_key"],
            secret_key  = cfg["ia_secret_key"],
            on_progress = progress,
        )

        # Phase 4: push to GitHub Pages
        progress({"phase": "publishing", "percent": 95})
        episode = {
            "title":         result.title,
            "filename":      result.filename,
            "ia_url":        ia_url,
            "ia_identifier": identifier,
            "duration":      result.duration,
            "uploader":      result.uploader,
            "thumbnail":     result.thumbnail,
            "description":   result.title,
            "published":     datetime.now(timezone.utc).isoformat(),
            "deleted":       False,
        }

        episodes = _load_episodes_from_github(cfg)
        if not any(e.get("filename") == episode["filename"] for e in episodes):
            episodes.append(episode)
        _push_to_github(episodes, f"Add episode: {result.title}", cfg)

        with jobs_lock:
            jobs[job_id] = {
                "status":    "done",
                "title":     result.title,
                "filename":  result.filename,
                "ia_url":    ia_url,
                "duration":  result.duration,
                "uploader":  result.uploader,
                "thumbnail": result.thumbnail,
            }

    except Exception as exc:
        with jobs_lock:
            jobs[job_id] = {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/setup")
def api_setup():
    data     = request.get_json(force=True, silent=True) or {}
    required = ["ia_access_key", "ia_secret_key", "podcast_id", "github_token", "github_repo"]
    for key in required:
        if not data.get(key, "").strip():
            return jsonify({"error": f"Missing: {key}"}), 400
    all_keys = required + ["podcast_name", "podcast_desc"]
    save_config({k: data[k].strip() for k in all_keys if data.get(k, "").strip()})
    return jsonify({"ok": True})


@app.get("/api/config-status")
def api_config_status():
    c        = load_config()
    feed_url = ""
    if c.get("github_repo"):
        feed_url = f"{_pages_url(c['github_repo'])}/feed.xml"
    return jsonify({
        "configured": config_ok(),
        "feed_url":   feed_url,
    })


@app.get("/api/episodes")
def api_get_episodes():
    if not config_ok():
        return jsonify([])
    try:
        episodes = _load_episodes_from_github(load_config())
        return jsonify(episodes)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/episodes/<path:filename>")
def api_delete_episode(filename: str):
    if not config_ok():
        return jsonify({"error": "Not configured"}), 400
    cfg = load_config()
    try:
        episodes = _load_episodes_from_github(cfg)
        target = next((e for e in episodes if e.get("filename") == filename), None)
        if not target:
            return jsonify({"error": "Episode not found"}), 404

        # Delete from Internet Archive
        identifier = target.get("ia_identifier", "")
        if identifier:
            try:
                delete_from_archive(
                    identifier = identifier,
                    filename   = filename,
                    access_key = cfg["ia_access_key"],
                    secret_key = cfg["ia_secret_key"],
                )
            except Exception:
                pass  # IA deletion best-effort; still mark deleted in feed

        # Mark as deleted (keep history)
        target["deleted"] = True
        _push_to_github(episodes, f"Delete episode: {target.get('title', filename)}", cfg)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/download")
def start_download():
    if not config_ok():
        return jsonify({"error": "Setup not complete"}), 400
    data = request.get_json(force=True, silent=True) or {}
    url  = (data.get("url") or "").strip()
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


@app.get("/api/file/<path:filename>")
def serve_file(filename: str):
    path = DOWNLOADS_DIR / filename
    if not path.exists():
        abort(404)
    return send_from_directory(
        str(DOWNLOADS_DIR.resolve()), filename,
        as_attachment=True, download_name=filename,
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>YouTube &#x2192; Podcast</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         background:#0f0f0f;color:#e8e8e8;min-height:100vh;
         display:flex;flex-direction:column;align-items:center;padding:2rem 1rem 4rem}
    .wrap{width:100%;max-width:600px}
    .header{display:flex;align-items:center;gap:.75rem;margin-bottom:1.75rem}
    h1{font-size:1.35rem;font-weight:700;color:#fff;letter-spacing:-.02em}
    h1 span{color:#ff4444}
    .card{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:14px;padding:1.5rem;margin-bottom:1.25rem}
    .card-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#666;margin-bottom:.9rem}
    label{display:block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#666;margin-bottom:.4rem}
    .field{margin-bottom:.9rem}
    input[type=text],input[type=password]{width:100%;background:#111;border:1px solid #2e2e2e;border-radius:10px;color:#e8e8e8;font-size:.9rem;padding:.6rem .9rem;outline:none;transition:border-color .15s}
    input:focus{border-color:#ff4444}
    input::placeholder{color:#444}
    .input-row{display:flex;gap:.5rem}
    .input-row input{flex:1}
    .btn-red{background:#ff4444;border:none;border-radius:10px;color:#fff;cursor:pointer;font-size:.9rem;font-weight:600;padding:.6rem 1.3rem;transition:background .15s,opacity .15s;white-space:nowrap}
    .btn-red:hover:not(:disabled){background:#e03333}
    .btn-red:disabled{opacity:.45;cursor:not-allowed}
    .hint{font-size:.72rem;color:#555;margin-top:.35rem;line-height:1.5}
    .hint a{color:#ff4444;text-decoration:none}
    .hint a:hover{text-decoration:underline}
    .section-sep{height:1px;background:#222;margin:.75rem 0}

    /* feed pill */
    .feed-row{display:flex;align-items:center;gap:.5rem;background:#111;border:1px solid #2a2a2a;border-radius:10px;padding:.55rem .75rem;margin-bottom:.5rem}
    .feed-row span{flex:1;font-family:monospace;font-size:.82rem;color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .copy-btn{background:#2a2a2a;border:none;border-radius:6px;color:#ccc;cursor:pointer;font-size:.75rem;padding:.3rem .65rem;white-space:nowrap}
    .copy-btn:hover{background:#333;color:#fff}

    /* progress */
    #progressCard{display:none}
    .phase-lbl{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#ff4444;margin-bottom:.5rem}
    .pct-row{display:flex;align-items:flex-end;gap:.3rem;margin-bottom:.65rem}
    .pct-num{font-size:2.4rem;font-weight:800;line-height:1;color:#fff;font-variant-numeric:tabular-nums}
    .pct-sym{font-size:1rem;color:#555;padding-bottom:.25rem}
    .bar-track{background:#222;border-radius:999px;height:10px;overflow:hidden;margin-bottom:.75rem}
    .bar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#c0392b,#ff4444,#ff6b6b);width:0%;transition:width .35s cubic-bezier(.4,0,.2,1);position:relative}
    .bar-fill::after{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);animation:shimmer 1.6s linear infinite}
    @keyframes shimmer{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
    .bar-fill.spin{width:35%!important;animation:slide 1.4s ease-in-out infinite}
    .bar-fill.spin::after{display:none}
    @keyframes slide{0%{transform:translateX(-120%)}100%{transform:translateX(360%)}}
    .pills{display:flex;gap:.4rem;flex-wrap:wrap}
    .pill{display:flex;align-items:center;gap:.3rem;background:#1c1c1c;border:1px solid #2e2e2e;border-radius:999px;padding:.25rem .65rem;font-size:.75rem;color:#bbb}
    .pill strong{color:#fff;font-variant-numeric:tabular-nums}

    /* result */
    #resultCard{display:none}
    .thumb-row{display:flex;gap:1rem;align-items:center;margin-bottom:.75rem}
    .thumb-row img{width:60px;height:60px;object-fit:cover;border-radius:8px;background:#222;flex-shrink:0}
    .ep-meta strong{display:block;font-size:.95rem;color:#fff}
    .ep-meta span{font-size:.78rem;color:#666}
    .badge{display:inline-flex;align-items:center;gap:.4rem;background:#1a3a1a;border:1px solid #2a5a2a;border-radius:8px;color:#4caf50;font-size:.8rem;font-weight:600;padding:.45rem .85rem;margin-top:.25rem}

    /* episode list */
    #episodesCard{display:none}
    .ep-list{display:flex;flex-direction:column;gap:.6rem}
    .ep-item{display:flex;align-items:center;gap:.75rem;background:#111;border:1px solid #2a2a2a;border-radius:10px;padding:.65rem .75rem;transition:opacity .2s}
    .ep-item.deleted{opacity:.4}
    .ep-item img{width:44px;height:44px;object-fit:cover;border-radius:6px;background:#1e1e1e;flex-shrink:0}
    .ep-info{flex:1;min-width:0}
    .ep-info strong{display:block;font-size:.85rem;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .ep-info span{font-size:.72rem;color:#555}
    .ep-item.deleted .ep-info strong{text-decoration:line-through;color:#666}
    .del-badge{font-size:.65rem;background:#2a1111;border:1px solid #5c2020;border-radius:5px;color:#884444;padding:.15rem .4rem;flex-shrink:0}
    .btn-del{background:none;border:1px solid #3a1a1a;border-radius:7px;color:#cc4444;cursor:pointer;font-size:.75rem;padding:.3rem .6rem;white-space:nowrap;transition:background .15s,color .15s}
    .btn-del:hover:not(:disabled){background:#3a1a1a;color:#ff6666}
    .btn-del:disabled{opacity:.4;cursor:not-allowed}
    .ep-list-empty{font-size:.82rem;color:#555;text-align:center;padding:.5rem 0}

    /* error */
    .error-box{display:none;margin-top:1rem;background:#2a1111;border:1px solid #5c2020;border-radius:10px;color:#ff7070;font-size:.82rem;padding:.7rem 1rem}
  </style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <svg width="34" height="34" viewBox="0 0 34 34" fill="none">
      <rect width="34" height="34" rx="9" fill="#ff4444"/>
      <path d="M13 11l10 6-10 6V11z" fill="white"/>
    </svg>
    <h1><span>YouTube</span> &#x2192; Apple Podcasts</h1>
  </div>

  <!-- Setup -->
  <div class="card" id="setupCard" style="display:none">
    <div class="card-title">Setup</div>

    <div class="field">
      <label>Podcast Name</label>
      <input type="text" id="cfgPodcastName" placeholder="My YouTube Podcast" value="My YouTube Podcast"/>
    </div>
    <div class="field">
      <label>Podcast Description</label>
      <input type="text" id="cfgPodcastDesc" placeholder="Audio downloaded from YouTube." value="Audio downloaded from YouTube."/>
    </div>
    <div class="field">
      <label>Podcast ID <small style="text-transform:none;letter-spacing:0;font-weight:400;color:#555">(unique slug)</small></label>
      <input type="text" id="cfgPodcastId" placeholder="my-podcast"/>
      <div class="hint">Used as the Archive.org item prefix. Must be globally unique.</div>
    </div>

    <div class="section-sep"></div>

    <div class="field">
      <label>Archive.org Access Key</label>
      <input type="text" id="cfgIaAccess" placeholder="Get from archive.org/account/s3.php"/>
      <div class="hint"><a href="https://archive.org/account/s3.php" target="_blank">archive.org/account/s3.php</a> &rarr; Generate new keys</div>
    </div>
    <div class="field">
      <label>Archive.org Secret Key</label>
      <input type="password" id="cfgIaSecret"/>
    </div>

    <div class="section-sep"></div>

    <div class="field">
      <label>GitHub Repository</label>
      <input type="text" id="cfgGhRepo" placeholder="username/repo-name"/>
    </div>
    <div class="field">
      <label>GitHub Personal Access Token</label>
      <input type="password" id="cfgGhToken" placeholder="github_pat_..."/>
      <div class="hint">
        <a href="https://github.com/settings/tokens?type=beta" target="_blank">github.com/settings/tokens</a>
        &rarr; Fine-grained &rarr; Contents: Read &amp; Write
      </div>
    </div>

    <button class="btn-red" id="saveBtn">Save &amp; Continue</button>
    <div class="error-box" id="setupError"></div>
  </div>

  <!-- Feed URL -->
  <div class="card" id="feedCard" style="display:none">
    <div class="card-title">Your Podcast Feed (Apple Podcasts)</div>
    <div class="feed-row">
      <span id="feedUrl"></span>
      <button class="copy-btn" onclick="copyFeed()">Copy</button>
    </div>
    <p class="hint">In Apple Podcasts: <strong>File &rarr; Follow a Show by URL</strong>.</p>
  </div>

  <!-- Add Episode -->
  <div class="card" id="downloadCard" style="display:none">
    <div class="card-title">Add Episode</div>
    <label for="urlInput">YouTube URL</label>
    <div class="input-row">
      <input type="text" id="urlInput" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" spellcheck="false"/>
      <button class="btn-red" id="dlBtn">Add Episode</button>
    </div>
    <div class="error-box" id="dlError"></div>
  </div>

  <!-- Progress -->
  <div class="card" id="progressCard">
    <div class="phase-lbl" id="phaseLabel">Starting&#x2026;</div>
    <div class="pct-row">
      <span class="pct-num" id="pctNum">0</span><span class="pct-sym">%</span>
    </div>
    <div class="bar-track"><div class="bar-fill spin" id="barFill"></div></div>
    <div class="pills" id="pills"></div>
  </div>

  <!-- Result -->
  <div class="card" id="resultCard">
    <div class="card-title">Added to Podcast</div>
    <div class="thumb-row">
      <img id="rThumb" src="" alt=""/>
      <div class="ep-meta">
        <strong id="rTitle"></strong>
        <span id="rMeta"></span>
      </div>
    </div>
    <div class="badge">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M2 7l3.5 3.5L12 3" stroke="#4caf50" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Live in Apple Podcasts &mdash; refresh your feed
    </div>
  </div>

  <!-- Episode List -->
  <div class="card" id="episodesCard">
    <div class="card-title" style="display:flex;align-items:center;justify-content:space-between">
      <span>Episode History</span>
      <button class="copy-btn" onclick="loadEpisodes()" id="refreshBtn">Refresh</button>
    </div>
    <div class="ep-list" id="epList">
      <div class="ep-list-empty">Loading&#x2026;</div>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

async function init() {
  const cfg = await (await fetch('/api/config-status')).json();
  if (!cfg.configured) {
    $('setupCard').style.display = 'block';
  } else {
    showMain(cfg.feed_url);
  }
}

function showMain(feedUrl) {
  $('feedUrl').textContent = feedUrl;
  $('feedCard').style.display = 'block';
  $('downloadCard').style.display = 'block';
  $('episodesCard').style.display = 'block';
  loadEpisodes();
}

function copyFeed() {
  navigator.clipboard.writeText($('feedUrl').textContent);
  const b = document.querySelector('.copy-btn');
  b.textContent = 'Copied!';
  setTimeout(() => b.textContent = 'Copy', 2000);
}

$('saveBtn').addEventListener('click', async () => {
  const err = $('setupError');
  err.style.display = 'none';
  const body = {
    podcast_name:  $('cfgPodcastName').value.trim(),
    podcast_desc:  $('cfgPodcastDesc').value.trim(),
    podcast_id:    $('cfgPodcastId').value.trim(),
    ia_access_key: $('cfgIaAccess').value.trim(),
    ia_secret_key: $('cfgIaSecret').value.trim(),
    github_repo:   $('cfgGhRepo').value.trim(),
    github_token:  $('cfgGhToken').value.trim(),
  };
  const res = await fetch('/api/setup', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) { err.textContent = data.error; err.style.display='block'; return; }
  $('setupCard').style.display = 'none';
  const [owner, repo] = body.github_repo.split('/');
  showMain(`https://${owner}.github.io/${repo}/feed.xml`);
});

$('dlBtn').addEventListener('click', startDl);
$('urlInput').addEventListener('keydown', e => { if (e.key === 'Enter') startDl(); });

async function startDl() {
  const url = $('urlInput').value.trim();
  if (!url) { $('urlInput').focus(); return; }
  $('dlError').style.display = 'none';
  $('resultCard').style.display = 'none';
  $('dlBtn').disabled = true;
  setProgress(0, 'Fetching video info\u2026', '', '');
  $('progressCard').style.display = 'block';

  try {
    const res = await fetch('/api/download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})});
    const data = await res.json();
    if (!res.ok) { done(data.error); return; }
    poll(data.job_id);
  } catch(e) { done('Server error: ' + e.message); }
}

async function poll(jobId) {
  const iv = setInterval(async () => {
    try {
      const j = await (await fetch('/api/status/' + jobId)).json();
      if (j.status === 'done') {
        clearInterval(iv); done(null); showResult(j); loadEpisodes();
      } else if (j.status === 'error') {
        clearInterval(iv); done('Error: ' + j.error);
      } else if (j.phase === 'downloading') {
        setProgress(j.percent||0, 'Downloading audio\u2026', j.speed||'', j.eta||'');
      } else if (j.phase === 'converting') {
        setProgress(j.percent||0, 'Converting to MP3\u2026', '', '');
      } else if (j.phase === 'uploading') {
        setProgress(j.percent||0, 'Uploading to Archive.org\u2026', '', '');
      } else if (j.phase === 'publishing') {
        setProgress(j.percent||95, 'Publishing to GitHub\u2026', '', '');
      } else {
        setProgress(0, 'Fetching video info\u2026', '', '');
      }
    } catch { clearInterval(iv); done('Lost connection.'); }
  }, 500);
}

function done(err) {
  $('dlBtn').disabled = false;
  $('progressCard').style.display = 'none';
  if (err) { $('dlError').textContent = err; $('dlError').style.display = 'block'; }
}

function setProgress(pct, phase, speed, eta) {
  const bar = $('barFill');
  if (pct > 0) {
    bar.classList.remove('spin'); bar.style.width = pct + '%';
    $('pctNum').textContent = Math.floor(pct);
  } else {
    bar.classList.add('spin'); bar.style.width = '';
    $('pctNum').textContent = '0';
  }
  $('phaseLabel').textContent = phase;
  $('pills').innerHTML = [
    speed ? `<div class="pill">Speed <strong>${speed}</strong></div>` : '',
    eta   ? `<div class="pill">ETA <strong>${eta}</strong></div>`   : '',
  ].join('');
}

function showResult(j) {
  $('rThumb').src = j.thumbnail || '';
  $('rTitle').textContent = j.title;
  const m = Math.floor(j.duration/60), s = String(j.duration%60).padStart(2,'0');
  $('rMeta').textContent = j.uploader + '  \u00b7  ' + m + ':' + s;
  $('resultCard').style.display = 'block';
  $('urlInput').value = '';
}

async function loadEpisodes() {
  const list = $('epList');
  list.innerHTML = '<div class="ep-list-empty">Loading\u2026</div>';
  try {
    const episodes = await (await fetch('/api/episodes')).json();
    if (!Array.isArray(episodes) || episodes.length === 0) {
      list.innerHTML = '<div class="ep-list-empty">No episodes yet.</div>';
      return;
    }
    // Show newest first
    list.innerHTML = [...episodes].reverse().map(ep => {
      const deleted = !!ep.deleted;
      const m = Math.floor((ep.duration||0)/60), s = String((ep.duration||0)%60).padStart(2,'0');
      const date = ep.published ? new Date(ep.published).toLocaleDateString() : '';
      return `
        <div class="ep-item${deleted?' deleted':''}" id="ep-${encodeURIComponent(ep.filename)}">
          <img src="${ep.thumbnail||''}" alt="" onerror="this.style.display='none'"/>
          <div class="ep-info">
            <strong title="${ep.title.replace(/"/g,'&quot;')}">${ep.title}</strong>
            <span>${ep.uploader||''} &middot; ${m}:${s.padStart(2,'0')} &middot; ${date}</span>
          </div>
          ${deleted
            ? '<span class="del-badge">Deleted</span>'
            : `<button class="btn-del" onclick="deleteEpisode('${ep.filename.replace(/'/g,"\\'")}', this)">Delete</button>`
          }
        </div>`;
    }).join('');
  } catch(e) {
    list.innerHTML = '<div class="ep-list-empty">Failed to load episodes.</div>';
  }
}

async function deleteEpisode(filename, btn) {
  if (!confirm('Delete this episode? The audio will be removed from Archive.org and the podcast feed.')) return;
  btn.disabled = true;
  btn.textContent = 'Deleting\u2026';
  try {
    const res = await fetch('/api/episodes/' + encodeURIComponent(filename), {method:'DELETE'});
    const data = await res.json();
    if (!res.ok) { alert('Error: ' + data.error); btn.disabled = false; btn.textContent = 'Delete'; return; }
    loadEpisodes();
  } catch(e) {
    alert('Network error: ' + e.message);
    btn.disabled = false;
    btn.textContent = 'Delete';
  }
}

init();
</script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"Starting local app at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
