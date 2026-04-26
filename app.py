"""
YouTube Audio Downloader — web app entry point.

Run:
    python app.py

Then open http://localhost:5000 in your browser.
"""

import threading
import uuid
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, abort

from downloader import download_audio, DOWNLOADS_DIR

app = Flask(__name__)
PORT = int(os.environ.get("PORT", 5000))

# In-memory job store  { job_id: { status, result, error } }
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_download(job_id: str, url: str) -> None:
    def on_progress(info: dict) -> None:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(info)

    try:
        result = download_audio(url, on_progress=on_progress)
        with jobs_lock:
            jobs[job_id] = {
                "status": "done",
                "title": result.title,
                "filename": result.filename,
                "duration": result.duration,
                "uploader": result.uploader,
                "thumbnail": result.thumbnail,
            }
    except Exception as exc:
        with jobs_lock:
            jobs[job_id] = {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/download")
def start_download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status": "pending"}

    thread = threading.Thread(target=_run_download, args=(job_id, url), daemon=True)
    thread.start()

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
    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists():
        abort(404)
    return send_from_directory(
        str(DOWNLOADS_DIR.resolve()),
        filename,
        as_attachment=True,
        download_name=filename,
    )


# ---------------------------------------------------------------------------
# Single-page app
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>YouTube Audio Downloader</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f0f0f;
      color: #e8e8e8;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
    }

    .card {
      background: #1a1a1a;
      border: 1px solid #2e2e2e;
      border-radius: 16px;
      padding: 2.5rem 2rem;
      width: 100%;
      max-width: 560px;
      box-shadow: 0 8px 40px rgba(0,0,0,.5);
    }

    .logo {
      display: flex;
      align-items: center;
      gap: .75rem;
      margin-bottom: 2rem;
    }

    .logo svg { flex-shrink: 0; }

    h1 {
      font-size: 1.4rem;
      font-weight: 700;
      letter-spacing: -.02em;
      color: #fff;
    }

    h1 span { color: #ff4444; }

    label {
      display: block;
      font-size: .8rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: #888;
      margin-bottom: .5rem;
    }

    .input-row {
      display: flex;
      gap: .5rem;
    }

    input[type=text] {
      flex: 1;
      background: #111;
      border: 1px solid #333;
      border-radius: 10px;
      color: #e8e8e8;
      font-size: .95rem;
      padding: .65rem 1rem;
      outline: none;
      transition: border-color .15s;
    }

    input[type=text]:focus { border-color: #ff4444; }
    input[type=text]::placeholder { color: #555; }

    button#downloadBtn {
      background: #ff4444;
      border: none;
      border-radius: 10px;
      color: #fff;
      cursor: pointer;
      font-size: .95rem;
      font-weight: 600;
      padding: .65rem 1.4rem;
      transition: background .15s, opacity .15s;
      white-space: nowrap;
    }

    button#downloadBtn:hover:not(:disabled) { background: #e03333; }
    button#downloadBtn:disabled { opacity: .5; cursor: not-allowed; }

    /* Status area */
    #status {
      margin-top: 1.75rem;
      display: none;
      background: #111;
      border: 1px solid #2a2a2a;
      border-radius: 14px;
      padding: 1.25rem 1.25rem 1rem;
    }

    .phase-label {
      font-size: .72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: #ff4444;
      margin-bottom: .6rem;
    }

    /* big percentage */
    .pct-row {
      display: flex;
      align-items: flex-end;
      gap: .4rem;
      margin-bottom: .75rem;
    }

    .pct-number {
      font-size: 2.6rem;
      font-weight: 800;
      line-height: 1;
      color: #fff;
      letter-spacing: -.04em;
      font-variant-numeric: tabular-nums;
      transition: color .2s;
    }

    .pct-symbol {
      font-size: 1.1rem;
      font-weight: 600;
      color: #666;
      padding-bottom: .3rem;
    }

    /* bar */
    .progress-bar-track {
      background: #222;
      border-radius: 999px;
      height: 10px;
      overflow: hidden;
      margin-bottom: .85rem;
      position: relative;
    }

    .progress-bar-fill {
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #c0392b, #ff4444, #ff6b6b);
      width: 0%;
      transition: width .4s cubic-bezier(.4,0,.2,1);
      position: relative;
    }

    /* shimmer overlay */
    .progress-bar-fill::after {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,.18) 50%, transparent 100%);
      animation: shimmer 1.6s linear infinite;
    }

    @keyframes shimmer {
      0%   { transform: translateX(-100%); }
      100% { transform: translateX(100%); }
    }

    .progress-bar-fill.indeterminate {
      width: 35% !important;
      animation: slide 1.4s ease-in-out infinite;
    }

    .progress-bar-fill.indeterminate::after { display: none; }

    @keyframes slide {
      0%   { transform: translateX(-120%); }
      100% { transform: translateX(360%); }
    }

    /* stats row */
    .stats-row {
      display: flex;
      gap: .5rem;
      flex-wrap: wrap;
    }

    .stat-pill {
      display: flex;
      align-items: center;
      gap: .35rem;
      background: #1c1c1c;
      border: 1px solid #2e2e2e;
      border-radius: 999px;
      padding: .3rem .75rem;
      font-size: .78rem;
      color: #bbb;
    }

    .stat-pill svg { flex-shrink: 0; opacity: .6; }
    .stat-pill strong { color: #fff; font-weight: 600; font-variant-numeric: tabular-nums; }

    /* Result card */
    #result {
      display: none;
      margin-top: 1.5rem;
      background: #111;
      border: 1px solid #2a2a2a;
      border-radius: 12px;
      padding: 1rem 1.25rem;
    }

    #result .thumb-row {
      display: flex;
      gap: 1rem;
      align-items: center;
      margin-bottom: 1rem;
    }

    #result img {
      width: 72px;
      height: 72px;
      object-fit: cover;
      border-radius: 8px;
      flex-shrink: 0;
      background: #222;
    }

    #result .meta { overflow: hidden; }

    #result .meta strong {
      display: block;
      font-size: .95rem;
      color: #fff;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    #result .meta span {
      font-size: .8rem;
      color: #777;
    }

    #result a.dl-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: .5rem;
      background: #1db954;
      border-radius: 10px;
      color: #fff;
      font-weight: 600;
      font-size: .9rem;
      padding: .65rem 1rem;
      text-decoration: none;
      transition: background .15s;
    }

    #result a.dl-btn:hover { background: #17a349; }

    #errorMsg {
      display: none;
      margin-top: 1.25rem;
      background: #2a1111;
      border: 1px solid #5c2020;
      border-radius: 10px;
      color: #ff7070;
      font-size: .85rem;
      padding: .75rem 1rem;
    }

    footer {
      margin-top: 1.5rem;
      font-size: .75rem;
      color: #444;
      text-align: center;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
        <rect width="36" height="36" rx="10" fill="#ff4444"/>
        <path d="M14 11.5l11 6.5-11 6.5V11.5z" fill="white"/>
      </svg>
      <h1><span>YouTube</span> Audio Downloader</h1>
    </div>

    <label for="urlInput">YouTube URL</label>
    <div class="input-row">
      <input type="text" id="urlInput"
             placeholder="https://www.youtube.com/watch?v=..."
             autocomplete="off" spellcheck="false"/>
      <button id="downloadBtn">Download</button>
    </div>

    <div id="status">
      <div class="phase-label" id="phaseLabel">Fetching video info</div>
      <div class="pct-row">
        <span class="pct-number" id="pctNumber">0</span>
        <span class="pct-symbol">%</span>
      </div>
      <div class="progress-bar-track">
        <div class="progress-bar-fill indeterminate" id="progressBar"></div>
      </div>
      <div class="stats-row" id="statsRow">
        <div class="stat-pill" id="pillSpeed" style="display:none">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M6 1a5 5 0 1 1 0 10A5 5 0 0 1 6 1zm0 2v3l2 1" stroke="#ff4444" stroke-width="1.4" stroke-linecap="round"/>
          </svg>
          Speed: <strong id="speedVal">—</strong>
        </div>
        <div class="stat-pill" id="pillEta" style="display:none">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M6 2v4l2.5 2.5M6 1a5 5 0 1 1 0 10A5 5 0 0 1 6 1z" stroke="#aaa" stroke-width="1.4" stroke-linecap="round"/>
          </svg>
          Remaining: <strong id="etaVal">—</strong>
        </div>
      </div>
    </div>

    <div id="result">
      <div class="thumb-row">
        <img id="thumbnail" src="" alt="thumbnail"/>
        <div class="meta">
          <strong id="trackTitle"></strong>
          <span id="trackMeta"></span>
        </div>
      </div>
      <a class="dl-btn" id="dlLink" href="#" download>
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M8 1v9m0 0L5 7m3 3 3-3M2 13h12" stroke="white" stroke-width="1.6"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        Save MP3
      </a>
    </div>

    <div id="errorMsg"></div>
  </div>

  <footer>Supports any public YouTube video &nbsp;·&nbsp; Output: MP3 VBR</footer>

  <script>
    const urlInput    = document.getElementById('urlInput');
    const downloadBtn = document.getElementById('downloadBtn');
    const statusEl    = document.getElementById('status');
    const resultEl    = document.getElementById('result');
    const errorMsg    = document.getElementById('errorMsg');
    const progressBar = document.getElementById('progressBar');
    const pctNumber   = document.getElementById('pctNumber');
    const phaseLabel  = document.getElementById('phaseLabel');
    const pillSpeed   = document.getElementById('pillSpeed');
    const pillEta     = document.getElementById('pillEta');
    const speedVal    = document.getElementById('speedVal');
    const etaVal      = document.getElementById('etaVal');

    function setLoading(on) {
      downloadBtn.disabled = on;
      statusEl.style.display = on ? 'block' : 'none';
    }

    function showError(msg) {
      errorMsg.textContent = msg;
      errorMsg.style.display = 'block';
    }

    function hideError() {
      errorMsg.style.display = 'none';
    }

    function showResult(job) {
      document.getElementById('thumbnail').src = job.thumbnail || '';
      document.getElementById('trackTitle').textContent = job.title;
      const mins = Math.floor(job.duration / 60);
      const secs = String(job.duration % 60).padStart(2, '0');
      document.getElementById('trackMeta').textContent =
        `${job.uploader}  ·  ${mins}:${secs}`;
      const link = document.getElementById('dlLink');
      link.href = `/api/file/${encodeURIComponent(job.filename)}`;
      link.download = job.filename;
      resultEl.style.display = 'block';
    }

    function setProgress(percent, phase, speed, eta) {
      if (percent > 0) {
        progressBar.classList.remove('indeterminate');
        progressBar.style.width = percent + '%';
        pctNumber.textContent = Math.floor(percent);
      } else {
        progressBar.classList.add('indeterminate');
        progressBar.style.width = '';
        pctNumber.textContent = '0';
      }

      phaseLabel.textContent = phase;

      if (speed) {
        pillSpeed.style.display = 'flex';
        speedVal.textContent = speed;
      } else {
        pillSpeed.style.display = 'none';
      }

      if (eta) {
        pillEta.style.display = 'flex';
        etaVal.textContent = eta;
      } else {
        pillEta.style.display = 'none';
      }
    }

    async function poll(jobId) {
      setProgress(0, 'Fetching video info…', '', '');

      const interval = setInterval(async () => {
        try {
          const res = await fetch(`/api/status/${jobId}`);
          const job = await res.json();

          if (job.status === 'done') {
            clearInterval(interval);
            setLoading(false);
            statusEl.style.display = 'none';
            showResult(job);
          } else if (job.status === 'error') {
            clearInterval(interval);
            setLoading(false);
            showError('Download failed: ' + job.error);
          } else if (job.phase === 'downloading') {
            const pct = job.percent || 0;
            const eta = job.eta ? job.eta : '';
            setProgress(pct, 'Downloading audio…', job.speed || '', eta);
          } else if (job.phase === 'converting') {
            const pct = job.percent || 0;
            setProgress(pct, 'Converting to MP3…', '', '');
          } else {
            setProgress(0, 'Fetching video info…', '', '');
          }
        } catch {
          clearInterval(interval);
          setLoading(false);
          showError('Lost connection to server.');
        }
      }, 500);
    }

    downloadBtn.addEventListener('click', async () => {
      const url = urlInput.value.trim();
      if (!url) { urlInput.focus(); return; }

      hideError();
      resultEl.style.display = 'none';
      setLoading(true);

      try {
        const res = await fetch('/api/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url }),
        });
        const data = await res.json();
        if (!res.ok) {
          setLoading(false);
          showError(data.error || 'Unknown error');
          return;
        }
        poll(data.job_id);
      } catch (err) {
        setLoading(false);
        showError('Could not reach server: ' + err.message);
      }
    });

    urlInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') downloadBtn.click();
    });
  </script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"Starting server at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
