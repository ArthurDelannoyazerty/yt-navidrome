# ---------- BUILD STAGE ----------
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Lockfile first = best layer caching (deps reinstall only when lock changes)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-editable

# ---------- RUNTIME STAGE ----------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# System deps live in the RUNTIME image (ffmpeg = audio/ReplayGain, chromaprint = fpcalc)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg chromaprint ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# JS runtime for yt-dlp EJS challenge solving
COPY --from=denoland/deno:latest /usr/bin/deno /usr/local/bin/deno

# Bring over ONLY the virtualenv from the build stage
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY src/ ./src/

RUN useradd -m -u 1000 pipeline \
 && mkdir -p /data/library \
 && chown -R pipeline:pipeline /app /data
USER pipeline

WORKDIR /app/src
EXPOSE 8008

# curl-free healthcheck (python slim has no curl)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8008/healthz')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8008"]