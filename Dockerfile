FROM python:3.13-slim

# Install ffmpeg (needed for audio conversion)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py downloader.py uploader.py feed.py ./

# Temporary download directory (files are removed after upload to Archive.org)
RUN mkdir -p downloads

ENV PORT=5000
ENV DATA_DIR=/app

EXPOSE 5000

CMD ["python", "app.py"]
