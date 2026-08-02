FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/streamplay
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV SP_DATA_DIR=/data \
    SP_DOWNLOAD_DIR=/downloads \
    SP_CACHE_DIR=/data/cache

VOLUME ["/data", "/downloads"]
EXPOSE 8643

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8643", "--workers", "1"]
