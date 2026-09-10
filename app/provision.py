"""Creating an instance, and getting a loader onto it whatever goes wrong.

Two things live here:

  1. **`ensure_loader`** -- the retry ladder that stands between "Crafty said
     201" and "this instance can actually start". Crafty fetches loader jars
     from a single mirror on a daemon thread; when that fails it logs one
     line and stops, the API still reports success, and the instance is left
     with nothing but eula.txt. The ladder is:

        Crafty's own download  ->  Crafty's jar index, fetched by us
                               ->  the loader's upstream project
                               ->  say plainly what is broken

     Only the last rung is a failure, and it names the mirror rather than
     blaming the user.

  2. **`create_empty`** -- a server with no modpack: a loader, a version, and
     an empty `mods/` or `plugins/` folder. Most servers do not start life as
     a downloaded pack, and until now there was no way to make one here at
     all.

The loader ladder is shared with the modpack installer rather than
duplicated, so a fix to either helps both.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time

from app import config, crafty, loaders, optimizer, properties, specs
from app.jobs import Job

# Crafty points `execution_command` at the loader INSTALLER
# (`-jar forge-installer-1.20.1.jar --installServer`) and only rewrites it
# once its own installer thread finishes. The presence of a command means
# nothing; the absence of this marker is what says the loader is installed.
INSTALLER_MARKER = "--installServer"

# Crafty sleeps 3s before starting its download, then allows three retries on
# a 2/4/8s backoff -- about 17s to fail. Past this the jar is not coming.
DOWNLOAD_GRACE = 75

# Crafty streams the jar straight to its final path, so a download in flight
# is a file whose mtime keeps advancing. Nothing moving for this long means
# whatever was happening has stopped -- including a connection that died
# mid-stream and left a truncated jar, which "is there a .jar" cannot see.
# Three minutes because Crafty reports mtimes at minute resolution.
STALL_SECONDS = 180


def _is_installer_family(family: str) -> bool:
    return loaders.family_of(family) in loaders.INSTALLER_FAMILIES


def loader_installed(family: str, command: str, names: set[str],
                     executable: str) -> bool:
    if _is_installer_family(family):
        return bool(command) and INSTALLER_MARKER not in command
    return bool(executable) and (
        executable.split("/")[-1] in names
        or executable in names
        or any(n.endswith(".jar") for n in names)
    )


async def ensure_loader(job: Job, server_id: str, *, family: str,
                        mc_version: str, crafty_loader: str = "") -> dict:
    """Block until the instance has a working launcher, repairing if needed.

    Returns a short record of what it had to do, so the install log can say
    "Crafty handled it" or "Crafty's mirror was down, we fetched it from
    NeoForged" rather than being silent about a repair.
    """
    family = loaders.family_of(family)
    crafty_loader = crafty_loader or loaders.crafty_key(family)
    job.set_step("Waiting for Crafty to install the loader", 60)

    deadline = time.time() + config.SERVER_READY_TIMEOUT
    grace_until = time.time() + DOWNLOAD_GRACE
    last_sig: dict[str, str] = {}
    last_change = time.time()
    attempts: list[str] = []
    announced = False

    while time.time() < deadline:
        try:
            entries = await crafty.list_dir(server_id, ".")
            names = {k for k in entries if k != "root_path"}
            server = await crafty.get_server(server_id)
            executable = server.get("executable") or ""
            command = server.get("execution_command") or ""

            if loader_installed(family, command, names, executable):
                job.log_line(f"Loader ready ({len(names)} entries in the "
                             "server directory)")
                return {"ok": True, "repairs": attempts,
                        "source": attempts[-1] if attempts else "crafty"}

            jar_here = any(n.endswith(".jar") for n in names)
            if jar_here and not announced:
                announced = True
                job.set_step("Crafty is installing the loader", 62)

            # Names plus mtimes: a jar still being written moves; an
            # abandoned one does not.
            sig = {n: str((entries.get(n) or {}).get("modified", ""))
                   for n in names}
            if sig != last_sig:
                last_sig, last_change = sig, time.time()

            stalled = time.time() - last_change > STALL_SECONDS
            if (not jar_here and time.time() > grace_until) or stalled:
                nxt = _next_rung(attempts)
                if nxt is None:
                    raise RuntimeError(_exhausted_message(family, mc_version,
                                                          attempts))
                attempts.append(nxt)
                await _repair(job, server_id, family, mc_version,
                              crafty_loader, executable, via=nxt)
                grace_until = time.time() + DOWNLOAD_GRACE
                last_change = time.time()
                last_sig = {}
                continue
        except crafty.CraftyError:
            pass
        await asyncio.sleep(5)

    raise RuntimeError(
        f"Crafty never finished installing {family} for Minecraft "
        f"{mc_version} (waited {config.SERVER_READY_TIMEOUT}s). The instance "
        "exists but has no launcher — you can retry the loader from the "
        "instance's Overview, or delete it and start again."
    )


def _next_rung(attempts: list[str]) -> str | None:
    for rung in ("crafty-index", "upstream"):
        if rung not in attempts:
            return rung
    return None


def _exhausted_message(family: str, mc: str, attempts: list[str]) -> str:
    return (
        f"The {family} loader for Minecraft {mc} could not be installed. "
        f"Crafty's own download failed, its jar index "
        f"({'tried' if 'crafty-index' in attempts else 'not tried'}) did not "
        f"help, and the upstream project did not serve a build either. "
        "Crafty's mirror (jars.arcadiatech.org) is the usual culprit and is "
        "usually back within the hour. The instance has been left in place so "
        "you can retry the loader rather than reinstalling the pack."
    )


async def _repair(job: Job, server_id: str, family: str, mc_version: str,
                  crafty_loader: str, executable: str, *, via: str) -> None:
    """Put the loader jar there ourselves, then finish the install."""
    if via == "crafty-index":
        job.log_line(
            "Crafty's loader download did not complete. Fetching the same jar "
            "from Crafty's own index instead.", "warn")
        job.set_step("Fetching the loader jar Crafty could not", 61)
        src = await _from_crafty_index(crafty_loader, mc_version)
    else:
        job.log_line(
            f"Crafty's index did not help either. Falling back to the "
            f"{family} project's own downloads.", "warn")
        job.set_step(f"Fetching {family} from upstream", 61)
        src = await loaders.resolve(family, mc_version)

    if not src:
        raise RuntimeError(
            f"No {family} build for Minecraft {mc_version} was found "
            f"{'in Crafty' + chr(39) + 's jar index' if via == 'crafty-index' else 'upstream'}."
        )

    payload = await loaders.fetch(src["url"])
    if src.get("sha256"):
        digest = hashlib.sha256(payload).hexdigest()
        if digest != src["sha256"]:
            raise RuntimeError(
                "The loader jar downloaded from Crafty's mirror is corrupt "
                f"(sha256 {digest[:12]}… expected {src['sha256'][:12]}…)."
            )
    if src.get("sha1"):
        digest = hashlib.sha1(payload).hexdigest()
        if digest != src["sha1"]:
            raise RuntimeError(
                f"The {family} jar downloaded from {src.get('source')} is "
                "corrupt — its checksum does not match what the project "
                "publishes."
            )

    # Keep Crafty's own filename when we have it: Crafty's record already
    # points `executable` at that name, and a jar under a different name
    # means a launch command that names a file which does not exist.
    name = (executable or src.get("filename")
            or f"{crafty_loader}-{mc_version}.jar").split("/")[-1]
    await crafty.upload_file(server_id, ".", name, payload)
    job.log_line(
        f"Uploaded {name} ({len(payload) / 1048576:.1f} MB) from "
        f"{src.get('source') or 'Crafty’s index'}"
    )

    if not executable or executable.split("/")[-1] != name:
        try:
            await crafty.patch_server(server_id, {"executable": name})
        except Exception:
            pass

    if _is_installer_family(family):
        await run_installer(job, server_id, family)
    elif src.get("kind") == "server":
        # Fabric/Paper/vanilla run the jar directly. Crafty writes the command
        # at create time and it survives, but an upstream jar can have a
        # different filename, so make sure the command names the file on disk.
        await _ensure_direct_command(job, server_id, name)


async def _from_crafty_index(crafty_loader: str, mc_version: str) -> dict:
    try:
        catalog = await crafty.jar_catalog()
    except Exception:
        return {}
    src = crafty.jar_source(catalog, "mc_java_servers", crafty_loader,
                            mc_version)
    return {**src, "source": "Crafty’s jar index",
            "kind": "installer" if "installer" in crafty_loader else "server",
            "filename": ""} if src else {}


async def _ensure_direct_command(job: Job, server_id: str, jar: str) -> None:
    server = await crafty.get_server(server_id)
    command = server.get("execution_command") or ""
    if jar in command:
        return
    replaced = re.sub(r"-jar\s+\S+\.jar", f"-jar {jar}", command)
    if replaced == command:
        replaced = f"java -Xms1G -Xmx4G -jar {jar} nogui"
    await crafty.patch_server(server_id, {"executable": jar,
                                          "execution_command": replaced})
    job.log_line(f"Launch command now points at {jar}")


async def run_installer(job: Job, server_id: str, family: str) -> None:
    """Run a Forge/NeoForge installer the way Crafty would, then fix the command.

    Crafty's create call already left the instance pointing at
    `-jar <installer>.jar --installServer`, so starting the server *is*
    running the installer. What Crafty normally does afterwards -- rewrite
    `executable` and `execution_command` to the real launch line -- lives on
    the thread that died with the download, so it has to be done here.
    """
    job.set_step("Running the loader installer", 63)
    await crafty.server_action(server_id, "start_server")

    deadline = time.time() + min(900, config.SERVER_READY_TIMEOUT)
    ok = False
    while time.time() < deadline:
        await asyncio.sleep(5)
        try:
            entries = await crafty.list_dir(server_id, ".")
            names = {k for k in entries if k != "root_path"}
        except crafty.CraftyError:
            continue
        if "libraries" in names and ("run.sh" in names or "run.bat" in names):
            await asyncio.sleep(5)      # let it flush its last writes
            ok = True
            break
    if not ok:
        raise RuntimeError(
            "The loader installer did not finish. Its output is in this "
            "instance's console — open the Console tab to see what it "
            "reported."
        )
    await rewrite_modded_command(job, server_id, family)


# Mirrors Crafty's own post-install rewrite (app/classes/installers/modded.py):
# read the run script the installer generated and turn it into a launch
# command. Getting this wrong means the next start re-runs the installer
# instead of the server.
_RUN_SCRIPT = re.compile(
    r"java @([a-zA-Z0-9_.]+) @([a-z./\-]+)([0-9.\-]+(?:-[a-zA-Z0-9]+)?)"
    r"/([a-z_0-9]+\.txt)"
)


async def rewrite_modded_command(job: Job, server_id: str, family: str) -> None:
    script = ""
    for candidate in ("run.sh", "run.bat"):
        try:
            script = await crafty.read_file(server_id, candidate)
            if script:
                break
        except crafty.CraftyError:
            continue

    match = _RUN_SCRIPT.search(script or "")
    if not match:
        job.log_line(
            "The loader installed, but its run script could not be parsed — "
            "this instance may need its launch command set by hand in Crafty.",
            "warn")
        return

    args_file, lib_path, version, txt = match.groups()
    exec_path = f"{lib_path}{version}/"
    loader = "neoforge" if "neoforge" in loaders.family_of(family) else "forge"
    await crafty.patch_server(server_id, {
        "executable": f"{exec_path}{loader}-{version}-server.jar",
        "execution_command": f"java @{args_file} @{exec_path}{txt} nogui",
    })
    job.log_line(f"Launch command set for {loader} {version}")


# --- creating a server from nothing ------------------------------------


async def _create_record(job: Job, *, name: str, family: str, mc_version: str,
                         mem_min: int, mem_max: int, port: int
                         ) -> tuple[str, bool]:
    """Ask Crafty for an instance. Returns (server_id, used_substitute).

    Crafty can only create from its own jar index, so a version it has never
    heard of -- a fresh Minecraft release, or a loader build its mirror never
    cached -- cannot be created directly. Rather than refuse, we create the
    nearest thing Crafty *does* have and then replace the jar upstream, which
    is the same repair the loader ladder already performs.
    """
    crafty_loader = loaders.crafty_key(family)
    catalog: dict = {}
    try:
        catalog = await crafty.jar_catalog()
    except Exception:
        job.log_line("Crafty's jar index is unreachable; creating against the "
                     "requested version directly.", "warn")

    available = list(((catalog.get("mc_java_servers") or {}).get("types") or {})
                     .get(crafty_loader, {}).get("versions", {}).keys())
    target = mc_version
    substitute = False
    if available and mc_version not in available:
        # Prefer the newest build in the same minor line, so a 1.21.4 request
        # lands on 1.21.x rather than on 1.16.
        line = mc_version.rsplit(".", 1)[0]
        same_line = [v for v in available if v.startswith(line)]
        pool = same_line or available
        target = sorted(pool, key=loaders._version_key)[-1]
        substitute = True
        job.log_line(
            f"Crafty's index has no {family} build for {mc_version}. Creating "
            f"against {target} and replacing the jar with the real "
            f"{mc_version} build afterwards.", "warn")

    server_id = await crafty.create_server(
        name=name, loader_type=crafty_loader, mc_version=target,
        mem_min=mem_min, mem_max=mem_max, port=port,
    )
    return server_id, substitute


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
    """Create a bare instance: a loader, a version, and an empty mod folder."""
    family = loaders.family_of(loader)
    if family not in {e["key"] for e in loaders.CATALOGUE}:
        raise ValueError(
            f"'{loader}' is not a loader this can create. Choose one of: "
            + ", ".join(e["key"] for e in loaders.CATALOGUE)
        )
    if not re.match(r"^\d+\.\d+(\.\d+)?$", (mc_version or "").strip()):
        raise ValueError(f"'{mc_version}' is not a Minecraft version")

    host = specs.effective_host()
    ram_max = int(mem_max or min(6, max(2, int(host.get("total_ram_gb") or 8) // 2)))
    ram_min = int(mem_min or max(1, ram_max // 2))
    if ram_min > ram_max:
        ram_min = ram_max

    job.set_step(f"Creating {family} {mc_version}", 10)
    server_id, substitute = await _create_record(
        job, name=name, family=family, mc_version=mc_version,
        mem_min=ram_min, mem_max=ram_max, port=port,
    )
    job.set_instance(server_id, name)
    job.emit("server_created", server_id, server_id=server_id)
    job.log_line(f"Created instance {server_id}")

    # Stamp it immediately, marked unfinished: if the loader never arrives the
    # fleet list should say "half-finished" rather than showing a healthy
    # server with nothing in it.
    mod_dir = loaders.mod_directory(family)
    try:
        await crafty.write_studio_manifest(server_id, {
            "schema": 1, "complete": False, "started_at": time.time(),
            "pack": {"name": name, "source": "empty"},
            "minecraft": mc_version, "loader": family,
            "mod_directory": mod_dir,
        })
    except Exception:
        pass

    if substitute:
        # The record exists against the wrong version. Force the ladder to run
        # now so the right jar lands before anything else happens.
        job.set_step("Replacing the loader with the requested build", 30)
        try:
            server = await crafty.get_server(server_id)
            await _repair(job, server_id, family, mc_version,
                          loaders.crafty_key(family),
                          server.get("executable") or "", via="upstream")
        except Exception as e:
            job.log_line(
                f"Could not install {family} {mc_version} directly: {e}. The "
                f"instance is running the closest build Crafty had.", "warn")

    result = await ensure_loader(job, server_id, family=family,
                                 mc_version=mc_version)

    job.set_step("Preparing the server directory", 78)
    # The mod or plugin folder is the entire point of an empty instance, and
    # a loader that has never started has not created one.
    for folder in (mod_dir, "config", "logs", "crash-reports"):
        try:
            await crafty.ensure_dir(server_id, folder)
        except Exception:
            job.log_line(f"Could not create {folder}/", "warn")
    if family in loaders.PLUGIN_FAMILIES:
        # Paper reads plugin configs out of plugins/<Name>/, so plugins/ is
        # the only one that has to exist. `config/` is harmless and stops the
        # Configs tab from being empty on a fresh Paper server.
        pass

    job.set_step("Applying server settings", 86)
    try:
        await crafty.write_file(server_id, "eula.txt", crafty.EULA_ACCEPTED)
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
            if family in ("forge", "neoforge"):
                await crafty.write_file(server_id, "user_jvm_args.txt",
                                        specs.render_jvm_args(flags, ram_max))
            else:
                await optimizer.set_command_memory(server_id, ram_max, flags)
            job.log_line(f"Tuned JVM: {ram_max:g} GB heap, {len(flags)} flags")
        except Exception as e:
            job.log_line(f"Could not apply performance tuning: {e}", "warn")

    try:
        java = await crafty.set_java_version(server_id, mc_version)
        if java.get("changed"):
            job.log_line(f"Set Java {java.get('java_major')} for this instance")
    except Exception as e:
        job.log_line(f"Could not set the Java version: {e}", "warn")

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

    job.set_step("Recording the instance manifest", 98)
    try:
        await crafty.write_studio_manifest(server_id, {
            "schema": 1,
            "complete": True,
            "installed_at": time.time(),
            "pack": {"name": name, "source": "empty",
                     "install_source": "blank",
                     "version": f"{family} {mc_version}"},
            "minecraft": mc_version,
            "loader": family,
            "loader_version": "",
            "crafty_loader": loaders.crafty_key(family),
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
        job.log_line(f"Could not write the instance manifest: {e}", "warn")

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
    """Retry the loader for an instance that was left without one.

    The one recovery path an interrupted create used to have was "delete it
    and start again", which throws away the port, the name and anything
    already uploaded.
    """
    manifest = await crafty.read_studio_manifest(server_id)
    server = await crafty.get_server(server_id)
    family = loaders.family_of(manifest.get("loader") or "")
    mc = manifest.get("minecraft") or ""
    if not mc:
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", server.get("executable") or "")
        mc = m.group(1) if m else ""
    if not family or not mc:
        raise ValueError(
            "This instance does not record which loader or Minecraft version "
            "it was built for, so the loader cannot be reinstalled "
            "automatically."
        )
    job.log_line(f"Reinstalling {family} for Minecraft {mc}")
    # Skip Crafty's own attempt: it already had its chance.
    await _repair(job, server_id, family, mc, loaders.crafty_key(family),
                  server.get("executable") or "", via="crafty-index")
    result = await ensure_loader(job, server_id, family=family, mc_version=mc)
    try:
        manifest["complete"] = True
        await crafty.write_studio_manifest(server_id, manifest)
    except Exception:
        pass
    return {"server_id": server_id, "loader": family, "minecraft": mc, **result}
