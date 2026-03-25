FROM python:3.13-slim

LABEL maintainer="BridgeMost Contributors"
LABEL description="Transparent Telegram ↔ Mattermost bridge"

WORKDIR /app

# System deps (none needed for pure Python, but keep layer for future)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cache layer)
COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir .

# Default data directory for SQLite
RUN mkdir -p /data
VOLUME /data

# Health endpoint
EXPOSE 9191

# Config mount point
VOLUME /config

ENV BRIDGEMOST_CONFIG=/config/config.yaml

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9191/health')" || exit 1

ENTRYPOINT ["python", "-m", "bridgemost"]
