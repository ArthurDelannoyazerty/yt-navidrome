FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

# ffmpeg = downloads/ReplayGain | chromaprint = fpcalc (AcoustID)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg chromaprint curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# JS runtime for yt-dlp EJS (solves YouTube challenges inside the container)
COPY --from=denoland/deno:latest /usr/bin/deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "yt-dlp[default]"

COPY src/ ./src/

RUN useradd -m -u 1000 pipeline \
 && mkdir -p /data/library \
 && chown -R pipeline:pipeline /app /data
USER pipeline

# Absolute in-container defaults; overridable at runtime
ENV NAVIDROME_LIB_DIR=/data/library \
    LIBRARY_DB=/data/library.db

WORKDIR /app/src
EXPOSE 8008
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS http://localhost:8008/healthz || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8008"]