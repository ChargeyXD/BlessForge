# The BlessForge server runtime: one image per Java major.
#
#   docker build --build-arg JAVA=21 -t ghcr.io/chargeyxd/blessforge-runtime:java21 agent/images
#   (or agent/images/build.sh for all four)
#
# Just a JRE and bf-entry. The game server, its mods and its libraries live
# in the server's folder, mounted at /server -- the only writable place the
# container has besides a small noexec /tmp. The agent runs it as uid 1000
# with a read-only root filesystem, every capability dropped and
# no-new-privileges (agent/containers.py), so nothing here runs as root
# after the build.
#
# Java majors: 8 (Minecraft <= 1.16), 17 (1.17 - 1.20.4), 21 (1.20.5 - 1.21.x),
# 25 (26.x, which requires it -- checked against Mojang's version manifest).
ARG JAVA=21
FROM eclipse-temurin:${JAVA}-jre

ARG JAVA
COPY bf-entry /usr/local/bin/bf-entry
RUN chmod 0755 /usr/local/bin/bf-entry \
 && mkdir -p /server && chmod 0755 /server

ENV LANG=C.UTF-8 \
    HOME=/server
WORKDIR /server
LABEL org.opencontainers.image.title="blessforge-runtime" \
      org.opencontainers.image.description="Java ${JAVA} runtime for BlessForge game servers" \
      org.opencontainers.image.source="https://github.com/ChargeyXD/BlessForge" \
      io.blessforge.java="${JAVA}"

USER 1000:1000
ENTRYPOINT ["/usr/local/bin/bf-entry"]
