FROM python:3.12-slim

LABEL org.opencontainers.image.title="BlessForge" \
      org.opencontainers.image.description="Install CurseForge modpacks into Crafty Controller, then manage mods, configs, performance and startup problems." \
      org.opencontainers.image.source="https://github.com/" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8710 \
    DATA_DIR=/data

WORKDIR /app

# curl is only here so the container has a working HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Run unprivileged. /data holds the download cache only -- all durable state
# lives in Crafty itself, so losing this volume costs nothing but bandwidth.
RUN useradd -u 1000 -m studio \
 && mkdir -p /data \
 && chown -R studio:studio /data /app
USER studio

VOLUME ["/data"]
EXPOSE 8710

HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" > /dev/null || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
