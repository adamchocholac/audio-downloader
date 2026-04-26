"""
YouTube → Apple Podcasts bridge.

Run:  python app.py
Open: http://localhost:5000

Paste a YouTube URL → the audio is downloaded, added to the RSS feed,
and appears in Apple Podcasts automatically.

Subscribe once in Apple Podcasts:
  Mac:    File → Follow a Show by URL → http://localhost:5000/feed.xml
  iPhone: use your local network IP shown on the page (same Wi-Fi required)
"""

import socket
import threading
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, abort

from downloader import download_audio, DOWNLOADS_DIR
from feed import build_rss, save_episode, load_episodes

app  = Flask(__name__)
PORT = int(os.environ.get("PORT", 5000))


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


HOST = os.environ.get("HOST_URL", f"http://{local_ip()}:{PORT}")

# In-memory job store
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _run_job(job_id: str, url: str) -> None:
    def on_progress(info: dict) -> None:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(info)

    try:
        result = download_audio(url, on_progress=on_progress)

        save_episode({
            "title":       result.title,
            "filename":    result.filename,
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
                "duration": result.duration,
                "uploader": result.uploader,
                "thumbnail":result.thumbnail,
            }

    except Exception as exc:
        with jobs_lock:
            jobs[job_id] = {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.post("/api/download")
def start_download():
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


@app.get("/api/episodes")
def api_episodes():
    return jsonify(load_episodes())


@app.get("/feed.xml")
def rss_feed():
    return Response(build_rss(HOST), mimetype="application/rss+xml")


@app.get("/audio/<path:filename>")
def serve_audio(filename: str):
    path = DOWNLOADS_DIR / filename
    if not path.exists():
        abort(404)
    return send_from_directory(str(DOWNLOADS_DIR.resolve()), filename)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    feed_url = f"{HOST}/feed.xml"
    episodes = load_episodes()

    ep_rows = "".join(
        f"""<div class="ep-row">
          <img src="{ep['thumbnail']}" alt="" onerror="this.style.display='none'"/>
          <div class="info">
            <strong>{ep['title']}</strong>
            <span>{ep['uploader']} &nbsp;·&nbsp; {ep['duration']//60}:{ep['duration']%60:02d}</span>
          </div>
        </div>"""
        for ep in reversed(episodes)
    ) or '<p class="empty">No episodes yet. Paste a YouTube URL above to add the first one.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>YouTube → Podcast</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          background:#0f0f0f;color:#e8e8e8;min-height:100vh;
          display:flex;flex-direction:column;align-items:center;padding:2rem 1rem 4rem}}
    .wrap{{width:100%;max-width:600px}}
    .header{{display:flex;align-items:center;gap:.75rem;margin-bottom:1.75rem}}
    h1{{font-size:1.35rem;font-weight:700;color:#fff;letter-spacing:-.02em}}
    h1 span{{color:#ff4444}}
    .card{{background:#1a1a1a;border:1px solid #2e2e2e;border-radius:14px;padding:1.5rem;margin-bottom:1.25rem}}
    .card-title{{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#666;margin-bottom:.9rem}}

    /* feed URL */
    .feed-row{{display:flex;align-items:center;gap:.5rem;background:#111;border:1px solid #2a2a2a;border-radius:10px;padding:.55rem .75rem}}
    .feed-row span{{flex:1;font-family:monospace;font-size:.82rem;color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .copy-btn{{background:#2a2a2a;border:none;border-radius:6px;color:#ccc;cursor:pointer;font-size:.75rem;padding:.3rem .65rem;white-space:nowrap;transition:background .15s}}
    .copy-btn:hover{{background:#333;color:#fff}}
    .feed-hint{{font-size:.74rem;color:#555;margin-top:.55rem;line-height:1.5}}
    .feed-hint strong{{color:#888}}

    /* input */
    label{{display:block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#666;margin-bottom:.5rem}}
    .input-row{{display:flex;gap:.5rem}}
    input[type=text]{{flex:1;background:#111;border:1px solid #2e2e2e;border-radius:10px;color:#e8e8e8;font-size:.9rem;padding:.6rem .9rem;outline:none;transition:border-color .15s}}
    input:focus{{border-color:#ff4444}}
    input::placeholder{{color:#444}}
    .btn-red{{background:#ff4444;border:none;border-radius:10px;color:#fff;cursor:pointer;font-size:.9rem;font-weight:600;padding:.6rem 1.3rem;transition:background .15s,opacity .15s;white-space:nowrap}}
    .btn-red:hover:not(:disabled){{background:#e03333}}
    .btn-red:disabled{{opacity:.45;cursor:not-allowed}}

    /* progress */
    #progressCard{{display:none}}
    .phase-lbl{{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#ff4444;margin-bottom:.5rem}}
    .pct-row{{display:flex;align-items:flex-end;gap:.3rem;margin-bottom:.65rem}}
    .pct-num{{font-size:2.4rem;font-weight:800;line-height:1;color:#fff;font-variant-numeric:tabular-nums}}
    .pct-sym{{font-size:1rem;color:#555;padding-bottom:.25rem}}
    .bar-track{{background:#222;border-radius:999px;height:10px;overflow:hidden;margin-bottom:.75rem}}
    .bar-fill{{height:100%;border-radius:999px;background:linear-gradient(90deg,#c0392b,#ff4444,#ff6b6b);width:0%;transition:width .35s cubic-bezier(.4,0,.2,1);position:relative}}
    .bar-fill::after{{content:'';position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.18),transparent);animation:shimmer 1.6s linear infinite}}
    @keyframes shimmer{{0%{{transform:translateX(-100%)}}100%{{transform:translateX(100%)}}}}
    .bar-fill.spin{{width:35%!important;animation:slide 1.4s ease-in-out infinite}}
    .bar-fill.spin::after{{display:none}}
    @keyframes slide{{0%{{transform:translateX(-120%)}}100%{{transform:translateX(360%)}}}}
    .pills{{display:flex;gap:.4rem;flex-wrap:wrap}}
    .pill{{display:flex;align-items:center;gap:.3rem;background:#1c1c1c;border:1px solid #2e2e2e;border-radius:999px;padding:.25rem .65rem;font-size:.75rem;color:#bbb}}
    .pill strong{{color:#fff;font-variant-numeric:tabular-nums}}

    /* result */
    #resultCard{{display:none}}
    .thumb-row{{display:flex;gap:1rem;align-items:center;margin-bottom:.75rem}}
    .thumb-row img{{width:60px;height:60px;object-fit:cover;border-radius:8px;background:#222;flex-shrink:0}}
    .ep-meta strong{{display:block;font-size:.95rem;color:#fff}}
    .ep-meta span{{font-size:.78rem;color:#666}}
    .added-badge{{display:inline-flex;align-items:center;gap:.4rem;background:#1a3a1a;border:1px solid #2a5a2a;border-radius:8px;color:#4caf50;font-size:.8rem;font-weight:600;padding:.45rem .85rem;margin-top:.25rem}}

    /* error */
    .error-box{{display:none;margin-top:1rem;background:#2a1111;border:1px solid #5c2020;border-radius:10px;color:#ff7070;font-size:.82rem;padding:.7rem 1rem}}

    /* episodes */
    .ep-row{{display:flex;align-items:center;gap:.75rem;padding:.65rem 0;border-bottom:1px solid #1e1e1e}}
    .ep-row:last-child{{border-bottom:none}}
    .ep-row img{{width:44px;height:44px;border-radius:6px;object-fit:cover;background:#222;flex-shrink:0}}
    .ep-row .info{{flex:1;overflow:hidden}}
    .ep-row .info strong{{display:block;font-size:.85rem;color:#e0e0e0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
    .ep-row .info span{{font-size:.73rem;color:#555}}
    .empty{{color:#444;font-size:.85rem;text-align:center;padding:1.5rem 0}}
  </style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <svg width="34" height="34" viewBox="0 0 34 34" fill="none">
      <rect width="34" height="34" rx="9" fill="#ff4444"/>
      <path d="M13 11l10 6-10 6V11z" fill="white"/>
    </svg>
    <h1><span>YouTube</span> &rarr; Apple Podcasts</h1>
  </div>

  <!-- Feed URL -->
  <div class="card">
    <div class="card-title">Your Podcast Feed</div>
    <div class="feed-row">
      <span id="feedUrl">{feed_url}</span>
      <button class="copy-btn" onclick="copyFeed()">Copy</button>
    </div>
    <p class="feed-hint">
      <strong>Mac:</strong> Apple Podcasts &rarr; File &rarr; Follow a Show by URL &rarr; paste this URL.<br>
      <strong>iPhone:</strong> Make sure your phone is on the same Wi-Fi, then use the URL above in Apple Podcasts.
    </p>
  </div>

  <!-- Download -->
  <div class="card">
    <div class="card-title">Add Episode from YouTube</div>
    <label for="urlInput">YouTube URL</label>
    <div class="input-row">
      <input type="text" id="urlInput" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off" spellcheck="false"/>
      <button class="btn-red" id="dlBtn">Add Episode</button>
    </div>
    <div class="error-box" id="dlError"></div>
  </div>

  <!-- Progress -->
  <div class="card" id="progressCard">
    <div class="phase-lbl" id="phaseLabel">Starting…</div>
    <div class="pct-row">
      <span class="pct-num" id="pctNum">0</span>
      <span class="pct-sym">%</span>
    </div>
    <div class="bar-track"><div class="bar-fill spin" id="barFill"></div></div>
    <div class="pills" id="pills"></div>
  </div>

  <!-- Result -->
  <div class="card" id="resultCard">
    <div class="card-title">Episode Added to Feed</div>
    <div class="thumb-row">
      <img id="rThumb" src="" alt=""/>
      <div class="ep-meta">
        <strong id="rTitle"></strong>
        <span id="rMeta"></span>
      </div>
    </div>
    <div class="added-badge">
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M2 7l3.5 3.5L12 3" stroke="#4caf50" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      Available in Apple Podcasts — refresh your feed
    </div>
  </div>

  <!-- Episodes -->
  <div class="card">
    <div class="card-title">Episodes ({len(episodes)})</div>
    <div id="epList">{ep_rows}</div>
  </div>

</div>
<script>
  const $ = id => document.getElementById(id);

  function copyFeed() {{
    navigator.clipboard.writeText($('feedUrl').textContent);
    const b = document.querySelector('.copy-btn');
    b.textContent = 'Copied!';
    setTimeout(() => b.textContent = 'Copy', 2000);
  }}

  $('dlBtn').addEventListener('click', start);
  $('urlInput').addEventListener('keydown', e => {{ if (e.key === 'Enter') start(); }});

  async function start() {{
    const url = $('urlInput').value.trim();
    if (!url) {{ $('urlInput').focus(); return; }}

    $('dlError').style.display = 'none';
    $('resultCard').style.display = 'none';
    $('dlBtn').disabled = true;
    setProgress(0, 'Fetching video info…', '', '');
    $('progressCard').style.display = 'block';

    try {{
      const res  = await fetch('/api/download', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{url}}),
      }});
      const data = await res.json();
      if (!res.ok) {{ done(data.error); return; }}
      poll(data.job_id);
    }} catch(e) {{ done('Server error: ' + e.message); }}
  }}

  async function poll(jobId) {{
    const iv = setInterval(async () => {{
      try {{
        const j = await (await fetch('/api/status/' + jobId)).json();
        if (j.status === 'done') {{
          clearInterval(iv);
          done(null);
          showResult(j);
          setTimeout(() => location.reload(), 3000);
        }} else if (j.status === 'error') {{
          clearInterval(iv); done('Error: ' + j.error);
        }} else if (j.phase === 'downloading') {{
          setProgress(j.percent||0, 'Downloading audio…', j.speed||'', j.eta||'');
        }} else if (j.phase === 'converting') {{
          setProgress(j.percent||0, 'Converting to MP3…', '', '');
        }} else {{
          setProgress(0, 'Fetching video info…', '', '');
        }}
      }} catch {{ clearInterval(iv); done('Lost connection.'); }}
    }}, 500);
  }}

  function done(err) {{
    $('dlBtn').disabled = false;
    $('progressCard').style.display = 'none';
    if (err) {{ $('dlError').textContent = err; $('dlError').style.display = 'block'; }}
  }}

  function setProgress(pct, phase, speed, eta) {{
    const bar = $('barFill');
    if (pct > 0) {{
      bar.classList.remove('spin'); bar.style.width = pct + '%';
      $('pctNum').textContent = Math.floor(pct);
    }} else {{
      bar.classList.add('spin'); bar.style.width = '';
      $('pctNum').textContent = '0';
    }}
    $('phaseLabel').textContent = phase;
    $('pills').innerHTML = [
      speed ? `<div class="pill">Speed <strong>${{speed}}</strong></div>` : '',
      eta   ? `<div class="pill">ETA <strong>${{eta}}</strong></div>`   : '',
    ].join('');
  }}

  function showResult(j) {{
    $('rThumb').src = j.thumbnail || '';
    $('rTitle').textContent = j.title;
    const m = Math.floor(j.duration/60), s = String(j.duration%60).padStart(2,'0');
    $('rMeta').textContent = j.uploader + '  ·  ' + m + ':' + s;
    $('resultCard').style.display = 'block';
    $('urlInput').value = '';
  }}
</script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"Starting server at http://localhost:{PORT}")
    print(f"Podcast feed:   {HOST}/feed.xml")
    app.run(host="0.0.0.0", port=PORT, debug=False)
