import json
import os
from datetime import datetime, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator

_DATA_DIR     = Path(os.environ.get("DATA_DIR", "."))
EPISODES_FILE = _DATA_DIR / "episodes.json"


def load_episodes() -> list[dict]:
    if EPISODES_FILE.exists():
        with open(EPISODES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def build_rss(feed_url: str, podcast_name: str, podcast_desc: str, episodes: list[dict]) -> bytes:
    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.id(feed_url)
    fg.title(podcast_name)
    fg.description(podcast_desc)
    fg.author({"name": podcast_name})
    fg.link(href=feed_url, rel="self")
    fg.language("en")
    fg.podcast.itunes_author(podcast_name)
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_category("Technology")

    for ep in reversed(episodes):
        audio_url = ep["ia_url"]
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
