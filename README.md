# YouTube Audio Downloader

A minimal web app — paste a YouTube URL, get an MP3.

## Requirements

- Python 3.11+
- [ffmpeg](https://ffmpeg.org/download.html) installed and on your `PATH`

## Setup

```powershell
# Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Run

```powershell
python app.py
```

Open **http://localhost:5000** in your browser.

## How it works

1. Paste any YouTube URL and click **Download**.
2. The server downloads the best audio stream in the background and converts it to a 192 kbps MP3.
3. When ready, click **Save MP3** to download the file to your computer.

## Project structure

```
audio-downloader/
├── app.py           — Flask server + single-page UI
├── downloader.py    — yt-dlp download logic
├── downloads/       — MP3 files (created automatically)
└── requirements.txt
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Web UI |
| `POST` | `/api/download` | Start a download job `{"url": "..."}` → `{"job_id": "..."}` |
| `GET`  | `/api/status/<job_id>` | Poll job status |
| `GET`  | `/api/file/<filename>` | Download the finished MP3 |
