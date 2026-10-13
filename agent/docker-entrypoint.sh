#!/bin/sh
# The agent's container: start as root only long enough to hand uid 1000 the
# folders it writes, then run as uid 1000 with the Docker socket's group and
# nothing else -- what entrypoint.sh did for the agent when it ran from the
# panel's image.
#
# Why root at all: Docker (and CasaOS) create bind-mount directories as root,
# and an image that drops privileges in the Dockerfile can never fix that
# from the inside. uid 1000 is the uid the game containers run as, so the
# server folders the agent creates are theirs too.
set -e

if [ "$(id -u)" = "0" ]; then
    # setpriv changes the user, not the environment: HOME would stay /root,
    # which uid 1000 cannot read.
    export HOME=/home/blessforge

    # Only the folders the agent owns: where the game servers live (the same
    # path on the host and in here, LOCAL_SERVERS_ROOT) and its own state.
    # Never anything above them.
    for d in "${LOCAL_SERVERS_ROOT:-}" /var/lib/blessforge-agent; do
        [ -n "$d" ] || continue
        mkdir -p "$d" 2>/dev/null || true
        chown 1000:1000 "$d" 2>/dev/null || true
    done

    # The one container given the Docker socket: that is how it creates game
    # containers. Its group, numerically -- whatever the host calls it.
    if [ -S /var/run/docker.sock ]; then
        SOCK_GID=$(stat -c %g /var/run/docker.sock)
        exec setpriv --reuid=1000 --regid=1000 --groups="$SOCK_GID" --inh-caps=-all "$@"
    fi
    exec setpriv --reuid=1000 --regid=1000 --init-groups --inh-caps=-all "$@"
fi

# Already unprivileged (someone set `user:` in compose). Nothing to hand over.
exec "$@"
