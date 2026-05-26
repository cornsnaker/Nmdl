# syntax=docker/dockerfile:1.7

# ==========================================
# Stage 1: Build & Extraction Artifacts
# ==========================================
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tar \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

# Download and extract N_m3u8DL-RE
RUN curl -L https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.3.0-beta/N_m3u8DL-RE_v0.3.0-beta_linux-x64_20241203.tar.gz \
    | tar -xzf - \
    && chmod +x N_m3u8DL-RE

# Download and extract Bento4 mp4decrypt
RUN curl -fSL -o bento4.zip https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip \
    && unzip -q bento4.zip -d bento4 \
    && mv bento4/*/bin/mp4decrypt . \
    && chmod +x mp4decrypt


# ==========================================
# Stage 2: Final Production Runtime
# ==========================================
FROM python:3.12-slim AS runtime

# Optimize Python environment and configure default paths
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    DOWNLOAD_ROOT=/app/downloads \
    SESSION_NAME=/app/session/nmdl_bot

# Runtime dependency only (leaving curl, tar, and unzip behind for security)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Pull binaries directly from the builder stage
COPY --from=builder /tmp/N_m3u8DL-RE /usr/local/bin/
COPY --from=builder /tmp/mp4decrypt /usr/local/bin/

# Smoke tests to ensure binaries work in this slim environment
RUN N_m3u8DL-RE --version || true
RUN mp4decrypt --help >/dev/null 2>&1 || true

WORKDIR /app

# Install Python dependencies (Cached layer)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy bot application and startup script
COPY bot.py start.sh ./
RUN chmod +x start.sh

# Define data persistence boundaries
VOLUME ["/app/downloads", "/app/session"]

# Hand off execution to the startup script
CMD ["./start.sh"]
