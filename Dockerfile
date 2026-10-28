FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[music]"

COPY series ./series
COPY assets ./assets

# SQLite state + renders; mount a persistent volume here
VOLUME ["/app/data"]
ENV DATA_DIR=/app/data PYTHONUNBUFFERED=1

HEALTHCHECK --interval=5m --timeout=10s CMD reelbot status >/dev/null || exit 1
CMD ["reelbot", "run"]
