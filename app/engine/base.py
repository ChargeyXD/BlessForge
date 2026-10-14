"""What BlessForge needs from whatever actually runs a Minecraft server.

`ServerEngine` is that contract. Everything in the app that touches a server
-- its files, its console, its power, its launch command -- goes through it,
and nothing above this package knows which driver is answering. Two exist:

  * `mock`   -- in memory, for tests and for driving the UI with no node.
  * `agent`  -- the BlessForge node agent (WP-05): one hardened container per
               server on a node BlessForge owns.

The shapes below are the whole contract. They keep the field names the rest
of the app and the front end already speak (`server_name`, `server_port`,
`execution_command`), because those names describe the thing rather than any
one product; what changed is that sizes are bytes, times are epoch seconds,
and the Java a server runs on is a field rather than something parsed back
out of its command.

ServerRecord (`get_server`, `list_servers`):

    server_id          str    uuid
    server_name        str
    server_port        int    the game port inside the node's network
    executable         str    the jar the startup command launches ("" until
                              a loader is installed)
    execution_command  str    the startup command, run in the server folder
    java_major         int?   which runtime image the server runs on
    memory_mb          int    the container's memory limit
    node               str    which node holds it
    path               str    for display only
    created            str    ISO-8601
    type               str    "minecraft-java"
    auto_start         bool

Stats (`get_stats`):

    running, crashed   bool
    cpu                float  percent of one core-set, as the runtime reports
    mem                int    resident bytes
    mem_percent        float  share of the server's own memory limit
    online, max        int?   players, when the server answers a status ping
    players            list[str]
    world_name         str?
    world_size         str?   human, for display
    started            float? epoch seconds, None while stopped

Directory listing (`list_dir`): `{name: {"path", "dir", "size", "modified"}}`
with `size` in bytes and `modified` in epoch seconds.
"""
from __future__ import annotations

import datetime
import re
from typing import Any, AsyncIterator, Protocol, runtime_checkable


class EngineError(RuntimeError):
    """A server operation failed. `status` is the HTTP code to surface."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


# Minecraft reads eula.txt as a properties file; this is the form every
# server accepts.
EULA_ACCEPTED = "eula=true\n"

# What `power` accepts. The `_server` spellings are the ones the HTTP API has
# always taken from the front end, so they are accepted and normalised.
# "switch": stop whichever server on the node holds this one's port, then
# start this one (servers may share a port; one runs at a time).
POWER_ACTIONS = ("start", "stop", "restart", "kill", "switch")


def power_action(action: str) -> str:
    a = (action or "").strip().lower()
    if a.endswith("_server"):
        a = a[: -len("_server")]
    if a not in POWER_ACTIONS:
        raise EngineError(f"unknown power action {action!r}", 400)
    return a


@runtime_checkable
class ServerEngine(Protocol):
    """The operations a driver implements. All paths are server-relative."""

    name: str

    # --- servers ---------------------------------------------------------
    async def list_servers(self) -> list[dict]: ...
    async def get_server(self, server_id: str) -> dict: ...
    async def get_stats(self, server_id: str) -> dict: ...
    async def create_server(self, *, name: str, port: int, memory_mb: int,
                            mc_version: str = "", loader: str = "",
                            java_major: int | None = None,
                            server_id: str | None = None,
                            limits: dict | None = None) -> str: ...
    async def delete_server(self, server_id: str, delete_files: bool = True) -> None: ...
    async def update_server(self, server_id: str, fields: dict) -> None: ...
    async def power(self, server_id: str, action: str) -> None: ...
    async def install(self, server_id: str, spec: dict, on_progress=None) -> dict: ...
    async def set_runtime(self, server_id: str, mc_version: str,
                          java_major: int | None = None) -> dict: ...

    # --- files -----------------------------------------------------------
    async def list_dir(self, server_id: str, path: str = ".") -> dict: ...
    async def read_file(self, server_id: str, path: str) -> str: ...
    async def read_tail(self, server_id: str, path: str, nbytes: int) -> str: ...
    async def write_file(self, server_id: str, path: str, contents: str) -> None: ...
    async def delete_paths(self, server_id: str, rel_paths: list[str]) -> None: ...
    async def rename_path(self, server_id: str, path: str, new_name: str) -> None: ...
    async def move_path(self, server_id: str, path: str, dest_dir: str) -> None: ...
    async def create_entry(self, server_id: str, parent: str, name: str,
                           directory: bool = False) -> None: ...
    async def upload_file(self, server_id: str, location: str, filename: str,
                          payload: bytes) -> None: ...
    async def upload_path(self, server_id: str, location: str, filename: str,
                          src: Any) -> None: ...
    async def extract(self, server_id: str, rel_zip_path: str) -> None: ...
    async def compress(self, server_id: str, rel_paths: list[str], dest_rel: str) -> str: ...
    async def download_file(self, server_id: str, rel_path: str) -> bytes: ...
    def stream_file(self, server_id: str, rel_path: str) -> AsyncIterator[bytes]: ...

    # --- console ---------------------------------------------------------
    async def console_lines(self, server_id: str, *, from_file: bool = False
                            ) -> list[str]: ...
    async def send_command(self, server_id: str, command: str) -> dict: ...


# --- Java -------------------------------------------------------------------

# Minecraft version -> the Java major it must run on. A newer Java is not
# automatically safe: NeoForge and Forge refuse anything past the version
# they were built for, which is why a runtime defaulting to the newest Java
# silently kills a 1.21.1 server at startup.
#
# The year-based releases (26.x) are not 1.x, so they cannot fall through to
# the last branch: Mojang moved them to Java 25. The version JSON Mojang
# publishes says so per release (`javaVersion.majorVersion`), and the
# install templates read that when it is reachable -- this table is the
# offline answer.
def required_java_major(mc_version: str) -> int:
    try:
        parts = [int(p) for p in (mc_version or "").split(".")[:3]]
    except ValueError:
        return 21
    if not parts:
        return 21
    major, minor = (parts + [0, 0])[:2]
    patch = parts[2] if len(parts) > 2 else 0
    if major >= 26:
        return 25
    if major != 1:
        return 21
    if minor > 20 or (minor == 20 and patch >= 5):
        return 21
    if minor >= 17:
        return 17
    return 8


# A launch command that names a versioned JVM path says which Java it runs.
# Servers on a node run whatever runtime image `java_major` picked and their
# command says plain `java`, so this is only the fallback for a record that
# does not carry `java_major` (an imported one, say). The path is matched
# first and the major read out of it second: there are two path shapes in
# the wild (java-21-openjdk-amd64 and openjdk-21) and one regex covering both
# stops covering either.
_JAVA_PATH = re.compile(r"(/\S*?/(?:jre/)?bin/java)(?=[\s\"']|$)")
_JAVA_MAJOR = re.compile(r"(?:java|openjdk)-(?:1\.)?(\d+)")


def java_in_command(command: str) -> dict:
    """Which Java a launch command invokes. `major` None means "cannot tell"."""
    command = command or ""
    m = _JAVA_PATH.search(command)
    if m:
        path = m.group(1)
        v = _JAVA_MAJOR.search(path)
        return {"path": path, "major": int(v.group(1)) if v else None,
                "pinned": bool(v)}
    if re.search(r"(?:^|[\s\"'])java(?:[\s\"']|$)", command):
        return {"path": "java", "major": None, "pinned": False}
    return {"path": None, "major": None, "pinned": False}


def java_of(server: dict) -> dict:
    """Which Java this server runs on: the record's runtime, else its command."""
    major = server.get("java_major")
    if major:
        return {"path": f"java {major} runtime", "major": int(major), "pinned": True}
    return java_in_command(server.get("execution_command") or "")


# --- time -------------------------------------------------------------------


def uptime_seconds(stats: dict) -> int | None:
    """How long a server has been up, in seconds, or None.

    `started` is epoch seconds from a driver. An ISO-8601 string is accepted
    too, and so is the offset-less "YYYY-MM-DD HH:MM:SS" an imported record
    may carry -- read as UTC, because reading it against this host's clock
    is how a server started an hour ago once showed as starting four and a
    half hours in the future. A clock skew reads as unknown, never as a
    negative uptime.
    """
    started = stats.get("started")
    if started is None or started is False or started == "":
        return None
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        secs = now - float(started)
    elif isinstance(started, str):
        t = None
        try:
            t = datetime.datetime.fromisoformat(started)
        except ValueError:
            try:
                t = datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        secs = now - t.timestamp()
    else:
        return None
    return round(secs) if secs >= 0 else None
