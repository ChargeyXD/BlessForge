#!/bin/sh
# Build (and optionally push) the four runtime images.
#
#   agent/images/build.sh            build locally
#   PUSH=1 agent/images/build.sh     build and push
#
# REPO defaults to the name agent/config.py expects; a node pointed at a
# private registry sets [agent.images] in its agent.toml instead.
set -eu
REPO=${REPO:-ghcr.io/chargeyxd/blessforge-runtime}
HERE=$(cd "$(dirname "$0")" && pwd)
for v in 8 17 21 25; do
  docker build --pull --build-arg JAVA="$v" -t "$REPO:java$v" "$HERE"
  if [ "${PUSH:-0}" = 1 ]; then docker push "$REPO:java$v"; fi
done
