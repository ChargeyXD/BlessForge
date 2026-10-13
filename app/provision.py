"""Creating a server, and getting a loader onto it whatever goes wrong.

Two things live here:

  1. **`install_loader`** -- everything between "the server exists" and "this
     server can start". The jar is resolved from the loader's own project
     (`app/loaders.py`), and then fetched by whoever can reach it:

        the node downloads it          (engine.install, `fetch`)
          ->  BlessForge downloads it, checks it, and uploads it
                                       (the node's own network may be the
                                        broken part: a home line with DNS
                                        trouble, a mirror that drops one IP)
          ->  say plainly what is broken

     For Forge and NeoForge the installer then runs once on the node, and
     the startup command is written from what it left behind -- a `run.sh`
     naming the args file on 1.17+, a runnable `forge-*.jar` before that.
     Getting that command wrong means every start re-runs the installer, so
     it is read back from the files rather than guessed.

  2. **`create_empty`** -- a server with no modpack: a loader, a version, and
     an empty `mods/` or `plugins/` folder.

The modpack installer calls the same ladder rather than carrying a copy of
it, so a fix to either helps both.
"""
from __future__ import annotations

import hashlib
import re
import time

from app import config, engine, loaders, optimizer, properties, specs, templates
from app.jobs import Job

# A Forge/NeoForge command that still runs the installer. Its presence means
# the loader is not installed yet, whatever else is in the folder.
INSTALLER_MARKER = "--installServer"


def _is_installer_family(family: str) -> bool:
    return loaders.family_of(family) in loaders.INSTALLER_FAMILIES


def loader_installed(family: str, command: str, names: set[str],
                     executable: str) -> bool:
    """Is there a launcher in this folder that the command actually starts?"""
    if _is_installer_family(family):
        return bool(command) and INSTALLER_MARKER not in command
    return bool(executable) and (
        executable.split("/")[-1] in names
        or executable in names
        or any(n.endswith(".jar") for n in names)
    )


def _verify(payload: bytes, src: dict) -> None:
    """Check a jar against whatever checksum its project publishes."""
    for algo in ("sha256", "sha1"):
        want = (src.get(algo) or "").lower()
        if want:
            got = hashlib.new(algo, payload).hexdigest()
            if got != want:
                raise RuntimeError(
                    f"The {src.get('filename')} downloaded from "
                    f"{src.get('source') or 'upstream'} is corrupt -- its "
                    f"{algo} is {got[:12]}..., the project publishes "
                    f"{want[:12]}...")


async def _fetch_loader(job: Job, server_id: str, family: str, mc_version: str,
                        src: dict, java: int) -> list[str]:
    """Get the loader jar into the server folder. Returns the rungs used."""
    fetch = {k: src.get(k) for k in ("url", "filename", "sha1", "sha256")
             if src.get(k)}
    rungs = ["node"]
    try:
        await engine.install(server_id, {
            "family": family, "mc_version": mc_version,
            "loader_version": src.get("loader_version"),
            "java_major": java,
            "fetch": fetch,
        })
        job.log_line(f"The node fetched {src['filename']} from "
                     f"{src.get('source') or 'upstream'}")
        return rungs
    except engine.EngineError as e:
        job.log_line(
            f"The node could not download {src['filename']} ({e}). "
            "Fetching it here and handing it over instead.", "warn")

    rungs.append("relay")
    job.set_step(f"Fetching {family} through BlessForge", 61)
    try:
        payload = await loaders.fetch(src["url"])
    except Exception as e:      # noqa: BLE001 -- reported as the last rung
        raise RuntimeError(_exhausted_message(family, mc_version, src, str(e)))
    _verify(payload, src)
    await engine.upload_file(server_id, ".", src["filename"], payload)
    job.log_line(f"Uploaded {src['filename']} ({len(payload) / 1048576:.1f} MB) "
                 f"from {src.get('source') or 'upstream'}")
    return rungs


def _exhausted_message(family: str, mc: str, src: dict, why: str) -> str:
    return (
        f"The {family} loader for Minecraft {mc} could not be fetched. The "
        f"node could not download it from {src.get('source') or 'its project'}, "
        f"and neither could BlessForge ({why}). That is almost always the "
        "project's download server being down, and it is usually back within "
        "the hour. The server has been left in place: use Finish setup to "
        "retry the loader rather than reinstalling.")


async def install_loader(job: Job, server_id: str, *, family: str,
                         mc_version: str, loader_version: str = "") -> dict:
    """Put a working launcher in the server folder and point the server at it.

    Returns a short record of what it did, so the install log can say "the
    node fetched it" or "the node could not, BlessForge relayed it" rather
    than being silent about a repair.
    """
    family = loaders.family_of(family)
    tpl = templates.get(family)
    job.set_step(f"Resolving {family} for Minecraft {mc_version}", 58)
    src = await loaders.resolve(family, mc_version, build=loader_version or None)
    if not src and loader_version:
        job.log_line(f"{family} {loader_version} is not published for "
                     f"{mc_version}; using the newest build instead.", "warn")
        src = await loaders.resolve(family, mc_version)
    if not src:
        raise RuntimeError(
            f"No {family} build for Minecraft {mc_version} was found. The "
            f"{family} project does not publish one, or did not answer.")

    java = await templates.java_major(family, mc_version)
    job.set_step(f"Installing {family} {src.get('loader_version') or ''}".strip(), 60)
    rungs = await _fetch_loader(job, server_id, family, mc_version, src, java)

    if tpl["install"]["fetch"] == "installer":
        job.set_step("Running the loader installer", 63)
        steps = templates.installer_steps(family, src["filename"])
        try:
            result = await engine.install(server_id, {
                "family": family, "mc_version": mc_version,
                "loader_version": src.get("loader_version"),
                "java_major": java,
                "run": steps["run"],
                "remove_after": steps["remove_after"],
                "timeout": max(steps["timeout"], config.SERVER_READY_TIMEOUT),
            })
        except engine.EngineError as e:
            tail = ((e.body or {}).get("output") or [])[-8:] if isinstance(
                e.body, dict) else []
            for line in tail:
                job.log_line(f"  {line}", "warn")
            raise RuntimeError(
                f"The {family} installer did not finish: {e}. The server has "
                "been left in place; Finish setup retries the loader.")
        for line in (result.get("output") or [])[-3:]:
            job.log_line(line)

    command, executable = await startup_command(
        server_id, family, mc_version, src)
    await engine.update_server(server_id, {"executable": executable,
                                           "execution_command": command})
    job.log_line(f"Startup command: {command}")
    runtime = await engine.set_runtime(server_id, mc_version, java)
    job.log_line(f"Runs on Java {runtime.get('java_major')}")
    return {"ok": True, "source": rungs[-1], "repairs": rungs[1:],
            "loader_version": src.get("loader_version"),
            "from": src.get("source")}


# The args line a modern Forge/NeoForge installer writes into run.sh:
#   java @user_jvm_args.txt @libraries/net/neoforged/neoforge/21.1.209/unix_args.txt "$@"
_RUN_SCRIPT = re.compile(
    r"java @([a-zA-Z0-9_.]+) @([a-z./\-]+)([0-9.\-]+(?:-[a-zA-Z0-9]+)?)"
    r"/([a-z_0-9]+\.txt)"
)

# Placeholder heap: the optimizer sizes it for real straight afterwards.
_DEFAULT_HEAP = "-Xms1G -Xmx2G"


async def startup_command(server_id: str, family: str, mc_version: str,
                          src: dict) -> tuple[str, str]:
    """(command, executable) for a freshly installed loader.

    Read from the folder, not assumed: what a Forge installer leaves differs
    by era, and a command naming a file that is not there is a server that
    never starts.
    """
    family = loaders.family_of(family)
    if templates.get(family)["install"]["fetch"] != "installer":
        jar = src["filename"]
        return templates.startup(family, jar=jar, jvm=_DEFAULT_HEAP), jar

    script = ""
    for candidate in ("run.sh", "run.bat"):
        try:
            script = await engine.read_file(server_id, candidate)
            if script:
                break
        except engine.EngineError:
            continue
    match = _RUN_SCRIPT.search(script or "")
    if match:
        args_file, lib_path, version, txt = match.groups()
        exec_path = f"{lib_path}{version}/"
        loader = "neoforge" if family == "neoforge" else "forge"
        command = templates.startup(family, args_file=f"{exec_path}{txt}")
        wanted = templates.jvm_args_file(family)
        if wanted and args_file != wanted:
            command = command.replace(f"@{wanted}", f"@{args_file}")
        return command, f"{exec_path}{loader}-{version}-server.jar"

    # Before 1.17 the installer leaves a runnable jar instead of a script.
    names = [n for n in await engine.list_dir(server_id, ".")
             if n.endswith(".jar") and n.startswith("forge-")
             and "installer" not in n]
    if names:
        jar = sorted(names, key=len)[0]
        return templates.startup(family, jar=jar, jvm=_DEFAULT_HEAP), jar
    raise RuntimeError(
        f"The {family} installer ran, but left neither a run script nor a "
        "server jar. Its output is on the Console tab.")


async def _has_args_command(server_id: str, args_file: str) -> bool:
    """Does the startup command read its flags from `args_file`?

    A modern Forge/NeoForge does; a pre-1.17 Forge runs a jar with the flags
    on the command line, and an args file would be written and ignored.
    """
    try:
        server = await engine.get_server(server_id)
    except Exception:       # noqa: BLE001
        return True
    return f"@{args_file}" in (server.get("execution_command") or "")


# The modpack installer's old name for the ladder. Kept so its call reads the
# same; there is nothing to wait FOR any more, only work to do.
async def ensure_loader(job: Job, server_id: str, *, family: str,
                        mc_version: str, loader_version: str = "") -> dict:
    return await install_loader(job, server_id, family=family,
                                mc_version=mc_version,
                                loader_version=loader_version)


# --- creating a server from nothing ------------------------------------


async def create_empty(
    job: Job,
    *,
    name: str,
    loader: str,
    mc_version: str,
    port: int = 25565,
    mem_min: int | None = None,
    mem_max: int | None = None,
    motd: str | None = None,
    optimize: bool = True,
    difficulty: str | None = None,
    gamemode: str | None = None,
    max_players: int | None = None,
    online_mode: bool = True,
    seed_mods: list[dict] | None = None,
) -> dict:
    """Create a bare server: a loader, a version, and an empty mod folder."""
    family = loaders.family_of(loader)
    tpl = templates.get(family)          # ValueError for a loader with no template
    if not re.match(r"^\d+\.\d+(\.\d+)?$", (mc_version or "").strip()):
        raise ValueError(f"'{mc_version}' is not a Minecraft version")

    host = await specs.host_for()
    ram_max = int(mem_max or min(6, max(2, int(host.get("total_ram_gb") or 8) // 2)))
    ram_min = int(mem_min or max(1, ram_max // 2))
    if ram_min > ram_max:
        ram_min = ram_max

    job.set_step(f"Creating {family} {mc_version}", 10)
    server_id = await engine.create_server(
        name=name, port=port, memory_mb=specs.container_memory_mb(ram_max),
        mc_version=mc_version, loader=family,
    )
    job.set_instance(server_id, name)
    job.emit("server_created", server_id, server_id=server_id)
    job.log_line(f"Created server {server_id}")

    # Stamp it immediately, marked unfinished: if the loader never arrives the
    # fleet list should say "half-finished" rather than showing a healthy
    # server with nothing in it.
    mod_dir = tpl["mod_directory"]
    try:
        await engine.write_manifest(server_id, {
            "schema": 1, "complete": False, "started_at": time.time(),
            "pack": {"name": name, "source": "empty"},
            "minecraft": mc_version, "loader": family,
            "mod_directory": mod_dir,
        })
    except Exception:
        pass

    result = await install_loader(job, server_id, family=family,
                                  mc_version=mc_version)

    job.set_step("Preparing the server directory", 78)
    # The mod or plugin folder is the entire point of an empty server, and a
    # loader that has never started has not created one.
    for folder in (mod_dir, "config", "logs", "crash-reports"):
        try:
            await engine.ensure_dir(server_id, folder)
        except Exception:
            job.log_line(f"Could not create {folder}/", "warn")

    job.set_step("Applying server settings", 86)
    try:
        await engine.write_file(server_id, "eula.txt", engine.EULA_ACCEPTED)
        job.log_line("EULA accepted")
    except Exception:
        job.log_line("Could not write eula.txt", "warn")

    prop_updates: dict[str, str] = {}
    if motd:
        prop_updates["motd"] = motd
    if difficulty:
        prop_updates["difficulty"] = difficulty
    if gamemode:
        prop_updates["gamemode"] = gamemode
    if max_players:
        prop_updates["max-players"] = str(int(max_players))
    prop_updates["online-mode"] = "true" if online_mode else "false"
    if prop_updates:
        try:
            await properties.save(server_id, prop_updates)
            job.log_line(", ".join(f"{k}={v}" for k, v in prop_updates.items()))
        except Exception as e:
            job.log_line(f"Could not write server.properties: {e}", "warn")

    try:
        set_port = await properties.set_port(server_id, port, force=True)
        for w in set_port.get("warnings", []):
            job.log_line(w, "warn")
    except Exception as e:
        job.log_line(f"Could not set the port to {port}: {e}", "warn")

    if optimize:
        job.set_step("Tuning for this host", 92)
        try:
            flags = [f["flag"] for f in specs.build_flag_plan(
                heap_gb=ram_max, host=host, mc_version=mc_version,
                loader=family) if f["enabled"]]
            args_file = templates.jvm_args_file(family)
            if args_file and await _has_args_command(server_id, args_file):
                await engine.write_file(server_id, args_file,
                                        specs.render_jvm_args(flags, ram_max))
            else:
                await optimizer.set_command_memory(server_id, ram_max, flags)
            job.log_line(f"Tuned JVM: {ram_max:g} GB heap, {len(flags)} flags")
        except Exception as e:
            job.log_line(f"Could not apply performance tuning: {e}", "warn")
    try:
        # The container follows the heap, and remembers who chose it.
        from app import heap as heaps
        sized = await heaps.resize(server_id, int(ram_max * 1024), by_user=bool(mem_max),
                                   mods=0, host=host)
        for w in sized["warnings"]:
            job.log_line(w, "warn")
    except Exception as e:
        job.log_line(f"Could not size the container: {e}", "warn")

    installed: list[dict] = []
    if seed_mods:
        job.set_step(f"Installing {len(seed_mods)} starter "
                     f"{'plugins' if mod_dir == 'plugins' else 'mods'}", 95)
        from app import mods as modmgr        # local: avoids an import cycle
        for entry in seed_mods:
            try:
                record = await modmgr.add_mod(
                    server_id,
                    source=entry.get("source", "modrinth"),
                    project_id=str(entry["project_id"]),
                    file_id=str(entry["file_id"]),
                    directory=mod_dir,
                )
                record["project_id"] = entry.get("project_id")
                record["file_id"] = entry.get("file_id")
                record["source"] = entry.get("source", "modrinth")
                installed.append(record)
                job.log_line(f"Installed {record.get('installed')}")
            except Exception as e:
                job.log_line(
                    f"Could not install {entry.get('name') or entry.get('project_id')}"
                    f": {e}", "warn")

    job.set_step("Recording the server manifest", 98)
    try:
        await engine.write_manifest(server_id, {
            "schema": 1,
            "complete": True,
            "installed_at": time.time(),
            "pack": {"name": name, "source": "empty",
                     "install_source": "blank",
                     "version": f"{family} {mc_version}"},
            "minecraft": mc_version,
            "loader": family,
            "loader_version": result.get("loader_version") or "",
            "mod_directory": mod_dir,
            "mods": [
                {"file": f"{mod_dir}/{r.get('installed')}", "name": r.get("name"),
                 "source": r.get("source"), "project_id": r.get("project_id"),
                 "file_id": r.get("file_id"), "version": r.get("version"),
                 "logo": r.get("logo")}
                for r in installed if r.get("installed")
            ],
            "problems": [],
            "loader_repairs": result.get("repairs") or [],
        })
    except Exception as e:
        job.log_line(f"Could not write the server manifest: {e}", "warn")

    job.log_line(
        f"'{name}' is ready — {family} {mc_version}, empty {mod_dir}/ folder, "
        f"port {port}"
    )
    return {
        "server_id": server_id,
        "name": name,
        "loader": family,
        "minecraft": mc_version,
        "port": port,
        "mod_directory": mod_dir,
        "takes_plugins": loaders.takes_plugins(family),
        "heap_gb": ram_max,
        "loader_source": result.get("source"),
        "repairs": result.get("repairs") or [],
        "mods_installed": len(installed),
    }


async def reinstall_loader(job: Job, server_id: str) -> dict:
    """Retry the loader for a server that was left without one.

    The one recovery path an interrupted create used to have was "delete it
    and start again", which throws away the port, the name and anything
    already uploaded.
    """
    manifest = await engine.read_manifest(server_id)
    server = await engine.get_server(server_id)
    family = loaders.family_of(manifest.get("loader") or "")
    mc = manifest.get("minecraft") or ""
    if not mc:
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", server.get("executable") or "")
        mc = m.group(1) if m else ""
    if not family or not mc:
        raise ValueError(
            "This server does not record which loader or Minecraft version "
            "it was built for, so the loader cannot be reinstalled "
            "automatically."
        )
    job.log_line(f"Reinstalling {family} for Minecraft {mc}")
    result = await install_loader(job, server_id, family=family, mc_version=mc,
                                  loader_version=manifest.get("loader_version") or "")
    try:
        manifest["complete"] = True
        await engine.write_manifest(server_id, manifest)
    except Exception:
        pass
    return {"server_id": server_id, "loader": family, "minecraft": mc, **result}
