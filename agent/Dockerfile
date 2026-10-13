# The node agent on its own: agent/ and its requirements, none of the
# panel's. It runs under a 256 MB limit in the one-box docker-compose.yml,
# where it used to run from the whole panel image.
#
#   docker build -t ghcr.io/chargeyxd/blessforge-agent:latest agent
#
# (agent/images/Dockerfile is a different thing: the game servers' runtime
# images. This image ships that folder too -- it is what the agent builds a
# runtime image from when one cannot be pulled.)

# The dependencies, installed into a venv that is copied across whole: no
# pip of its own (the base image's installs into it), and nothing pip
# downloaded or cached leaves this stage.
FROM python:3.12-slim AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv --without-pip /venv \
 && pip --python /venv/bin/python install -r /tmp/requirements.txt

FROM python:3.12-slim

LABEL org.opencontainers.image.title="blessforge-agent" \
      org.opencontainers.image.description="The BlessForge node agent: runs each game server in its own locked-down container." \
      org.opencontainers.image.source="https://github.com/ChargeyXD/BlessForge" \
      org.opencontainers.image.licenses="MIT"

# MALLOC_ARENA_MAX: as in the panel's image, two glibc arenas instead of one
# per thread keeps a small async process small.
ENV PATH=/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2

# curl for the healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deps /venv /venv
WORKDIR /app
COPY . ./agent/

# uid 1000: the uid the game containers run as, so the server folders the
# agent writes are theirs too. The container starts as root only to hand
# that user its folders and the Docker socket's group (docker-entrypoint.sh).
RUN useradd -u 1000 -m blessforge \
 && install -m 0755 agent/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8443/health > /dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "agent"]
