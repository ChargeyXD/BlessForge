#!/bin/sh
# Start as root only long enough to make the data directory usable, then drop
# to the unprivileged app user.
#
# Why this exists: CasaOS creates bind-mount directories as root. The app runs
# as uid 1000, so a plain `USER studio` image mounted at /DATA/AppData/<app>
# cannot write a single byte -- every download, cache write and import fails
# with a permission error the user never sees. Running as root permanently
# would fix it and cost too much; this fixes it and costs one syscall.
set -e

DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    # Only ever touch the three directories this app owns. Never chown
    # DATA_DIR itself: if someone points the mount at the wrong folder in the
    # CasaOS dialog -- Crafty's AppData, say -- taking ownership of it would
    # turn a misconfiguration into someone else's outage.
    for d in cache downloads uploads; do
        mkdir -p "$DATA_DIR/$d" 2>/dev/null || true
        chown studio:studio "$DATA_DIR/$d" 2>/dev/null || true
    done

    # A mount that already holds a Crafty install is the one mistake worth
    # naming out loud, because BlessForge would otherwise look merely broken.
    if [ -e "$DATA_DIR/crafty.sqlite" ] || [ -d "$DATA_DIR/servers" ] || \
       [ -d "$DATA_DIR/data/servers" ]; then
        echo "WARNING: $DATA_DIR looks like a Crafty Controller data directory." >&2
        echo "         BlessForge wants its own empty folder here (a cache), not Crafty's." >&2
        echo "         Point this mount at /DATA/AppData/blessforge/data instead." >&2
    fi

    exec setpriv --reuid=1000 --regid=1000 --init-groups --inh-caps=-all "$@"
fi

# Already unprivileged (someone set `user:` in compose). Nothing to hand over.
exec "$@"
