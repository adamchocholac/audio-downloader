FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir flask feedgen requests

COPY feed.py server.py ./

RUN mkdir -p /data

ENV DATA_DIR=/data
ENV PORT=8080

EXPOSE 8080

CMD ["python", "server.py"]
