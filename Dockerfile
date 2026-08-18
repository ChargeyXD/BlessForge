FROM python:3.12-slim

LABEL org.opencontainers.image.title="BlessForge" \
      org.opencontainers.image.description="Install CurseForge modpacks into Crafty Controller, or import your own export, then manage mods, configs, performance and startup problems." \
      org.opencontainers.image.source="https://github.com/ChargeyXD/BlessForge" \
      org.opencontainers.image.url="https://github.com/ChargeyXD/BlessForge" \
      org.opencontainers.image.documentation="https://github.com/ChargeyXD/BlessForge#readme" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8710 \
    DATA_DIR=/data

WORKDIR /app

# curl for healthcheck + ca-certificates for secure outbound API calls (CurseForge/Modrinth/Crafty)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Run unprivileged. /data holds download & unpack cache.
RUN useradd -u 1000 -m studio \
 && mkdir -p /data/cache /data/downloads /data/uploads /app \
 && chown -R studio:studio /data /app
USER studio

VOLUME ["/data"]
EXPOSE 8710

HEALTHCHECK --interval=20s --timeout=4s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8710}/api/healthz" > /dev/null || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8710} --proxy-headers --forwarded-allow-ips='*'"]
