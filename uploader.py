import requests
from pathlib import Path
from typing import Callable

IA_S3_ENDPOINT = "https://s3.us.archive.org"


def upload_to_archive(
    identifier: str,
    file_path: Path,
    title: str,
    access_key: str,
    secret_key: str,
    on_progress: Callable[[dict], None] | None = None,
) -> str:
    """
    Upload an MP3 to Internet Archive via their S3-compatible API.
    Returns the permanent public URL of the uploaded file.
    Streams the upload so on_progress receives real percentage:
        { "phase": "uploading", "percent": 0-100 }
    """
    file_size = file_path.stat().st_size
    filename  = file_path.name

    headers = {
        "Authorization":              f"LOW {access_key}:{secret_key}",
        "x-archive-meta-title":       title,
        "x-archive-meta-mediatype":   "audio",
        "x-archive-meta-subject":     "podcast;youtube;audio",
        "x-archive-size-hint":        str(file_size),
        "x-archive-queue-derive":     "0",
    }

    url = f"{IA_S3_ENDPOINT}/{identifier}/{filename}"

    if on_progress:
        on_progress({"phase": "uploading", "percent": 0})

    def _stream():
        uploaded   = 0
        chunk_size = 65_536
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                uploaded += len(chunk)
                if on_progress:
                    on_progress({
                        "phase":   "uploading",
                        "percent": round(uploaded / file_size * 100, 1),
                    })
                yield chunk

    response = requests.put(url, data=_stream(), headers=headers, timeout=300)
    response.raise_for_status()

    if on_progress:
        on_progress({"phase": "uploading", "percent": 100})

    return f"https://archive.org/download/{identifier}/{filename}"
