# YouTube → Apple Podcasts

Download any YouTube video as an MP3 and add it to Apple Podcasts as a private podcast episode — all for free.

## How it works

1. **Local app** (`app.py`) runs on your PC
2. You paste a YouTube URL → it downloads the audio, converts to MP3
3. The MP3 is uploaded to **Internet Archive** (free, permanent storage)
4. The episode metadata is committed to **GitHub** — `feed.xml` + `episodes.json`
5. **GitHub Pages** serves `feed.xml` as a public RSS feed
6. Apple Podcasts subscribes to that feed — works without your PC running

## Requirements

- Python 3.11+
- `ffmpeg` installed ([winget install ffmpeg](https://winget.run/pkg/Gyan/FFmpeg))
- Free accounts: [Internet Archive](https://archive.org/account/signup) · [GitHub](https://github.com)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000** and fill in the setup form:

| Field | Where to get it |
|---|---|
| Podcast Name & Description | Choose anything |
| Podcast ID | A unique slug, e.g. `yourname-podcast` |
| Archive.org Access Key | [archive.org/account/s3.php](https://archive.org/account/s3.php) |
| Archive.org Secret Key | Same page |
| GitHub Repository | `username/repo-name`, e.g. `adamchocholac/audio-downloader` |
| GitHub Token | [github.com/settings/tokens](https://github.com/settings/tokens?type=beta) → Fine-grained → **Contents: Read & Write** |

## Enable GitHub Pages (one-time)

1. Go to your repo on GitHub → **Settings → Pages**
2. Source: **GitHub Actions**
3. Save

Your feed URL will be:
```
https://<username>.github.io/<repo>/feed.xml
```

Add this URL in Apple Podcasts via **File → Follow a Show by URL**.

## Adding episodes

1. Open **http://localhost:5000**
2. Paste any YouTube URL
3. Click **Add Episode**

The app downloads, converts, uploads to Archive.org, and pushes the updated feed to GitHub.
The episode appears in Apple Podcasts within ~1 minute (after GitHub Pages deploys).

## Architecture

```
[Local app.py]
    │  yt-dlp → ffmpeg → MP3
    │
    ├─→ [Internet Archive]  permanent MP3 hosting
    │
    └─→ [GitHub repo]  episodes.json + feed.xml
              │
              └─→ [GitHub Pages]  https://user.github.io/repo/feed.xml
                        │
                        └─→ [Apple Podcasts]  subscribes to feed
```
