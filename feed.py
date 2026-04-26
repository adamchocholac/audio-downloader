import json
import os
from datetime import datetime, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator

_DATA_DIR    = Path(os.environ.get("DATA_DIR", "."))
EPISODES_FILE = _DATA_DIR / "episodes.json"


def load_episodes() -> list[dict]:
    if EPISODES_FILE.exists():
        with open(EPISODES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_episode(episode: dict) -> None:
    episodes = load_episodes()
    if not any(e.get("filename") == episode["filename"] for e in episodes):
        episodes.append(episode)
        with open(EPISODES_FILE, "w", encoding="utf-8") as f:
            json.dump(episodes, f, indent=2, ensure_ascii=False)


def build_rss(host_url: str) -> bytes:
    feed_url = f"{host_url}/feed.xml"

    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.id(feed_url)
    fg.title("My YouTube Podcast")
    fg.description("Audio downloaded from YouTube.")
    fg.author({"name": "YouTube Podcast"})
    fg.link(href=feed_url, rel="self")
    fg.language("en")
    fg.podcast.itunes_author("YouTube Podcast")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_category("Technology")

    for ep in reversed(load_episodes()):
        audio_url = f"{host_url}/audio/{ep['filename']}"
        fe = fg.add_entry()
        fe.id(audio_url)
        fe.title(ep["title"])
        fe.description(ep.get("description") or ep["title"])
        fe.enclosure(audio_url, 0, "audio/mpeg")
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

    return fg.rss_str(pretty=True)
