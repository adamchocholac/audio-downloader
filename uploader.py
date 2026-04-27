import requests
from pathlib import Path
from typing import Callable
from urllib.parse import quote

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
    """
    file_size = file_path.stat().st_size
    filename  = file_path.name

    headers = {
        "Authorization":              f"LOW {access_key}:{secret_key}",
        "Content-Length":             str(file_size),
        "x-archive-auto-make-bucket": "1",
        "x-archive-meta-title":       quote(title, safe=''),
        "x-archive-meta-mediatype":   "audio",
        "x-archive-meta-subject":     "podcast;youtube;audio",
        "x-archive-size-hint":        str(file_size),
        "x-archive-queue-derive":     "0",
    }

    url = f"{IA_S3_ENDPOINT}/{identifier}/{quote(filename, safe='')}"

    if on_progress:
        on_progress({"phase": "uploading", "percent": 0})

    class _ProgressFile:
        """File-like wrapper for progress reporting. Using a file object (not a
        generator) prevents requests/urllib3 from adding Transfer-Encoding: chunked,
        which conflicts with an explicit Content-Length header."""
        def __init__(self):
            self._f        = open(file_path, "rb")
            self._uploaded = 0

        def read(self, n=-1):
            chunk = self._f.read(n)
            if chunk:
                self._uploaded += len(chunk)
                if on_progress:
                    on_progress({
                        "phase":   "uploading",
                        "percent": round(self._uploaded / file_size * 100, 1),
                    })
            return chunk

        def close(self):
            self._f.close()

    pf = _ProgressFile()
    try:
        response = requests.put(url, data=pf, headers=headers, timeout=300)
        response.raise_for_status()
    finally:
        pf.close()

    if on_progress:
        on_progress({"phase": "uploading", "percent": 100})

    return f"https://archive.org/download/{identifier}/{quote(filename, safe='')}"


def delete_from_archive(
    identifier: str,
    filename: str,
    access_key: str,
    secret_key: str,
) -> None:
    """Delete a file from an Internet Archive item."""
    url = f"{IA_S3_ENDPOINT}/{identifier}/{quote(filename, safe='')}"
    headers = {"Authorization": f"LOW {access_key}:{secret_key}"}
    response = requests.delete(url, headers=headers, timeout=30)
    response.raise_for_status()
