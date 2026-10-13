FROM python:3.12-slim

LABEL org.opencontainers.image.title="BlessForge" \
      org.opencontainers.image.description="A Minecraft server panel: install CurseForge modpacks or your own exports, then manage mods, configs, performance and startup problems." \
      org.opencontainers.image.source="https://github.com/ChargeyXD/BlessForge" \
      org.opencontainers.image.url="https://github.com/ChargeyXD/BlessForge" \
      org.opencontainers.image.documentation="https://github.com/ChargeyXD/BlessForge#readme" \
      org.opencontainers.image.licenses="MIT"

# MALLOC_ARENA_MAX: glibc gives each thread its own arena (up to 8x cores),
# and each arena keeps its own free lists. On an 8-core box that is a lot of
# memory held per-arena and never returned. Two arenas is plenty for a
# mostly-async process and cuts the resident footprint of a big install
# substantially.
# MALLOC_TRIM_THRESHOLD_: without it glibc raises the trim threshold
# dynamically as the process allocates big blocks, and after a 400 MB install
# it will effectively never trim on its own again.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8710 \
    DATA_DIR=/data \
    MALLOC_ARENA_MAX=2 \
    MALLOC_TRIM_THRESHOLD_=134217728

WORKDIR /app

# curl for healthcheck + ca-certificates for secure outbound API calls (CurseForge/Modrinth/nodes)
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
# The schema's migrations. The app runs `alembic upgrade head` itself on
# start, so an image without these would boot against an empty database.
COPY alembic/ ./alembic/
COPY alembic.ini ./
# One template per loader (app/templates.py): without them nothing can be created.
COPY templates/ ./templates/
# The node agent, so one image runs the panel, the worker AND the agent the
# one-box docker-compose.yml bundles (`python -m agent`): its layers are
# already on that machine. A node on a machine of its own uses the slim
# blessforge-agent image instead (agent/Dockerfile). agent/images is the
# runtime images' Dockerfile, built on the machine when it cannot be pulled.
COPY agent/ ./agent/
# Moving servers off Crafty, from inside the panel's container:
#   docker exec -it blessforge python dev/tools/import_from_crafty.py --dry-run
COPY dev/tools/import_from_crafty.py ./dev/tools/import_from_crafty.py

# The app runs unprivileged as uid 1000. The container still STARTS as root so
# the entrypoint can hand it a writable /data -- CasaOS creates bind-mount
# directories as root, and an image that drops privileges in the Dockerfile can
# never fix that from the inside. entrypoint.sh chowns only the
# directories this app owns and then drops to studio via setpriv. Every
# directory the app writes into has to appear in both lists -- those writes
# are wrapped in try/except, so one left off is not an error anybody sees,
# it is a feature that silently never persists.
RUN useradd -u 1000 -m studio \
 && mkdir -p /data/cache /data/downloads /data/uploads /data/state \
            /data/backups /data/exports /data/roulette-exports /app \
 && chown -R studio:studio /data /app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8710

HEALTHCHECK --interval=20s --timeout=4s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8710}/api/healthz" > /dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8710} --proxy-headers --forwarded-allow-ips='*'"]
