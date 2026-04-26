import json
from datetime import datetime, timezone
from pathlib import Path

from feedgen.feed import FeedGenerator

EPISODES_FILE = Path("episodes.json")


def load_episodes() -> list[dict]:
    if EPISODES_FILE.exists():
        with open(EPISODES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_episode(episode: dict) -> None:
    episodes = load_episodes()
    # Avoid duplicates by ia_url
    if not any(e.get("ia_url") == episode["ia_url"] for e in episodes):
        episodes.append(episode)
        with open(EPISODES_FILE, "w", encoding="utf-8") as f:
            json.dump(episodes, f, indent=2, ensure_ascii=False)


def build_rss(host_url: str, podcast_name: str, podcast_description: str) -> bytes:
    """Generate a valid Apple Podcasts-compatible RSS feed."""
    feed_url = f"{host_url}/feed.xml"

    fg = FeedGenerator()
    fg.load_extension("podcast")

    fg.id(feed_url)
    fg.title(podcast_name)
    fg.description(podcast_description)
    fg.author({"name": podcast_name})
    fg.link(href=feed_url, rel="self")
    fg.language("en")
    fg.podcast.itunes_author(podcast_name)
    fg.podcast.itunes_summary(podcast_description)
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_category("Technology")

    for ep in reversed(load_episodes()):
        fe = fg.add_entry()
        fe.id(ep["ia_url"])
        fe.title(ep["title"])
        fe.description(ep.get("description") or ep["title"])
        fe.enclosure(ep["ia_url"], 0, "audio/mpeg")
        fe.podcast.itunes_duration(str(ep.get("duration", 0)))
        fe.podcast.itunes_author(ep.get("uploader", ""))

        thumbnail = ep.get("thumbnail", "")
        if thumbnail:
            fe.podcast.itunes_image(thumbnail)

        pub = ep.get("published")
        if pub:
            dt = datetime.fromisoformat(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            fe.published(dt)

    return fg.rss_str(pretty=True)
