"""One hardened container per server, and the one function that builds it.

`container_spec()` turns a server's desired state into a Docker Engine API
create body, and `assert_safe()` refuses any body that breaks the rules --
it runs before EVERY create, including the agent's own, so a bug in the
spec builder becomes a refused start rather than an escape:

  * never the Docker socket, never privileged, no added capabilities, never
    the host's network, PID or IPC namespace;
  * `Memory` equal to `MemorySwap` (no swap to hide in), a CPU quota, a PID
    limit (a fork bomb stops at it), a block-IO weight;
  * every capability dropped, no-new-privileges, a read-only root
    filesystem with only /tmp writable (tmpfs, noexec). The JVM's own temp
    folder is in the server folder instead (NATIVE_TMP): libraries such as
    JNA and Netty unpack a native library there and load it, which a noexec
    /tmp refuses -- Beyond Depth would not boot;
  * an unprivileged uid, and exactly one bind mount: the server's own
    folder, at /server;
  * the game port published on the node's game address only, plus the
    ports its add-ons open (agent/addons.py) -- each on the same number on
    both sides, never below 1024, and never more than a handful.

The agent restarts servers itself (RestartPolicy "no"), so a crash loop is
something it can see and report rather than something Docker hides.

Two runtimes implement the same small interface:

  * `DockerRuntime` -- the real one, speaking the Docker Engine API over its
    unix socket (or tcp) with httpx; console I/O over a hijacked attach.
  * `ProcessRuntime` -- the server as a plain child process, NO isolation.
    For developing on a machine without Docker; refused by the config
    loader unless explicitly allowed.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shlex
import struct
import tarfile
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from agent import clients
from agent.config import AgentConfig

CONTAINER_PREFIX = "bf-"
# The runtime images' own Dockerfile, shipped with the agent: what it builds
# from when an image cannot be pulled.
RUNTIME_CONTEXT = Path(__file__).resolve().parent / "images"
SERVER_MOUNT = "/server"
# The JVM's temp folder, inside the server folder, emptied at each start.
NATIVE_TMP = ".blessforge-tmp"
# The game port on two protocols, and agent/addons.py's MAX_ADDON_PORTS.
MAX_PUBLISHED = 2 + 8


_PORT_TAKEN = re.compile(
    r":(\d{1,5})(?: failed: port is already allocated|: bind: address already in use)")


def port_in_use(docker_error: str) -> int | None:
    """The host port a failed start could not publish, from Docker's error:
    "Bind for 0.0.0.0:8123 failed: port is already allocated" (another
    container has it) or "listen udp4 0.0.0.0:19132: bind: address already
    in use" (a program on the host does)."""
    m = _PORT_TAKEN.search(docker_error or "")
    return int(m.group(1)) if m else None


class RuntimeError_(Exception):
    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


class UnsafeSpec(RuntimeError_):
    def __init__(self, message: str):
        super().__init__(f"refusing an unsafe container: {message}", 500)


# --- the spec ---------------------------------------------------------------------


def argv_of(command: str) -> list[str]:
    """The startup command as argv, run WITHOUT a shell.

    `java @user_jvm_args.txt @libraries/.../unix_args.txt nogui` needs no
    shell, and not having one means the command line cannot do anything a
    shell could -- redirect, chain, substitute -- whatever is typed into it.
    """
    argv = shlex.split(command or "")
    if not argv:
        raise RuntimeError_("this server has no startup command yet", 409)
    return argv


def limits_of(spec: dict) -> dict:
    lim = dict(spec.get("limits") or {})
    memory_mb = int(spec.get("memory_mb") or lim.get("memory_mb") or 2048)
    return {
        "memory_mb": max(256, memory_mb),
        # No plan limit means the node's own cores. The default used to be
        # 100 -- ONE core -- so every pack on a self-hosted node booted on a
        # single core of eight, and a 370-mod pack took a quarter of an hour.
        "cpu_pct": max(10, int(lim.get("cpu_pct") or (os.cpu_count() or 1) * 100)),
        "io_weight": min(1000, max(10, int(lim.get("io_weight") or 500))),
        "disk_mb": int(lim.get("disk_mb") or 0),
    }


def container_spec(server: dict, cfg: AgentConfig, folder: Path) -> dict:
    """The Docker create body for one server. Pure: same input, same output."""
    port = int(server["port"])
    lim = limits_of(server)
    mem = lim["memory_mb"] * 1024 * 1024
    # The game port on both protocols (query and some voice mods share it),
    # then each add-on port the agent found in the folder.
    published = [f"{port}/tcp", f"{port}/udp"]
    for extra in server.get("addon_ports") or []:
        key = f"{int(extra['port'])}/{extra['proto']}"
        if key not in published:
            published.append(key)
    ports = {k: {} for k in published}
    bindings = {k: [{"HostIp": cfg.game_bind_ip, "HostPort": k.split("/")[0]}]
                for k in published}
    return {
        "Image": cfg.image_for(server.get("java_major")),
        "Entrypoint": ["/usr/local/bin/bf-entry"],
        "Cmd": argv_of(server.get("execution_command") or ""),
        "WorkingDir": SERVER_MOUNT,
        "User": f"{cfg.container_uid}:{cfg.container_gid}",
        "Env": [
            f"SERVER_MEMORY={lim['memory_mb']}",
            f"SERVER_PORT={port}",
            "HOME=/server",
            "TZ=UTC",
            # Before the command's own flags, so a pack that sets its own
            # temp folder still wins.
            f"JAVA_TOOL_OPTIONS=-Djava.io.tmpdir={SERVER_MOUNT}/{NATIVE_TMP}",
        ],
        "OpenStdin": True, "StdinOnce": False,
        "AttachStdin": True, "AttachStdout": True, "AttachStderr": True,
        "Tty": False,
        "ExposedPorts": ports,
        "Labels": {"io.blessforge.server": server["uuid"],
                   "io.blessforge.node": cfg.node_id},
        "HostConfig": {
            "Binds": [f"{folder}:{SERVER_MOUNT}:rw"],
            "Memory": mem,
            "MemorySwap": mem,
            "NanoCpus": int(lim["cpu_pct"] / 100 * 1_000_000_000),
            "PidsLimit": cfg.pids_limit,
            "BlkioWeight": lim["io_weight"],
            "CapDrop": ["ALL"],
            "CapAdd": [],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
            "Tmpfs": {"/tmp": f"rw,noexec,nosuid,nodev,size={cfg.tmp_size_mb}m"},
            "PortBindings": bindings,
            "NetworkMode": cfg.game_network,
            "RestartPolicy": {"Name": "no"},
            "Privileged": False,
            "IpcMode": "private",
            "PidMode": "",
            "Init": True,
            "Ulimits": [{"Name": "nofile", "Soft": 65536, "Hard": 65536}],
            "LogConfig": {"Type": "json-file",
                          "Config": {"max-size": "10m", "max-file": "2"}},
        },
    }


def installer_spec(server: dict, cfg: AgentConfig, folder: Path,
                   argv: list[str]) -> dict:
    """A short-lived container that runs a loader installer.

    The same limits and the same lock-down as the server -- it is running
    code the internet handed us -- except that it gets a network, because a
    Forge installer downloads its libraries, and publishes no port.
    """
    spec = container_spec({**server, "execution_command": shlex.join(argv)},
                          cfg, folder)
    spec["Labels"]["io.blessforge.role"] = "installer"
    spec["OpenStdin"] = spec["AttachStdin"] = False
    spec["ExposedPorts"] = {}
    spec["HostConfig"]["PortBindings"] = {}
    return spec


def fresh_native_tmp(folder: Path) -> None:
    """An empty NATIVE_TMP for this start, through the jail like any other
    write into a server folder (a link planted there is removed, never
    followed)."""
    from agent import files
    try:
        files.delete(folder, [NATIVE_TMP])
    except files.JailError:
        pass
    files.mkdir(folder, ".", NATIVE_TMP)


_FORBIDDEN_MOUNTS = ("/var/run/docker.sock", "/run/docker.sock", "/proc", "/sys",
                     "/dev", "/etc", "/root", "/home", "/var/lib/docker")


def assert_safe(spec: dict, cfg: AgentConfig, folder: Path) -> None:
    """Refuse any container that breaks the isolation rules. Never relaxed."""
    hc = spec.get("HostConfig") or {}
    if hc.get("Privileged"):
        raise UnsafeSpec("privileged")
    if hc.get("CapAdd"):
        raise UnsafeSpec(f"added capabilities {hc['CapAdd']}")
    if [c.upper() for c in hc.get("CapDrop") or []] != ["ALL"]:
        raise UnsafeSpec("capabilities are not all dropped")
    opts = [o.replace(" ", "").lower() for o in hc.get("SecurityOpt") or []]
    if not any(o.startswith("no-new-privileges") and not o.endswith("false")
               for o in opts):
        raise UnsafeSpec("no-new-privileges is not set")
    for o in opts:
        if o.endswith("unconfined") or o in ("label=disable", "label:disable"):
            raise UnsafeSpec(f"security option {o}")
    for mode, label in ((hc.get("NetworkMode"), "network"),
                        (hc.get("PidMode"), "PID namespace"),
                        (hc.get("IpcMode"), "IPC namespace"),
                        (hc.get("UTSMode"), "UTS namespace"),
                        (hc.get("UsernsMode"), "user namespace"),
                        (hc.get("CgroupnsMode"), "cgroup namespace"),
                        (hc.get("Cgroup"), "cgroup")):
        if mode == "host":
            raise UnsafeSpec(f"the host's {label}")
        if isinstance(mode, str) and mode.startswith("container:"):
            raise UnsafeSpec(f"another container's {label}")
    if not hc.get("Memory") or hc.get("MemorySwap") != hc.get("Memory"):
        raise UnsafeSpec("memory limit missing, or swap not equal to it")
    if not hc.get("NanoCpus"):
        raise UnsafeSpec("no CPU limit")
    if not hc.get("PidsLimit") or hc["PidsLimit"] <= 0:
        raise UnsafeSpec("no PID limit")
    if not hc.get("ReadonlyRootfs"):
        raise UnsafeSpec("the root filesystem is writable")
    if hc.get("Devices") or hc.get("DeviceRequests") or hc.get("DeviceCgroupRules"):
        raise UnsafeSpec("host devices")
    if hc.get("Sysctls") or hc.get("CgroupParent") or hc.get("Runtime") not in (None, "", "runc"):
        raise UnsafeSpec("sysctls, a cgroup parent or another OCI runtime")
    if hc.get("VolumesFrom") or hc.get("Mounts") or hc.get("Volumes"):
        raise UnsafeSpec("mounts other than the server folder")
    binds = hc.get("Binds") or []
    want = f"{folder}:{SERVER_MOUNT}:rw"
    if binds != [want]:
        raise UnsafeSpec(f"bind mounts {binds}; only {want} is allowed")
    host_side = str(folder).replace("\\", "/")
    root = str(cfg.servers_root).replace("\\", "/").rstrip("/") + "/"
    if not host_side.startswith(root) or ".." in host_side.split("/"):
        raise UnsafeSpec(f"{folder} is not under {cfg.servers_root}")
    if any(host_side.startswith(f) or host_side == f.rstrip("/")
           for f in _FORBIDDEN_MOUNTS):
        raise UnsafeSpec(f"{folder} is a system path")
    user = str(spec.get("User") or "")
    if not user or user.split(":")[0] in ("0", "root"):
        raise UnsafeSpec("it would run as root")
    bindings = hc.get("PortBindings") or {}
    if len(bindings) > MAX_PUBLISHED:
        raise UnsafeSpec(f"{len(bindings)} published ports")
    for name, binding in bindings.items():
        number, _, proto = name.partition("/")
        if proto not in ("tcp", "udp") or not number.isdigit():
            raise UnsafeSpec(f"published port {name}")
        for b in binding or []:
            if (b.get("HostIp") or "") != cfg.game_bind_ip:
                raise UnsafeSpec(f"port {name} published on {b.get('HostIp') or 'every address'}")
            if str(b.get("HostPort") or "") != number or int(number) < 1024:
                raise UnsafeSpec(f"port {name} published as {b.get('HostPort') or 'any port'}")
    tmpfs = hc.get("Tmpfs") or {}
    if set(tmpfs) - {"/tmp"}:
        raise UnsafeSpec(f"tmpfs mounts {sorted(tmpfs)}")
    if hc.get("RestartPolicy", {}).get("Name") not in (None, "", "no"):
        raise UnsafeSpec("a Docker restart policy (the agent restarts servers itself)")


# --- the runtime interface ----------------------------------------------------------

# on_output(line) and on_exit(code, info) are called by the runtime for a running
# server; `info` says how it ended where the runtime knows ({"oom": True} when
# the kernel killed it for its memory limit).
Output = Callable[[str], None]
Exited = Callable[..., Awaitable[None]]


class Running:
    """A handle on one running server."""

    async def send(self, line: str) -> None: ...
    async def stop(self, timeout: int) -> None: ...
    async def kill(self) -> None: ...
    async def stats(self) -> dict: ...


# --- Docker -------------------------------------------------------------------------


def _docker_client(cfg: AgentConfig, timeout: float = 60) -> clients.Lent:
    """The node's one Docker client, lent for an `async with` with `timeout`."""
    host = cfg.docker_host

    def make() -> httpx.AsyncClient:
        if host.startswith("unix://"):
            transport = httpx.AsyncHTTPTransport(uds=host[len("unix://"):])
            return httpx.AsyncClient(transport=transport, base_url="http://docker",
                                     timeout=60)
        return httpx.AsyncClient(base_url=host.replace("tcp://", "http://"), timeout=60)
    return clients.Lent(clients.client(f"docker:{host}", make), timeout)


def _docker_time(value: str | None) -> float | None:
    """Docker's RFC 3339 time (nanoseconds, Z) as epoch seconds."""
    if not value or value.startswith("0001-"):
        return None
    from datetime import datetime
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", value)
    if not m:
        return None
    base = datetime.fromisoformat(m.group(1) + "+00:00").timestamp()
    return base + float(m.group(2) or 0)


def demux(buffer: bytearray) -> list[tuple[int, bytes]]:
    """Split Docker's multiplexed attach stream into (stream, payload) frames.

    Each frame is an 8-byte header -- stream type (1 stdout, 2 stderr), three
    zero bytes, a big-endian uint32 length -- then the payload. Consumes
    whole frames from `buffer` and leaves a partial one in it.
    """
    out = []
    while len(buffer) >= 8:
        stream_type = buffer[0]
        (length,) = struct.unpack(">I", bytes(buffer[4:8]))
        if len(buffer) < 8 + length:
            break
        out.append((stream_type, bytes(buffer[8:8 + length])))
        del buffer[:8 + length]
    return out


class DockerRunning(Running):
    def __init__(self, rt: "DockerRuntime", name: str, writer, reader_task):
        self.rt = rt
        self.name = name
        self.writer = writer
        self.reader_task = reader_task
        # Set when the agent lets go of a server that keeps running (its
        # own shutdown): the end of the console stream is then not an exit.
        self.detached = False
        # The previous reading's (container CPU, host CPU) totals: CPU use is
        # the change between two readings.
        self._cpu_before: tuple[int, int] | None = None

    async def send(self, line: str) -> None:
        self.writer.write((line.rstrip("\n") + "\n").encode())
        await self.writer.drain()

    async def stop(self, timeout: int) -> None:
        """`stop` on the console, then wait for the server to save and exit.

        Only if it has not exited after `timeout` seconds does Docker's own
        stop follow. That is SIGTERM at once and SIGKILL ten seconds later --
        sending it straight after `stop`, as this once did, raced the two
        shutdowns, and on Paper the signal won: the world was saved by a
        JVM shutdown hook instead of by the server's own stop.
        """
        try:
            await self.send("stop")
        except Exception:       # noqa: BLE001 -- the fallback below still happens
            pass
        try:
            # The pump ends when the attach stream closes -- the process is
            # gone. shield(): a timeout here must not cancel the pump.
            await asyncio.wait_for(asyncio.shield(self.reader_task), timeout)
            return
        except asyncio.TimeoutError:
            pass
        async with _docker_client(self.rt.cfg, 60) as c:
            await c.post(f"/containers/{self.name}/stop", params={"t": 10})

    async def kill(self) -> None:
        async with _docker_client(self.rt.cfg) as c:
            await c.post(f"/containers/{self.name}/kill")

    async def detach(self) -> None:
        """Let go of the console and leave the server running: the agent is
        shutting down (an update, a restart), and the next one re-adopts it."""
        self.detached = True
        try:
            self.writer.close()
        except Exception:       # noqa: BLE001
            pass
        if self.reader_task is not None:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except (asyncio.CancelledError, Exception):     # noqa: BLE001
                pass

    async def stats(self) -> dict:
        """One quick reading. `one-shot`: without it Docker waits for a second
        sample of its own, 1-2 s per call. CPU is the change since this
        server's previous reading, so the first one after a start has none."""
        async with _docker_client(self.rt.cfg, 15) as c:
            r = await c.get(f"/containers/{self.name}/stats",
                            params={"stream": "false", "one-shot": "true"})
            if r.status_code >= 400:
                return {}
            s = r.json()
        return self._reading(s)

    def _reading(self, s: dict) -> dict:
        cpu = s.get("cpu_stats") or {}
        total = int(cpu.get("cpu_usage", {}).get("total_usage", 0) or 0)
        system = int(cpu.get("system_cpu_usage", 0) or 0)
        cores = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {})
                                              .get("percpu_usage") or []) or 1
        before, self._cpu_before = self._cpu_before, (total, system)
        pct = None
        if before and system > before[1] and total >= before[0]:
            pct = round((total - before[0]) / (system - before[1]) * cores * 100, 1)
        mem = s.get("memory_stats") or {}
        used = mem.get("usage", 0) - (mem.get("stats") or {}).get("inactive_file", 0)
        limit = mem.get("limit") or 1
        return {"cpu": pct, "mem": int(used), "mem_percent": round(used / limit * 100, 1)}


class DockerRuntime:
    name = "docker"

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def container(self, uuid: str) -> str:
        return CONTAINER_PREFIX + uuid

    async def info(self) -> dict:
        async with _docker_client(self.cfg, 10) as c:
            v = (await c.get("/version")).json()
            listed = (await c.get("/containers/json",
                                  params={"filters": json.dumps(
                                      {"label": ["io.blessforge.server"]})})).json()
        return {"docker": v.get("Version"), "api": v.get("ApiVersion"),
                "running_containers": len(listed)}

    async def _ensure_image(self, c: clients.Lent, image: str) -> None:
        if (await c.get(f"/images/{image}/json")).status_code == 200:
            return
        repo, _, tag = image.rpartition(":")
        r = await c.post("/images/create", params={"fromImage": repo, "tag": tag},
                         timeout=1800)
        if r.status_code < 400 and '"error"' not in r.text:
            return
        # Not pullable (not published, or a private registry this machine is
        # not logged in to): build it here from the Dockerfile the agent
        # ships with -- a JRE on Temurin's public image, nothing else.
        java = re.fullmatch(r"java(\d+)", tag or "")
        if not java or not RUNTIME_CONTEXT.is_dir():
            raise RuntimeError_(f"could not pull {image}: {r.text[:200]}", 502)
        await self._build_runtime(c, image, int(java.group(1)))

    async def _build_runtime(self, c: clients.Lent, image: str, java: int) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for f in sorted(RUNTIME_CONTEXT.iterdir()):
                if f.is_file():
                    tar.add(f, arcname=f.name)
        r = await c.post("/build", params={"t": image, "pull": "1", "rm": "1",
                                           "buildargs": json.dumps({"JAVA": str(java)})},
                         content=buf.getvalue(),
                         headers={"Content-Type": "application/x-tar"}, timeout=1800)
        # The build streams JSON lines; a failure is a line with "error".
        failed = [ln for ln in r.text.splitlines() if '"error"' in ln]
        if r.status_code >= 400 or failed:
            raise RuntimeError_(f"could not build {image}: "
                                f"{(failed[-1] if failed else r.text)[:300]}", 502)

    async def _recreate(self, c, name: str, spec: dict, *,
                        refuse_running: bool = False) -> None:
        if refuse_running:
            # A game server still running (an agent restart that did not
            # re-adopt it) is never force-deleted: that kills it unsaved.
            r = await c.get(f"/containers/{name}/json")
            if r.status_code == 200 and ((r.json() or {}).get("State") or {}).get("Running"):
                raise RuntimeError_("This server is already running.", 409)
        await c.delete(f"/containers/{name}", params={"force": "true"})
        r = await c.post("/containers/create", params={"name": name}, json=spec)
        if r.status_code >= 400:
            raise RuntimeError_(f"Docker refused the container: {r.text[:300]}", 502)

    async def start(self, server: dict, folder: Path, on_output: Output,
                    on_exit: Exited) -> Running:
        fresh_native_tmp(folder)
        name = self.container(server["uuid"])
        server = {**server, "addon_ports": list(server.get("addon_ports") or [])}
        async with _docker_client(self.cfg, 120) as c:
            while True:
                spec = container_spec(server, self.cfg, folder)
                assert_safe(spec, self.cfg, folder)
                await self._ensure_image(c, spec["Image"])
                await self._recreate(c, name, spec, refuse_running=True)
                reader, writer = await self._attach(name)
                r = await c.post(f"/containers/{name}/start")
                if r.status_code < 400:
                    break
                writer.close()
                taken = port_in_use(r.text)
                if taken is None:
                    raise RuntimeError_(f"Docker could not start it: {r.text[:300]}", 502)
                if taken == int(server["port"]):
                    raise RuntimeError_(
                        f"Port {taken} is already in use on this machine -- by another "
                        "program or container (Crafty publishes 25500-25600, for one). "
                        "Give this server a free port and start it again.", 409)
                # An add-on's port: start without it rather than not at all.
                dropped = [a for a in server["addon_ports"] if int(a["port"]) == taken]
                if not dropped:
                    raise RuntimeError_(f"Docker could not start it: {r.text[:300]}", 502)
                server["addon_ports"] = [a for a in server["addon_ports"]
                                         if int(a["port"]) != taken]
                for a in dropped:
                    where = (f"in {a['from']}" if a.get("from")
                             else "in its config file, which it writes on this first start,")
                    on_output(f"[BlessForge] {a['what']} wants port {taken}/{a['proto']}, "
                              "but something else on this machine already has it, so it "
                              f"is not published. Change its port {where} to a free one.")

        return self._follow(name, reader, writer, on_output, on_exit)

    def _follow(self, name: str, reader, writer, on_output: Output,
                on_exit: Exited) -> "DockerRunning":
        """A handle on a running container: its console pumped into
        on_output, and on_exit called with how it ended."""
        running = DockerRunning(self, name, writer, None)

        async def pump():
            buf = bytearray()
            pending = b""
            try:
                while True:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    for _stream, payload in demux(buf):
                        pending += payload
                        *lines, pending = pending.split(b"\n")
                        for ln in lines:
                            on_output(ln.decode("utf-8", "replace").rstrip("\r"))
            finally:
                if running.detached:
                    return
                code = None
                oom = False
                try:
                    async with _docker_client(self.cfg) as c2:
                        w = await c2.post(f"/containers/{name}/wait", timeout=30)
                        code = (w.json() or {}).get("StatusCode")
                        # Exit 137 is any SIGKILL; only Docker knows whether
                        # the kernel's OOM killer sent it. Prominence II died
                        # this way mid-boot with nothing in its own log.
                        i = await c2.get(f"/containers/{name}/json")
                        oom = bool(((i.json() or {}).get("State") or {}).get("OOMKilled"))
                except Exception:       # noqa: BLE001
                    pass
                await on_exit(code, {"oom": oom})

        running.reader_task = asyncio.create_task(pump())
        return running

    async def running_servers(self) -> dict[str, dict]:
        """This node's game servers whose containers are running right now,
        by server id -- what an agent restart finds still going."""
        async with _docker_client(self.cfg, 15) as c:
            r = await c.get("/containers/json", params={"filters": json.dumps(
                {"label": [f"io.blessforge.node={self.cfg.node_id}"],
                 "status": ["running"]})})
            if r.status_code >= 400:
                raise RuntimeError_(f"Docker would not list containers: {r.text[:200]}", 502)
            listed = r.json() or []
        out = {}
        for item in listed:
            labels = item.get("Labels") or {}
            uuid = labels.get("io.blessforge.server")
            if not uuid or labels.get("io.blessforge.role"):
                continue            # an installer, not a server
            if f"/{self.container(uuid)}" in (item.get("Names") or []):
                out[uuid] = item
        return out

    async def adopt(self, server: dict, on_output: Output, on_exit: Exited,
                    tail: int = 2000) -> tuple["DockerRunning", float | None]:
        """Take a running server back after an agent restart, without
        touching it: its last `tail` lines go to on_output first, then the
        console follows it live and commands reach its stdin again.
        Returns the handle and when the container started."""
        name = self.container(server["uuid"])
        async with _docker_client(self.cfg, 30) as c:
            r = await c.get(f"/containers/{name}/json")
            state = ((r.json() or {}).get("State") or {}) if r.status_code == 200 else {}
            if not state.get("Running"):
                raise RuntimeError_(f"{name} is not running", 409)
            logs = await c.get(f"/containers/{name}/logs",
                               params={"stdout": 1, "stderr": 1, "tail": tail})
        buf = bytearray(logs.content if logs.status_code == 200 else b"")
        text = b"".join(payload for _s, payload in demux(buf))
        for ln in text.decode("utf-8", "replace").splitlines()[-tail:]:
            on_output(ln.rstrip("\r"))
        reader, writer = await self._attach(name)
        return self._follow(name, reader, writer, on_output, on_exit), \
            _docker_time(state.get("StartedAt"))

    async def _attach(self, name: str):
        """A hijacked attach: stdin in, multiplexed stdout/stderr out."""
        host = self.cfg.docker_host
        if host.startswith("unix://"):
            reader, writer = await asyncio.open_unix_connection(host[len("unix://"):])
        else:
            hostport = host.split("://", 1)[-1]
            h, _, p = hostport.partition(":")
            reader, writer = await asyncio.open_connection(h, int(p or 2375))
        writer.write(
            f"POST /containers/{name}/attach?stream=1&stdin=1&stdout=1&stderr=1 HTTP/1.1\r\n"
            "Host: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n"
            "Content-Length: 0\r\n\r\n".encode())
        await writer.drain()
        status = await reader.readline()
        if b" 101 " not in status and b" 200 " not in status:
            writer.close()
            raise RuntimeError_(f"Docker refused the console: {status!r}", 502)
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        return reader, writer

    async def remove(self, uuid: str) -> None:
        async with _docker_client(self.cfg) as c:
            await c.delete(f"/containers/{self.container(uuid)}", params={"force": "true"})

    async def run_installer(self, server: dict, folder: Path, argv: list[str],
                            timeout: int, on_output: Output) -> int:
        spec = installer_spec(server, self.cfg, folder, argv)
        fresh_native_tmp(folder)
        assert_safe(spec, self.cfg, folder)
        name = self.container(server["uuid"]) + "-install"
        async with _docker_client(self.cfg, timeout + 60) as c:
            await self._ensure_image(c, spec["Image"])
            await self._recreate(c, name, spec)
            await c.post(f"/containers/{name}/start")
            try:
                w = await c.post(f"/containers/{name}/wait", timeout=timeout)
                code = (w.json() or {}).get("StatusCode")
            except httpx.TimeoutException:
                await c.post(f"/containers/{name}/kill")
                code = None
            logs = await c.get(f"/containers/{name}/logs",
                               params={"stdout": 1, "stderr": 1, "tail": 200})
            buf = bytearray(logs.content)
            for _s, payload in demux(buf):
                for ln in payload.decode("utf-8", "replace").splitlines():
                    on_output(ln)
            await c.delete(f"/containers/{name}", params={"force": "true"})
        if code is None:
            raise RuntimeError_(f"the installer ran past {timeout}s and was stopped", 504)
        return int(code)


# --- a plain process (development only) ---------------------------------------------


class ProcessRunning(Running):
    def __init__(self, proc: asyncio.subprocess.Process, task):
        self.proc = proc
        self.task = task
        self._ps = None

    async def send(self, line: str) -> None:
        if self.proc.stdin is None or self.proc.returncode is not None:
            raise RuntimeError_("the server is not running", 409)
        self.proc.stdin.write((line.rstrip("\n") + "\n").encode())
        await self.proc.stdin.drain()

    async def stop(self, timeout: int) -> None:
        try:
            await self.send("stop")
        except Exception:       # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout)
        except asyncio.TimeoutError:
            self.proc.kill()

    async def kill(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()

    async def stats(self) -> dict:
        return await asyncio.to_thread(self._reading)

    def _reading(self) -> dict:
        """CPU since the previous reading (no blocking interval), and memory."""
        try:
            import psutil
            if self._ps is None:
                self._ps = psutil.Process(self.proc.pid)
            p = self._ps
            mem = sum(c.memory_info().rss for c in [p, *p.children(recursive=True)])
            return {"cpu": p.cpu_percent(None), "mem": mem, "mem_percent": None}
        except Exception:       # noqa: BLE001
            return {}


class ProcessRuntime:
    """The server as a child process. NO isolation: development only."""
    name = "process"

    def __init__(self, cfg: AgentConfig):
        if not cfg.allow_unsafe_process_runtime:
            raise RuntimeError_("the process runtime is not allowed on this node")
        self.cfg = cfg

    async def info(self) -> dict:
        return {"docker": None, "runtime": "process (no isolation)",
                "running_containers": 0}

    def _argv(self, server: dict, folder: Path, argv: list[str]) -> list[str]:
        if argv and argv[0] == "java":
            java = self.cfg.java_paths.get(int(server.get("java_major") or 21))
            if java:
                argv = [java, *argv[1:]]
        if os.name == "nt":
            # Forge/NeoForge write unix_args.txt with ':' separators, which a
            # Windows JVM reads as part of a path. They also write
            # win_args.txt beside it; use that one here.
            out = []
            for a in argv:
                if a.startswith("@") and a.endswith("unix_args.txt"):
                    alt = a[:-len("unix_args.txt")] + "win_args.txt"
                    if (folder / alt[1:]).exists():
                        a = alt
                out.append(a)
            argv = out
        return argv

    async def start(self, server: dict, folder: Path, on_output: Output,
                    on_exit: Exited) -> Running:
        argv = self._argv(server, folder, argv_of(server.get("execution_command") or ""))
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(folder), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **kwargs)

        async def pump():
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                on_output(line.decode("utf-8", "replace").rstrip("\r\n"))
            await on_exit(await proc.wait())

        return ProcessRunning(proc, asyncio.create_task(pump()))

    async def remove(self, uuid: str) -> None:
        return None

    async def run_installer(self, server: dict, folder: Path, argv: list[str],
                            timeout: int, on_output: Output) -> int:
        argv = self._argv(server, folder, argv)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(folder), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)

        async def read():
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                on_output(line.decode("utf-8", "replace").rstrip("\r\n"))
        try:
            await asyncio.wait_for(asyncio.gather(read(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError_(f"the installer ran past {timeout}s and was stopped", 504)
        return int(proc.returncode or 0)


def runtime_for(cfg: AgentConfig):
    return DockerRuntime(cfg) if cfg.runtime == "docker" else ProcessRuntime(cfg)

