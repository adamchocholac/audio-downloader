import re
import os
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Callable

import yt_dlp

DOWNLOADS_DIR = Path("downloads")

FFMPEG_BIN_DIR = (
    r"C:\Users\uziachocho\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
)
FFMPEG_EXE = os.path.join(FFMPEG_BIN_DIR, "ffmpeg.exe")


@dataclass
class DownloadResult:
    title: str
    filename: str
    duration: int
    uploader: str
    thumbnail: str


def download_audio(
    url: str,
    on_progress: Callable[[dict], None] | None = None,
) -> DownloadResult:
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    # ── Step 1: download raw audio only, no postprocessor ────────────────────
    def _dl_hook(d: dict) -> None:
        if on_progress is None:
            return
        if d["status"] == "downloading":
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent    = round(downloaded / total * 100, 1) if total else 0
            speed_bps  = d.get("speed") or 0
            eta        = d.get("eta")
            on_progress({
                "phase":   "downloading",
                "percent": percent,
                "speed":   _fmt_speed(speed_bps),
                "eta":     f"{eta // 60}:{eta % 60:02d}" if eta is not None else "",
            })

    ydl_opts = {
        "format":          "bestaudio/best",
        "outtmpl":         str(DOWNLOADS_DIR / "%(title)s.%(ext)s"),
        "ffmpeg_location": FFMPEG_BIN_DIR,
        "progress_hooks":  [_dl_hook],
        "quiet":           True,
        "no_warnings":     True,
        "noplaylist":      True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        dl_info = ydl.extract_info(url, download=True)

    # Metadata from the single download call
    title     = dl_info.get("title", "audio")
    duration  = dl_info.get("duration", 0) or 0
    uploader  = dl_info.get("uploader", "Unknown")
    thumbnail = dl_info.get("thumbnail", "")

    # Actual file path yt-dlp wrote (trust this over anything we compute)
    requested = (dl_info.get("requested_downloads") or [{}])[0]
    raw_path  = Path(requested.get("filepath") or "")

    if not raw_path.exists():
        # Fallback: newest non-mp3 file
        candidates = sorted(
            [f for f in DOWNLOADS_DIR.iterdir() if f.suffix.lower() != ".mp3"],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if not candidates:
            raise FileNotFoundError("Downloaded audio file not found.")
        raw_path = candidates[0]

    # ── Step 2: convert to MP3 with live ffmpeg progress ─────────────────────
    mp3_path = raw_path.with_suffix(".mp3")
    _convert_to_mp3(raw_path, mp3_path, duration, on_progress)

    if raw_path != mp3_path and raw_path.exists():
        raw_path.unlink()

    return DownloadResult(
        title=title,
        filename=mp3_path.name,
        duration=duration,
        uploader=uploader,
        thumbnail=thumbnail,
    )


def _convert_to_mp3(
    input_path: Path,
    output_path: Path,
    duration_s: int,
    on_progress: Callable[[dict], None] | None,
) -> None:
    if on_progress:
        on_progress({"phase": "converting", "percent": 0})

    cmd = [
        FFMPEG_EXE, "-y",
        "-threads", "0",
        "-i", str(input_path),
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        "-compression_level", "0",
        "-progress", "pipe:1",
        "-nostats",
        str(output_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    for line in proc.stdout:
        if line.startswith("out_time_us="):
            try:
                us = int(line.split("=", 1)[1].strip())
                if duration_s > 0 and us > 0:
                    pct = min(99.0, us / (duration_s * 1_000_000) * 100)
                    if on_progress:
                        on_progress({"phase": "converting", "percent": round(pct, 1)})
            except ValueError:
                pass

    proc.wait()

    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ffmpeg failed with exit code {proc.returncode}")

    if on_progress:
        on_progress({"phase": "converting", "percent": 100})


def _fmt_speed(bps: float) -> str:
    if not bps:
        return ""
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f} MiB/s"
    if bps >= 1_024:
        return f"{bps / 1_024:.0f} KiB/s"
    return f"{bps:.0f} B/s"
