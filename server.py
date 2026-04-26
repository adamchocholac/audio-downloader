"""
Fly.io RSS feed server — minimal, no ffmpeg/yt-dlp needed.

Endpoints:
  GET  /             — status page
  GET  /feed.xml     — Apple Podcasts RSS feed
  GET  /api/episodes — list all episodes (JSON)
  POST /api/episodes — add a new episode (requires Authorization: Bearer <API_TOKEN>)
  DELETE /api/episodes/<filename> — remove an episode + mark as deleted
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, abort
from feedgen.feed import FeedGenerator

app = Flask(__name__)

DATA_DIR      = Path(os.environ.get("DATA_DIR", "."))
EPISODES_FILE = DATA_DIR / "episodes.json"
API_TOKEN     = os.environ.get("API_TOKEN", "")
PORT          = int(os.environ.get("PORT", 8080))
HOST          = os.environ.get("HOST_URL", "")


# ---------------------------------------------------------------------------
# Episode store
# ---------------------------------------------------------------------------

def _load() -> list[dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if EPISODES_FILE.exists():
        with open(EPISODES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(episodes: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(EPISODES_FILE, "w", encoding="utf-8") as f:
        json.dump(episodes, f, indent=2, ensure_ascii=False)


def _auth() -> bool:
    if not API_TOKEN:
        return True  # no token configured → open (dev mode)
    return request.headers.get("Authorization", "") == f"Bearer {API_TOKEN}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    host = HOST or request.host_url.rstrip("/")
    eps  = _load()
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<title>Podcast Feed Server</title>
<style>body{{font-family:system-ui;background:#111;color:#ccc;padding:2rem;max-width:600px;margin:auto}}
a{{color:#ff4444}}code{{background:#222;padding:.2em .5em;border-radius:4px}}</style>
</head><body>
<h2>Podcast Feed Server</h2>
<p>Feed URL: <code>{host}/feed.xml</code></p>
<p>Episodes: <strong>{len(eps)}</strong></p>
<p>Subscribe in Apple Podcasts: <b>File &rarr; Follow a Show by URL</b></p>
</body></html>"""


@app.get("/feed.xml")
def rss_feed():
    host = HOST or request.host_url.rstrip("/")
    feed_url = f"{host}/feed.xml"

    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.id(feed_url)
    fg.title(os.environ.get("PODCAST_NAME", "My YouTube Podcast"))
    fg.description(os.environ.get("PODCAST_DESC", "Audio downloaded from YouTube."))
    fg.author({"name": "YouTube Podcast"})
    fg.link(href=feed_url, rel="self")
    fg.language("en")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_category("Technology")

    for ep in reversed(_load()):
        fe = fg.add_entry()
        fe.id(ep["ia_url"])
        fe.title(ep["title"])
        fe.description(ep.get("description") or ep["title"])
        fe.enclosure(ep["ia_url"], 0, "audio/mpeg")
        fe.podcast.itunes_duration(str(ep.get("duration", 0)))
        fe.podcast.itunes_author(ep.get("uploader", ""))
        if ep.get("thumbnail"):
            fe.podcast.itunes_image(ep["thumbnail"])
        pub = ep.get("published")
        if pub:
            dt = datetime.fromisoformat(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            fe.published(dt)

    return Response(fg.rss_str(pretty=True), mimetype="application/rss+xml")


@app.get("/api/episodes")
def get_episodes():
    return jsonify(_load())


@app.post("/api/episodes")
def add_episode():
    if not _auth():
        abort(401)
    ep = request.get_json(force=True, silent=True)
    if not ep or not ep.get("ia_url"):
        return jsonify({"error": "Missing episode data"}), 400

    episodes = _load()
    if not any(e["ia_url"] == ep["ia_url"] for e in episodes):
        episodes.append(ep)
        _save(episodes)

    return jsonify({"ok": True, "total": len(episodes)}), 201


@app.delete("/api/episodes/<path:filename>")
def delete_episode(filename: str):
    if not _auth():
        abort(401)
    episodes = [e for e in _load() if e.get("filename") != filename]
    _save(episodes)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(f"Feed server at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
