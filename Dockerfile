# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# System dependencies:
#   ffmpeg     — thumbnail generation + (optional) muxing fallback
#   curl + ca  — fetching N_m3u8DL-RE + Bento4 release artefacts
#   tar        — extracting the N_m3u8DL-RE tarball
#   unzip      — extracting the Bento4 SDK zip
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        tar \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Install N_m3u8DL-RE binary (pinned version)
RUN curl -L https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.3.0-beta/N_m3u8DL-RE_v0.3.0-beta_linux-x64_20241203.tar.gz \
    | tar -xzf - \
    && chmod +x N_m3u8DL-RE \
    && mv N_m3u8DL-RE /usr/local/bin/ \
    && N_m3u8DL-RE --version || true

# Install Bento4 mp4decrypt (used by N_m3u8DL-RE for Widevine decryption)
RUN curl -fSL -o /tmp/bento4.zip \
        https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip \
    && unzip -q /tmp/bento4.zip -d /tmp/bento4 \
    && mv /tmp/bento4/*/bin/mp4decrypt /usr/local/bin/mp4decrypt \
    && chmod +x /usr/local/bin/mp4decrypt \
    && rm -rf /tmp/bento4 /tmp/bento4.zip \
    && mp4decrypt --help >/dev/null 2>&1 || true

WORKDIR /app

# Install Python deps first so they're cached independently of source changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py .

# Persist downloads & Pyrogram session outside the image
ENV DOWNLOAD_ROOT=/app/downloads \
    SESSION_NAME=/app/session/nmdl_bot
RUN mkdir -p /app/downloads /app/session
VOLUME ["/app/downloads", "/app/session"]

# Required at runtime: API_ID, API_HASH, BOT_TOKEN
#   docker run --rm -it \
#     -e API_ID=12345 -e API_HASH=... -e BOT_TOKEN=... \
#     -v $(pwd)/downloads:/app/downloads \
#     -v $(pwd)/session:/app/session \
#     nmdl-bot
CMD ["python", "-u", "bot.py"]
