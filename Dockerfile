FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# The downloader is isolated so nightly yt-dlp updates never alter the API/beets venv.
ARG DOWNLOADER_BUILD=initial
RUN --mount=type=cache,target=/root/.cache/uv \
    echo "$DOWNLOADER_BUILD" \
 && uv venv --python /usr/local/bin/python /opt/ytdlp \
 && uv pip install --python /opt/ytdlp/bin/python --prerelease=allow \
      "yt-dlp[default]" "tenacity>=9,<10"

FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    INGESTOR_STATE_DIR=/data/state NAVIDROME_LIB_DIR=/data/library \
    YTDLP_PYTHON=/opt/ytdlp/bin/python UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/data/state/uv-cache TZ=Europe/Paris
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libchromaprint-tools ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/denoland/deno:bin-2.9.5 /deno /usr/local/bin/deno
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/ytdlp /opt/ytdlp
WORKDIR /app
COPY beets.yaml ./
COPY src/ ./src/
COPY tools/ ./tools/
RUN useradd -m -u 1000 pipeline \
 && mkdir -p /data/state /data/library \
 && chown -R pipeline:pipeline /data /home/pipeline
USER pipeline
WORKDIR /app/src
EXPOSE 8008
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8008/healthz')" || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8008", "--workers", "1", "--timeout-graceful-shutdown", "30"]
