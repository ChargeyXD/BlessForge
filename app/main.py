"""HTTP API + static frontend for Crafty Modpack Studio."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import (
    ai,
    backups,
    cache,
    config,
    configs,
    crafty,
    curseforge,
    deps,
    diagnostics,
    exporter,
    installer,
    modrinth,
    mods as modmgr,
    optimizer,
    properties,
    roulette,
    smoketest,
    specs,
    uploads,
    watcher,
    whitelist,
)
from app.jobs import registry

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Own the background update sweep for the life of the process."""
    task = asyncio.create_task(watcher.run_forever())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="BlessForge", version="2.0.0", lifespan=_lifespan)

STATIC_DIR = Path(__file__).parent / "static"


def _err(e: Exception) -> HTTPException:
    if isinstance(e, uploads.UploadError):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, crafty.CraftyError):
        return HTTPException(status_code=e.status or 502, detail=str(e))
    if isinstance(e, curseforge.CurseForgeError):
        return HTTPException(status_code=502, detail=str(e))
    if isinstance(e, modrinth.ModrinthError):
        return HTTPException(status_code=502, detail=str(e))
    if isinstance(e, (ValueError, KeyError)):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


# Activity lists every job the browser has seen, and "Dependency scan for
# 12908773" tells nobody which of a dozen servers is being scanned. Resolving
# the name costs one Crafty call, so it is cached briefly rather than made on
# every job start.
_NAME_CACHE: dict[str, tuple[float, str]] = {}
_NAME_TTL = 60.0


async def _instance_name(server_id: str | None) -> str | None:
    if not server_id:
        return None
    cached = _NAME_CACHE.get(server_id)
    if cached and time.time() - cached[0] < _NAME_TTL:
        return cached[1]
    try:
        server = await asyncio.wait_for(crafty.get_server(server_id), timeout=5.0)
        name = server.get("server_name") or server_id[:8]
    except Exception:
        # A job that cannot name its instance is still a job worth starting.
        return None
    _NAME_CACHE[server_id] = (time.time(), name)
    return name


async def _job_for(kind: str, title: str, server_id: str):
    """Create a job already labelled with the instance it acts on."""
    name = await _instance_name(server_id)
    return registry.create(
        kind, f"{title} — {name}" if name else title,
        server_id=server_id, server_name=name,
    )


# --- health & config ---------------------------------------------------


@app.get("/healthz")
@app.get("/api/healthz")
async def healthz() -> dict:
    """Instant liveness probe for Docker and CasaOS container healthchecks."""
    return {"status": "ok", "app": "BlessForge"}


@app.get("/api/health")
async def health() -> dict:
    state = config.configured()
    checks: dict[str, Any] = {"crafty": None, "curseforge": None, "modrinth": None}

    if state["crafty"]:
        try:
            started = time.perf_counter()
            servers = await asyncio.wait_for(crafty.list_servers(), timeout=5.0)
            # Round-trip to Crafty. Worth showing: it is the difference
            # between "the controller is on this box" and "the controller is
            # across a link", which changes what every other number means.
            checks["crafty"] = {
                "ok": True,
                "servers": len(servers),
                "latency_ms": round((time.perf_counter() - started) * 1000),
            }
        except Exception as e:
            checks["crafty"] = {"ok": False, "error": str(e)}
    else:
        checks["crafty"] = {"ok": False, "error": "CRAFTY_URL / CRAFTY_TOKEN not set"}

    if state["curseforge"]:
        try:
            await asyncio.wait_for(curseforge.search(query="", page_size=1), timeout=5.0)
            checks["curseforge"] = {"ok": True}
        except Exception as e:
            error = str(e)
            # A mangled key looks exactly like a wrong key, so say which it is.
            warning = state.get("curseforge_key_warning")
            if warning and "403" in error:
                error = warning
            checks["curseforge"] = {"ok": False, "error": error}
    else:
        checks["curseforge"] = {"ok": False, "error": "CURSEFORGE_API_KEY not set"}

    if state["modrinth"]:
        try:
            await asyncio.wait_for(modrinth.search(query="", page_size=1), timeout=5.0)
            checks["modrinth"] = {"ok": True}
        except Exception as e:
            checks["modrinth"] = {"ok": False, "error": str(e)}
    else:
        checks["modrinth"] = {"ok": False, "error": "disabled"}

    # A /data that the app cannot write is invisible until the first install
    # dies half-way through: the cache silently fails, imports have nowhere to
    # land, and nothing in the UI ever says why. It happens whenever the mount
    # is pointed at a root-owned folder -- most often the wrong folder picked
    # in the CasaOS install dialog -- so it gets checked and reported.
    checks["storage"] = _storage_check()

    ready = bool(checks["crafty"] and checks["crafty"].get("ok"))
    return {"ready": ready, "config": state, "checks": checks}


def _storage_check() -> dict:
    probe = config.CACHE_DIR / ".write-probe"
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except Exception as e:
        looks_like_crafty = any(
            (config.DATA_DIR / name).exists()
            for name in ("crafty.sqlite", "servers", "data/servers")
        )
        hint = (
            f"{config.DATA_DIR} looks like Crafty's own data directory. "
            "BlessForge needs a folder of its own -- point the mount at "
            "/DATA/AppData/blessforge/data."
            if looks_like_crafty
            else f"{config.DATA_DIR} is not writable by the app (uid 1000)."
        )
        return {"ok": False, "error": f"{hint} ({e.__class__.__name__})",
                "path": str(config.DATA_DIR)}
    free = None
    try:
        free = round(shutil.disk_usage(str(config.DATA_DIR)).free / 1024 ** 3, 1)
    except OSError:
        pass
    return {"ok": True, "path": str(config.DATA_DIR), "free_gb": free}


# --- browsing ----------------------------------------------------------


@app.get("/api/browse/modpacks")
async def browse_modpacks(
    q: str = "",
    game_version: str | None = None,
    loader: str | None = None,
    category_id: int | None = None,
    sort: int = 2,
    index: int = 0,
    page_size: int = 30,
) -> dict:
    try:
        return await curseforge.search(
            query=q, class_id=curseforge.CLASS_MODPACKS, game_version=game_version,
            mod_loader=loader, category_id=category_id, sort_field=sort,
            index=index, page_size=page_size,
        )
    except Exception as e:
        raise _err(e)


@app.get("/api/browse/mods")
async def browse_mods(
    q: str = "",
    source: str = "curseforge",
    game_version: str | None = None,
    loader: str | None = None,
    category_id: int | None = None,
    sort: int = 2,
    index: int = 0,
    page_size: int = 30,
) -> dict:
    try:
        if source == "modrinth":
            return await modrinth.search(
                query=q, project_type="mod", game_version=game_version,
                loader=loader, index=index, page_size=page_size,
            )
        return await curseforge.search(
            query=q, class_id=curseforge.CLASS_MODS, game_version=game_version,
            mod_loader=loader, category_id=category_id, sort_field=sort,
            index=index, page_size=page_size,
        )
    except Exception as e:
        raise _err(e)


@app.get("/api/browse/modpacks/modrinth")
async def browse_modrinth_packs(
    q: str = "", game_version: str | None = None, loader: str | None = None,
    index: int = 0, page_size: int = 30,
) -> dict:
    try:
        return await modrinth.search(
            query=q, project_type="modpack", game_version=game_version,
            loader=loader, index=index, page_size=page_size,
        )
    except Exception as e:
        raise _err(e)


@app.get("/api/modpacks/{mod_id}/files")
async def modpack_files(
    mod_id: int, game_version: str | None = None, index: int = 0, page_size: int = 50
) -> dict:
    try:
        files = await curseforge.list_files(
            mod_id, game_version=game_version, index=index, page_size=page_size
        )
        return {"items": files}
    except Exception as e:
        raise _err(e)


@app.get("/api/modpacks/{mod_id}")
async def modpack_detail(mod_id: int) -> dict:
    try:
        return await curseforge.get_mod(mod_id)
    except Exception as e:
        raise _err(e)


@app.get("/api/modpacks/{mod_id}/files/{file_id}/plan")
async def modpack_plan(mod_id: int, file_id: int) -> dict:
    try:
        return await installer.resolve_pack_plan(mod_id, file_id)
    except Exception as e:
        raise _err(e)


@app.get("/api/mods/{source}/{project_id}/versions")
async def mod_versions(
    source: str, project_id: str,
    game_version: str | None = None, loader: str | None = None,
) -> dict:
    try:
        items = await modmgr.list_mod_versions(
            source, project_id, game_version=game_version, loader=loader
        )
        return {"items": items}
    except Exception as e:
        raise _err(e)


@app.get("/api/meta/categories")
async def categories(kind: str = "modpacks") -> dict:
    try:
        class_id = (curseforge.CLASS_MODPACKS if kind == "modpacks"
                    else curseforge.CLASS_MODS)
        return {"items": await curseforge.get_categories(class_id)}
    except Exception as e:
        raise _err(e)


@app.get("/api/meta/minecraft-versions")
async def minecraft_versions() -> dict:
    try:
        return {"items": await curseforge.get_minecraft_versions()}
    except Exception as e:
        raise _err(e)


# --- instances ---------------------------------------------------------


@app.get("/api/instances")
async def instances() -> dict:
    try:
        servers = await crafty.list_servers()
    except Exception as e:
        raise _err(e)

    async def enrich(s: dict) -> dict:
        sid = s.get("server_id")
        info = {
            "server_id": sid,
            "name": s.get("server_name"),
            "port": s.get("server_port"),
            "path": s.get("path"),
            "type": s.get("type"),
            "executable": s.get("executable"),
            "auto_start": s.get("auto_start"),
            "created": s.get("created"),
        }
        try:
            manifest = await crafty.read_studio_manifest(sid)
            info["pack"] = manifest.get("pack")
            info["minecraft"] = manifest.get("minecraft")
            info["loader"] = manifest.get("loader")
            info["managed"] = bool(manifest.get("pack"))
            # `complete` is written only when an install reaches the end, so
            # its absence on a manifest BlessForge wrote means the install
            # stopped half-way and left the instance behind. Defaults to True
            # so instances created before this existed are not all accused.
            info["incomplete"] = bool(manifest) and not manifest.get("complete", True)
            info["problems"] = len(manifest.get("problems") or [])
            # Off the manifest, not off a directory listing: the fleet is
            # re-read every twenty seconds and a list_dir per server per poll
            # is a Crafty request per server for a number that only changes
            # when someone installs something.
            info["mod_count"] = len(manifest.get("mods") or []) or None
        except Exception:
            info["managed"] = False
        # Infer loader/version from the executable path when unmanaged.
        if not info.get("loader"):
            exe = (s.get("executable") or "").lower()
            for fam, token in (("neoforge", "neoforge"), ("forge", "forge"),
                               ("fabric", "fabric")):
                if token in exe:
                    info["loader"] = fam
                    break
        try:
            stats = await crafty.get_stats(sid)
            info["running"] = bool(stats.get("running"))
            info["players"] = stats.get("online")
            info["max_players"] = stats.get("max")
            info["cpu"] = stats.get("cpu")
            # Crafty reports `mem` as a human string ("1.2GB") and the share
            # of host RAM separately. The gauges want the number.
            info["mem"] = stats.get("mem_percent")
            # `mem` is the resident set in bytes, and mem_percent its share of
            # host RAM. Crafty names them the other way round to how they read.
            info["mem_bytes"] = stats.get("mem")
            info["world_size"] = stats.get("world_size")
            # Crafty tracks this itself; no need to go looking for crash
            # reports on every card of a fleet list to find out.
            info["crashed"] = bool(stats.get("crashed"))
            info["reachable"] = True
        except Exception:
            # Crafty answers 500 for a server whose directory has gone
            # missing, and keeps doing so until it is restarted. That is a
            # state worth naming rather than an error to swallow.
            info["running"] = None
            info["reachable"] = False
        info["state"] = _instance_state(info)
        return info

    return {"items": await asyncio.gather(*(enrich(s) for s in servers))}


def _instance_state(info: dict) -> str:
    """One word for what is going on with a server.

    Computed here rather than in the browser so that every surface -- the
    fleet list, the palette, an activity chip -- agrees about what a server
    is doing, and so the rules live next to the facts they are drawn from.

    `orphan` is the one that matters: Crafty keeps a record for a server
    whose files have been removed and then fails every request about it
    forever after its next restart. It looks like a broken app unless it is
    named.
    """
    if not info.get("reachable"):
        return "orphan"
    if info.get("running"):
        return "running"
    if info.get("incomplete"):
        return "incomplete"
    if info.get("crashed"):
        return "crashed"
    return "stopped"


@app.get("/api/instances/{server_id}")
async def instance_detail(server_id: str) -> dict:
    try:
        server = await crafty.get_server(server_id)
        manifest = await crafty.read_studio_manifest(server_id)
        try:
            stats = await crafty.get_stats(server_id)
        except Exception:
            stats = {}
        info = {
            "reachable": True,          # Crafty answered, or we would not be here
            "running": bool(stats.get("running")),
            "crashed": bool(stats.get("crashed")),
            "incomplete": bool(manifest) and not manifest.get("complete", True),
        }
        return {
            "server": server,
            "manifest": manifest,
            "stats": stats,
            # Same field, same rule as the fleet list: one word for what is
            # going on, decided in one place.
            "state": _instance_state(info),
            "java": _java_facts(server, manifest),
            "uptime_s": crafty.uptime_seconds(stats),
        }
    except Exception as e:
        raise _err(e)


def _java_facts(server: dict, manifest: dict) -> dict:
    """Which Java runs this server, and whether it is the right one.

    Computed here rather than in the browser for the same reason `state` is:
    Crafty stores no java_version, the answer has to be parsed back out of
    execution_command, and one implementation of that parse is enough.
    """
    facts = crafty.java_in_command(server.get("execution_command") or "")
    mc = manifest.get("minecraft")
    if not mc:
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", server.get("executable") or "")
        mc = m.group(1) if m else ""
    facts["minecraft"] = mc or None
    facts["required"] = crafty.required_java_major(mc) if mc else None
    # None, not False, when the command names no version: "we cannot tell"
    # and "it is wrong" are different answers and must not share a colour.
    facts["ok"] = (
        facts["major"] == facts["required"]
        if facts["major"] and facts["required"] else None
    )
    return facts


@app.get("/api/instances/{server_id}/stats")
async def instance_stats(server_id: str) -> dict:
    """Just the live numbers, for surfaces that tick while you watch them.

    The Situation screen refreshes this every few seconds; going through
    /api/instances/{id} for it would re-read the manifest off Crafty's disk
    each time to redraw a CPU bar.
    """
    try:
        stats = await crafty.get_stats(server_id)
        return {**stats, "uptime_s": crafty.uptime_seconds(stats)}
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/action/{action}")
async def instance_action(server_id: str, action: str) -> dict:
    allowed = {"start_server", "stop_server", "restart_server", "kill_server"}
    if action not in allowed:
        raise HTTPException(400, f"action must be one of {sorted(allowed)}")

    # Crafty silently ignores a start for a server it already believes is
    # running, and its `running` flag both lags a stop by a poll cycle and
    # stays set for a process that died without it noticing. Either way a
    # plain start is dropped on the floor while the UI says "Starting X".
    #
    # A restart is right in both cases -- it stops whatever is there, if
    # anything, and starts it -- so that is what a start becomes.
    upgraded = False
    if action == "start_server":
        try:
            if (await crafty.get_stats(server_id)).get("running"):
                action, upgraded = "restart_server", True
        except Exception:
            pass          # if stats are unreadable, let the start try anyway

    prepared: dict = {}
    if action in ("start_server", "restart_server"):
        # Crafty regenerates the launch command whenever its loader installer
        # finishes, which can quietly drop a Java version we pinned earlier.
        # Re-assert it here so a start is always correct, and make sure the
        # EULA is in the byte-exact form Crafty's start check demands.
        prepared = await _prepare_for_start(server_id)
    try:
        await crafty.server_action(server_id, action)
        return {"ok": True, "action": action, "prepared": prepared,
                "upgraded": upgraded,
                "why": ("Crafty still had this marked as running, so it was "
                        "restarted rather than started -- a plain start would "
                        "have been ignored.") if upgraded else None}
    except Exception as e:
        raise _err(e)


async def _prepare_for_start(server_id: str) -> dict:
    out: dict = {}
    try:
        manifest = await crafty.read_studio_manifest(server_id)
        server = await crafty.get_server(server_id)
        mc = manifest.get("minecraft")
        if not mc:
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", server.get("executable") or "")
            mc = m.group(1) if m else ""
        if mc:
            result = await crafty.set_java_version(server_id, mc)
            if result.get("changed"):
                out["java"] = result.get("java_path")
    except Exception as e:
        out["java_error"] = str(e)

    try:
        eula = await crafty.read_file(server_id, "eula.txt")
        if eula.split("\n", 1)[0].lower() not in (
            "eula=true", "eula = true", "eula= true", "eula =true"
        ) and re.search(r"eula\s*=\s*true", eula, re.I):
            await crafty.write_file(server_id, "eula.txt", crafty.EULA_ACCEPTED)
            out["eula"] = "normalised"
    except Exception:
        pass
    return out


@app.delete("/api/instances/{server_id}")
async def instance_delete(server_id: str, files: bool = True) -> dict:
    try:
        await crafty.delete_server(server_id, delete_files=files)
        return {"ok": True}
    except Exception as e:
        raise _err(e)


# --- mods --------------------------------------------------------------


@app.get("/api/instances/{server_id}/mods")
async def instance_mods(server_id: str, directory: str = "mods") -> dict:
    try:
        return await modmgr.list_mods(server_id, directory)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/mods/toggle")
async def toggle_mod(server_id: str, body: dict = Body(...)) -> dict:
    try:
        return await modmgr.set_enabled(
            server_id, body["file"], bool(body.get("enabled", True)),
            body.get("directory", "mods"),
        )
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/mods/bulk-toggle")
async def bulk_toggle(server_id: str, body: dict = Body(...)) -> dict:
    files = body.get("files") or []
    enabled = bool(body.get("enabled", False))
    # Disabling a dozen jars at once is the change people most want back, and
    # a snapshot is a list of names -- so take one before, not after. A single
    # toggle is its own undo (toggle it again) and is not worth a snapshot.
    snap = None
    if len(files) > 1:
        snap = await backups.snapshot(
            server_id,
            f"before {'enabling' if enabled else 'disabling'} {len(files)} mods")
    results, errors = [], []
    for f in files:
        try:
            results.append(await modmgr.set_enabled(
                server_id, f, enabled, body.get("directory", "mods")))
        except Exception as e:
            errors.append({"file": f, "error": str(e)})
    return {"changed": results, "errors": errors,
            "snapshot": (snap or {}).get("id")}


@app.post("/api/instances/{server_id}/mods/delete")
async def delete_mods(server_id: str, body: dict = Body(...)) -> dict:
    try:
        return await modmgr.delete_mods(
            server_id, body.get("files") or [], body.get("directory", "mods")
        )
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/mods/add")
async def add_mod(server_id: str, body: dict = Body(...)) -> dict:
    """Install a mod. By default its required dependencies come too."""
    if body.get("with_dependencies", True):
        job = registry.create("add_mod", f"Install {body.get('name') or 'mod'}")
        registry.start(
            job,
            lambda j: modmgr.add_mod_with_dependencies(
                j, server_id,
                source=body.get("source", "curseforge"),
                project_id=body["project_id"],
                file_id=body["file_id"],
                directory=body.get("directory", "mods"),
                replace_file=body.get("replace_file"),
                include_dependencies=True,
                skip_projects=body.get("skip_dependencies") or [],
            ),
        )
        return {"job_id": job.id}
    try:
        return await modmgr.add_mod(
            server_id,
            source=body.get("source", "curseforge"),
            project_id=body["project_id"],
            file_id=body["file_id"],
            directory=body.get("directory", "mods"),
            replace_file=body.get("replace_file"),
        )
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/mods/resolve")
async def resolve_dependencies(server_id: str, body: dict = Body(...)) -> dict:
    """Preview what installing a mod would pull in, before committing."""
    try:
        return await deps.resolve_for_instance(
            server_id,
            source=body.get("source", "curseforge"),
            project_id=body["project_id"],
            file_id=body.get("file_id"),
        )
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/mods/icons")
async def refresh_icons(server_id: str) -> dict:
    try:
        return await modmgr.enrich_icons(server_id)
    except Exception as e:
        raise _err(e)


@app.get("/api/instances/{server_id}/mods/updates")
async def mod_updates(server_id: str) -> dict:
    try:
        return await modmgr.check_updates(server_id)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/mods/identify")
async def identify_mods(server_id: str, directory: str = "mods") -> dict:
    job = registry.create("identify", f"Identify mods in {server_id[:8]}")
    registry.start(job, lambda j: modmgr.identify_mods(j, server_id, directory))
    return {"job_id": job.id}


# --- configs -----------------------------------------------------------


@app.get("/api/instances/{server_id}/configs")
async def list_configs(server_id: str, root: str | None = None) -> dict:
    try:
        return await configs.list_configs(server_id, root)
    except Exception as e:
        raise _err(e)


@app.get("/api/instances/{server_id}/configs/read")
async def read_config(server_id: str, path: str = Query(...)) -> dict:
    try:
        return await configs.read_config(server_id, path)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/configs/write")
async def write_config(server_id: str, body: dict = Body(...)) -> dict:
    try:
        path = body["path"]
        # An edited config is the one thing here that exists nowhere else --
        # no catalogue will hand it back. Keep the previous bytes first.
        snap = None
        try:
            before = await configs.read_config(server_id, path)
            snap = await backups.snapshot(
                server_id, f"before editing {path}",
                files={path: (before.get("content") or "").encode()})
        except Exception:
            pass
        result = await configs.write_config(server_id, path, body["content"])
        return {**result, "snapshot": (snap or {}).get("id")}
    except Exception as e:
        raise _err(e)


# --- diagnostics -------------------------------------------------------


@app.get("/api/instances/{server_id}/diagnose")
async def diagnose(server_id: str) -> dict:
    try:
        quick, logs = await asyncio.gather(
            diagnostics.quick_check(server_id),
            diagnostics.analyse_logs(server_id),
            return_exceptions=True,
        )
        result: dict[str, Any] = {"findings": []}
        if isinstance(quick, dict):
            result.update(quick)
        if isinstance(logs, dict):
            result["findings"] = (result.get("findings") or []) + logs["findings"]
            result["log_tail"] = logs.get("log_tail")
            result["crash_tail"] = logs.get("crash_tail")
            result["log_path"] = logs.get("log_path")
            result["crash_path"] = logs.get("crash_path")
            result["has_logs"] = logs.get("has_logs")
        result["findings"].sort(
            key=lambda f: diagnostics.SEVERITY_ORDER.get(f.get("severity"), 9)
        )

        # "The server crashed" is not a useful answer on its own. Whenever a
        # crash report exists, the log is read end to end and the jars it
        # actually implicates are named, with the line that implicates each.
        if isinstance(logs, dict) and logs.get("crash_path"):
            try:
                review = await diagnostics.crash_review(server_id)
                result["crash"] = review
                if review.get("culprits"):
                    top = review["culprits"][:4]
                    result["findings"].insert(0, {
                        "severity": "critical",
                        "category": "mods",
                        "title": f"Crash traced to {len(review['culprits'])} mod(s)",
                        "detail": "; ".join(
                            f"{c['file']} — {c['why']}" for c in top
                        ),
                        "fix": {"action": "disable_mods",
                                "files": [c["file"] for c in top
                                          if c["confidence"] != "low"]},
                        "evidence": "\n".join(c["evidence"] for c in top)[:1500],
                    })
            except Exception:
                pass
        return result
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/deep-scan")
async def deep_scan(server_id: str) -> dict:
    job = await _job_for("deep_scan", "Dependency scan", server_id)
    registry.start(job, lambda j: diagnostics.deep_scan(j, server_id))
    return {"job_id": job.id}


# --- undo, boot test, export, scheduled updates ------------------------


@app.get("/api/instances/{server_id}/backups")
async def list_backups(server_id: str) -> dict:
    """Snapshots taken before this server's destructive changes."""
    return {"items": backups.list_snapshots(server_id),
            "limit": config.MAX_BACKUPS}


@app.post("/api/instances/{server_id}/backups")
async def take_backup(server_id: str, reason: str = Body("manual", embed=True)) -> dict:
    try:
        return await backups.snapshot(server_id, reason)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/backups/{snap_id}/restore")
async def restore_backup(server_id: str, snap_id: str) -> dict:
    try:
        return await backups.restore(server_id, snap_id)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/smoke-test")
async def smoke_test(server_id: str, timeout: int = Body(300, embed=True)) -> dict:
    """Boot once, watch, stop, and report -- for a server with no log yet."""
    job = await _job_for("smoke_test", "Boot test", server_id)
    registry.start(job, lambda j: smoketest.run(j, server_id,
                                                timeout=max(60, min(timeout, 900))))
    return {"job_id": job.id}


@app.post("/api/instances/{server_id}/export")
async def export_instance(
    server_id: str,
    include_disabled: bool = Body(False, embed=True),
    bundle_others: bool = Body(True, embed=True),
) -> dict:
    """Write this server out as a CurseForge modpack archive."""
    job = await _job_for("export", "Export pack", server_id)

    async def run(j):
        result = await exporter.export_instance(
            j, server_id, include_disabled=include_disabled,
            bundle_others=bundle_others)
        blob = result.pop("bytes")
        path = config.DATA_DIR / "exports" / result["filename"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        result["download"] = f"/api/exports/{result['filename']}"
        return result

    registry.start(job, run)
    return {"job_id": job.id}


@app.get("/api/exports/{filename}")
async def download_export(filename: str):
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "", filename)
    path = config.DATA_DIR / "exports" / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such export")
    return FileResponse(path, media_type="application/zip", filename=safe)


@app.get("/api/updates")
async def update_summary() -> dict:
    """The last scheduled sweep for newer mod builds."""
    state = watcher.load()
    return {
        **state,
        "every_hours": config.UPDATE_CHECK_HOURS,
        "total": sum(s.get("updates", 0) for s in (state.get("servers") or {}).values()),
    }


@app.post("/api/updates/check")
async def update_check_now() -> dict:
    """Run the sweep now instead of waiting for the schedule."""
    job = registry.create("updates", "Checking every server for mod updates")
    registry.start(job, lambda j: watcher.check_all())
    return {"job_id": job.id}


@app.get("/api/diagnose/dependency/{mod_id}")
async def dependency_sources(
    mod_id: str, game_version: str | None = None, loader: str | None = None
) -> dict:
    try:
        return await diagnostics.suggest_dependency_sources(
            mod_id, game_version=game_version, loader=loader
        )
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/fix/accept-eula")
async def fix_eula(server_id: str) -> dict:
    try:
        await crafty.write_file(server_id, "eula.txt", crafty.EULA_ACCEPTED)
        return {"ok": True}
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/fix/set-ram")
async def fix_ram(server_id: str, body: dict = Body(...)) -> dict:
    gb = int(body.get("max_gb", 8))
    min_gb = int(body.get("min_gb", max(1, gb // 2)))
    args = (
        "# Managed by Crafty Modpack Studio\n"
        f"-Xms{min_gb}G\n-Xmx{gb}G\n"
    )
    try:
        await crafty.write_file(server_id, "user_jvm_args.txt", args)
        return {"ok": True, "min_gb": min_gb, "max_gb": gb}
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/fix/java")
async def fix_java(server_id: str, body: dict = Body(default={})) -> dict:
    """Pin the instance to a Java version its loader supports."""
    mc = body.get("minecraft")
    if not mc:
        manifest = await crafty.read_studio_manifest(server_id)
        mc = manifest.get("minecraft")
    if not mc:
        # Fall back to the version embedded in the loader path.
        server = await crafty.get_server(server_id)
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", server.get("executable") or "")
        mc = m.group(1) if m else ""
    if not mc:
        raise HTTPException(400, "could not determine the Minecraft version")
    try:
        return await crafty.set_java_version(server_id, mc)
    except Exception as e:
        raise _err(e)


@app.get("/api/instances/{server_id}/logs")
async def instance_logs(server_id: str, lines: int = 300) -> dict:
    try:
        result = await diagnostics.analyse_logs(server_id, tail_lines=lines)
        return {
            "log_path": result["log_path"],
            "tail": result["log_tail"],
            "crash_path": result["crash_path"],
            "crash_tail": result["crash_tail"],
        }
    except Exception as e:
        raise _err(e)


# --- terminal ----------------------------------------------------------
#
# Crafty keeps a live console buffer per server and also writes
# logs/latest.log. Neither on its own is enough: the buffer is empty for a
# server Crafty did not start itself (or that was running before Crafty
# restarted), and the file lags behind by a flush. So "auto" reads the buffer
# and falls back to the file, which is what someone opening a terminal
# actually wants to see.

# Crafty answers a missing log file with a line of prose rather than an
# empty list, which would otherwise be rendered as the server's own output.
_NO_LOG_FILE = "Unable to find file to tail"


async def _console_snapshot(server_id: str, source: str = "auto",
                            with_stats: bool = True) -> dict:
    lines: list[str] = []
    used = source
    if source in ("auto", "buffer"):
        try:
            lines = await crafty.console_lines(server_id)
            used = "buffer"
        except crafty.CraftyError:
            lines = []
    if not lines and source in ("auto", "file"):
        try:
            file_lines = await crafty.console_lines(server_id, from_file=True)
        except crafty.CraftyError:
            file_lines = []
        if len(file_lines) == 1 and _NO_LOG_FILE in file_lines[0]:
            file_lines = []
        if file_lines or source == "file":
            lines, used = file_lines, "file"

    running = None
    if with_stats:
        try:
            stats = await crafty.get_stats(server_id)
            running = bool(stats.get("running"))
        except Exception:
            running = False
    return {"lines": lines, "source": used, "running": running,
            "count": len(lines)}


@app.get("/api/instances/{server_id}/console")
async def console(server_id: str, source: str = "auto") -> dict:
    """A snapshot of the server console."""
    if source not in ("auto", "buffer", "file"):
        raise HTTPException(400, "source must be auto, buffer or file")
    try:
        return await _console_snapshot(server_id, source)
    except Exception as e:
        raise _err(e)


@app.get("/api/instances/{server_id}/console/stream")
async def console_stream(server_id: str, source: str = "auto") -> StreamingResponse:
    """Follow the console live.

    Crafty exposes no push channel, so this polls it and forwards only what
    is new. The diff is done here rather than in the browser so a reconnect
    does not repaint the whole buffer, and so the wire carries one line
    rather than the last five hundred every second.
    """
    async def stream():
        seen: list[str] = []
        idle = 0
        tick = 0
        running = False
        try:
            while True:
                # Whether the server is up changes on the scale of minutes,
                # not of the 1.5s poll the console needs, so it is asked for
                # every fifth pass instead of doubling the calls to Crafty.
                tick += 1
                want_stats = tick % 5 == 1
                try:
                    snap = await _console_snapshot(server_id, source,
                                                   with_stats=want_stats)
                    if snap["running"] is not None:
                        running = snap["running"]
                    snap["running"] = running
                except Exception as e:
                    yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"
                    await asyncio.sleep(5)
                    continue

                lines = snap["lines"]
                # The buffer is a ring: when it wraps, the tail no longer
                # starts where we left off, so fall back to sending the lot
                # rather than silently dropping output.
                if lines[: len(seen)] == seen:
                    fresh = lines[len(seen):]
                else:
                    fresh = lines
                    yield f"data: {json.dumps({'event': 'reset'})}\n\n"
                seen = lines

                if fresh:
                    idle = 0
                    yield "data: " + json.dumps({
                        "event": "lines", "lines": fresh,
                        "running": snap["running"], "source": snap["source"],
                    }) + "\n\n"
                else:
                    idle += 1
                    if idle % 10 == 0:
                        yield "data: " + json.dumps({
                            "event": "idle", "running": snap["running"],
                            "source": snap["source"],
                        }) + "\n\n"
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/instances/{server_id}/command")
async def send_command(server_id: str, body: dict = Body(...)) -> dict:
    """Send one line to the server console."""
    try:
        return await crafty.send_command(server_id, body.get("command", ""))
    except Exception as e:
        raise _err(e)


# --- imported archives -------------------------------------------------


@app.post("/api/uploads/modpack")
async def upload_modpack(file: UploadFile = File(...)) -> dict:
    """Accept a modpack archive exported from the CurseForge app.

    Streamed straight to disk and analysed offline, so the response can
    describe the pack -- loader, Minecraft version, mod count -- before the
    user commits to an install. No CurseForge calls happen here.
    """
    async def chunks():
        while True:
            chunk = await file.read(4 * 1024 * 1024)
            if not chunk:
                return
            yield chunk

    try:
        record = await uploads.store(file.filename or "modpack.zip", chunks())
    except Exception as e:
        raise _err(e)
    finally:
        await file.close()
    return record


@app.get("/api/uploads")
async def list_uploads() -> dict:
    """Previously imported archives, so a re-install needs no second upload."""
    return {"items": uploads.list_uploads(), "limit": config.MAX_UPLOADS}


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(upload_id: str) -> dict:
    try:
        return {"deleted": uploads.delete(upload_id)}
    except Exception as e:
        raise _err(e)


# --- installs ----------------------------------------------------------


def _pack_ref(body: dict) -> dict:
    """Read a pack reference: CurseForge ids, or an imported archive id.

    Every install entry point takes both, and neither is meaningful without
    the other half, so the shape is validated in one place.
    """
    upload_id = (body.get("upload_id") or "").strip()
    if upload_id:
        try:
            # Checked here rather than inside the job: an archive that was
            # pruned or never existed is a bad request, and answering it with
            # a job that fails ten seconds later just hides that.
            uploads.get(upload_id)
        except Exception as e:
            raise _err(e)
        return {"upload_id": upload_id}
    try:
        return {"mod_id": int(body["mod_id"]), "file_id": int(body["file_id"])}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            400, "either upload_id, or both mod_id and file_id, are required"
        )


@app.post("/api/install/preflight")
async def install_preflight(body: dict = Body(...)) -> dict:
    """Analyse a pack and list the mods that look client-only, for review."""
    ref = _pack_ref(body)
    job = registry.create("preflight", "Analysing pack")
    registry.start(
        job,
        lambda j: installer.preflight(
            j, **ref,
            prefer_server_pack=bool(body.get("prefer_server_pack", True)),
        ),
    )
    return {"job_id": job.id}


# --- mod roulette ------------------------------------------------------


@app.get("/api/roulette/meta")
async def roulette_meta() -> dict:
    """The controls the roulette screen is built from, and the host it rolls for."""
    return {
        "categories": [
            {k: c[k] for k in ("key", "title", "glyph", "color")}
            for c in roulette.CATEGORIES
        ],
        "intensity": roulette.INTENSITY_LABELS,
        "defaults": roulette.DEFAULT_CONSTRAINTS,
        "loaders": ["Forge", "NeoForge", "Fabric", "Quilt"],
        "sources": ["curseforge", "modrinth", "both"],
        "host": specs.effective_host(),
        "seed": roulette.mint_seed(),
    }


@app.post("/api/roulette/pool")
async def roulette_pool(body: dict = Body(default={})) -> dict:
    """Build (or reuse) the candidate pool for a set of constraints.

    Returned as a job because the first build on a fresh Minecraft version is
    dozens of catalogue requests; every later call is a disk read and
    finishes immediately.
    """
    c = roulette.merge_constraints(body)
    job = registry.create("roulette_pool", f"Pool for {c['loader']} {c['minecraft']}")

    async def run(j) -> dict:
        pool = await roulette.build_pool(
            c["minecraft"], c["loader"], c["source"], job=j,
            refresh=bool(body.get("refresh")),
        )
        allowed = roulette.eligible(pool["mods"], c)
        j.log_line(f"{len(allowed)} of {len(pool['mods'])} mods pass these constraints")
        return _pool_view(pool, allowed, c)

    registry.start(job, run)
    return {"job_id": job.id}


def _pool_view(pool: dict, allowed: list[dict], c: dict) -> dict:
    by_cat: dict[str, int] = {}
    for m in allowed:
        by_cat[m["category"]] = by_cat.get(m["category"], 0) + 1
    return {
        "minecraft": pool["minecraft"],
        "loader": pool["loader"],
        "source": pool["source"],
        "total": len(pool["mods"]),
        "eligible": len(allowed),
        "by_category": by_cat,
        "built_at": pool["built_at"],
        "note": (
            "Too narrow to deal a full hand. Un-ban a category or drop the "
            "quality floor."
            if len(allowed) < max(8, c["count"] // 4)
            else f"{len(allowed)} of {len(pool['mods'])} pass your constraints "
                 f"on {pool['minecraft']} / {pool['loader']}."
        ),
    }


@app.post("/api/roulette/roll")
async def roulette_roll(body: dict = Body(default={})) -> dict:
    """Pull the lever. Deterministic: same seed + constraints == same hand.

    Returned as a job rather than a plain response because dealing a hand of
    120 mods means pinning 120 real builds, one catalogue request each. That
    is fifteen seconds of honest work, and a spinner with nothing behind it
    for fifteen seconds reads as a hang.
    """
    c = roulette.merge_constraints(body.get("constraints") or body)
    seed = roulette.normalise_seed(body.get("seed"))
    holds = [str(h) for h in (body.get("holds") or [])]
    job = registry.create("roulette_roll", f"Rolling {seed}")

    async def run(j) -> dict:
        j.set_step("Reading the pool", 4)
        pool = await roulette.build_pool(c["minecraft"], c["loader"], c["source"], job=j)
        allowed = roulette.eligible(pool["mods"], c)
        if len(allowed) < 5:
            raise RuntimeError(
                f"Only {len(allowed)} mods pass these constraints — not enough to "
                "deal a hand. Un-ban a category or drop the quality floor."
            )
        # Deal generously, then pin real builds and drop whatever has none, so
        # the hand shown is one that can actually be installed. Over-dealing
        # first means a few unavailable mods do not silently shrink the pack.
        wide = dict(c, count=min(len(allowed), int(c["count"] * 1.35) + 4))
        dealt = roulette.deal(seed, pool["mods"], wide, holds)
        j.set_step(f"Pinning a build for {len(dealt)} mods", 20)
        hand, dropped = await roulette.resolve_hand(dealt, c, job=j)

        # Side information only arrives with the file, so a mod the pool
        # believed was server-safe can turn out not to be. Honour the toggle
        # now that the truth is known, and use the over-deal to replace what
        # leaves rather than handing back a short pack.
        if not c["toggles"].get("client"):
            kept = []
            for m in hand:
                if m.get("flag") == "CLIENT" and m["name"] not in holds:
                    dropped.append({
                        "name": m["name"],
                        "reason": (m.get("flag_why") or {}).get("client", "client-only"),
                    })
                else:
                    kept.append(m)
            hand = kept
        hand = hand[: c["count"]]
        j.set_step("Reading the odds", 94)
        return {
            "seed": seed,
            "constraints": c,
            "hand": hand,
            "dropped": dropped,
            "summary": roulette.summarise(hand, c, specs.effective_host()),
            "pool": _pool_view(pool, allowed, c),
        }

    registry.start(job, run)
    return {"job_id": job.id}


@app.post("/api/roulette/reroll")
async def roulette_reroll(body: dict = Body(...)) -> dict:
    """Replace one slot in a hand, leaving the rest of the roll alone."""
    c = roulette.merge_constraints(body.get("constraints") or {})
    hand = body.get("hand") or []
    target = body.get("mod")
    if not hand or not target:
        raise HTTPException(400, "hand and mod are both required")
    try:
        pool = await roulette.build_pool(c["minecraft"], c["loader"], c["source"])
        seed = roulette.normalise_seed(body.get("seed"))
        fresh = roulette.reroll_one(seed, hand, target, pool["mods"], c)
        return {
            "seed": seed,
            "hand": fresh,
            "summary": roulette.summarise(fresh, c, specs.effective_host()),
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _err(e)


@app.post("/api/roulette/install")
async def roulette_install(body: dict = Body(...)) -> dict:
    """Accept a hand: build the pack, install it, keep the export."""
    hand = body.get("hand") or []
    if not hand:
        raise HTTPException(400, "no hand supplied — pull the lever first")
    c = roulette.merge_constraints(body.get("constraints") or {})
    seed = roulette.normalise_seed(body.get("seed"))
    name = (body.get("server_name") or f"Roulette {seed}").strip()
    job = registry.create("roulette", f"Rolling {name}", server_name=name)

    registry.start(job, lambda j: roulette.install_roll(
        j, seed=seed, constraints=c, hand=hand, server_name=name,
        port=int(body.get("port", 25565)), motd=body.get("motd"),
        optimize=bool(body.get("optimize", True)),
    ))
    return {"job_id": job.id}


@app.get("/api/roulette/export/{roll_id}")
async def roulette_export(roll_id: str) -> FileResponse:
    """Download the CurseForge pack a roll produced."""
    path = roulette.export_path(roll_id)
    if not path.exists():
        raise HTTPException(404, "that export is no longer on disk")
    return FileResponse(
        path, media_type="application/zip", filename=f"{roll_id}.zip"
    )


@app.post("/api/roulette/preview-export")
async def roulette_preview_export(body: dict = Body(...)) -> dict:
    """What the export would contain, without installing anything."""
    hand = body.get("hand") or []
    if not hand:
        raise HTTPException(400, "no hand supplied")
    c = roulette.merge_constraints(body.get("constraints") or {})
    c["seed"] = roulette.normalise_seed(body.get("seed"))
    cf = [m for m in hand if m.get("source") == "curseforge"]
    return {
        "files": len(cf),
        "bundled": len(hand) - len(cf),
        "manifest_only": len(hand) == len(cf),
        "note": (
            "Every rolled mod is on CurseForge, so the export is a small "
            "manifest the CurseForge app can open directly."
            if len(hand) == len(cf) else
            f"{len(hand) - len(cf)} rolled mods are Modrinth-only, so their "
            "jars travel inside the zip under overrides/mods/."
        ),
    }


# --- optimizer ---------------------------------------------------------


@app.get("/api/host/specs")
async def host_specs() -> dict:
    return specs.effective_host()


@app.get("/api/cache")
async def cache_status() -> dict:
    """What the download cache is holding, and the ceiling it is kept under."""
    used = cache.usage_bytes()
    limit = config.MAX_CACHE_GB * 1024**3
    return {
        "used_bytes": used,
        "used_gb": round(used / 1024**3, 2),
        "limit_gb": config.MAX_CACHE_GB,
        "over": bool(limit and used > limit),
    }


@app.post("/api/cache/prune")
async def cache_prune() -> dict:
    """Trim the cache now rather than waiting for the next install."""
    try:
        return cache.prune()
    except Exception as e:
        raise _err(e)


@app.get("/api/instances/{server_id}/properties")
async def get_properties(server_id: str) -> dict:
    """Every server.properties key, typed and described."""
    try:
        return await properties.load(server_id)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/properties")
async def set_properties(server_id: str, body: dict = Body(...)) -> dict:
    try:
        return await properties.save(server_id, body.get("updates") or {})
    except Exception as e:
        raise _err(e)


@app.get("/api/instances/{server_id}/port")
async def get_port(server_id: str) -> dict:
    try:
        return await properties.port_status(server_id)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/port")
async def set_port(server_id: str, body: dict = Body(...)) -> dict:
    """Change the port in Crafty's record and server.properties together."""
    try:
        return await properties.set_port(
            server_id,
            body.get("port"),
            update_query=bool(body.get("update_query", True)),
            force=bool(body.get("force", False)),
        )
    except Exception as e:
        raise _err(e)


@app.get("/api/instances/{server_id}/optimize")
async def optimize_plan(server_id: str) -> dict:
    try:
        return await optimizer.build_plan(server_id)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/optimize")
async def optimize_apply(server_id: str, body: dict = Body(...)) -> dict:
    try:
        return await optimizer.apply(server_id, body)
    except Exception as e:
        raise _err(e)


# --- AI assistant ------------------------------------------------------


@app.get("/api/ai/status")
async def ai_status() -> dict:
    return await ai.status()


@app.post("/api/ai/warm")
async def ai_warm() -> dict:
    """Preload the model so the first real question is not stuck behind it."""
    return await ai.warm()


@app.post("/api/instances/{server_id}/ai/analyse")
async def ai_analyse(server_id: str, body: dict = Body(default={})) -> dict:
    """Ask the local model to explain a failure and propose a plan.

    It only ever proposes: every action comes back for the user to approve,
    and destructive ones are flagged so the UI can ask twice.
    """
    job = await _job_for("ai", "AI analysis", server_id)

    async def run(j) -> dict:
        j.set_step("Gathering evidence", 8)
        quick = await diagnostics.quick_check(server_id)
        j.set_step("Reading logs", 16)
        logs = await diagnostics.analyse_logs(server_id)
        j.set_step("Listing mods", 24)
        listing = await modmgr.list_mods(server_id)
        instance = {
            "minecraft": quick.get("minecraft") or listing.get("minecraft"),
            "loader": quick.get("loader") or listing.get("loader"),
            "pack": quick.get("pack") or listing.get("pack"),
            "mod_count": listing.get("count"),
        }
        findings = (quick.get("findings") or []) + logs["findings"]
        j.log_line(
            f"Evidence: {len(findings)} findings, {listing.get('count', 0)} mods, "
            f"{'crash report + ' if logs.get('crash_tail') else ''}"
            f"{'log' if logs.get('log_tail') else 'no log'}"
        )

        # On a CPU-only host the model spends a while reading the prompt
        # before producing anything. Naming that phase matters: otherwise the
        # first stretch looks identical to a hang.
        j.set_step("Model is reading the evidence", 35)
        started = time.time()
        first_token = {"seen": False}

        def on_token(piece: str) -> None:
            if not first_token["seen"]:
                first_token["seen"] = True
                j.log_line(
                    f"Model started writing after {time.time() - started:.0f}s"
                )
                j.set_step("Writing analysis", 55)
            j.stream_chunk(piece)
            elapsed = time.time() - started
            # Creep the bar toward 90% so it always looks alive.
            j.percent = min(90.0, 55 + elapsed * 0.8)

        result = await ai.analyse(
            instance=instance,
            findings=findings,
            log_tail=logs.get("log_tail", ""),
            crash_tail=logs.get("crash_tail", ""),
            mods=listing.get("mods", []),
            question=body.get("question", ""),
            on_token=on_token,
        )
        j.stream_chunk("", flush=True)
        j.log_line(f"Model finished in {time.time() - started:.0f}s")
        j.set_step("Done", 100)
        return result

    registry.start(job, run)
    return {"job_id": job.id}


@app.post("/api/instances/{server_id}/ai/apply")
async def ai_apply(server_id: str, body: dict = Body(...)) -> dict:
    """Execute AI-proposed actions the user has explicitly approved.

    The action list is re-validated here rather than trusted from the client,
    so a stale or tampered proposal cannot do anything the normal endpoints
    would not allow.
    """
    actions = body.get("actions") or []
    if not isinstance(actions, list) or not actions:
        raise HTTPException(400, "no actions supplied")
    if not body.get("confirmed"):
        raise HTTPException(400, "actions must be explicitly confirmed")

    listing = await modmgr.list_mods(server_id)
    known = {m["file"] for m in listing.get("mods", [])}
    plan = ai.validate_plan({"actions": actions}, known)
    applied, failed = await _apply_actions(server_id, plan["actions"])
    return {"applied": applied, "failed": failed,
            "rejected": plan.get("rejected", [])}


@app.get("/api/instances/{server_id}/crash-review")
async def crash_review(server_id: str) -> dict:
    """Which mods does the crash log blame? Deterministic, no model needed."""
    try:
        return await diagnostics.crash_review(server_id)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/ai/crash-review")
async def ai_crash_review(server_id: str) -> dict:
    """Read the whole crash log and name the mods that caused the crash.

    The regex pass runs first and is handed to the model as evidence, so the
    two halves of the answer can never contradict each other -- and if the
    model is unreachable, the deterministic culprits are still returned.
    """
    job = await _job_for("ai_crash", "Crash review", server_id)

    async def run(j) -> dict:
        j.set_step("Reading the crash report", 8)
        context = await diagnostics.read_crash_context(server_id)
        j.set_step("Listing mods", 18)
        listing = await modmgr.list_mods(server_id)
        mods = listing.get("mods", [])

        combined = "\n".join(
            t for t in (context["crash_text"], context["log_text"]) if t
        )
        found = diagnostics.attribute_crash(combined, [m["file"] for m in mods]) \
            if combined else []
        quick = await diagnostics.quick_check(server_id)
        findings = (quick.get("findings") or []) + (
            diagnostics.scan_text(combined) if combined else []
        )

        if not context["has_logs"]:
            j.set_step("No logs to read", 100)
            return {
                "available": True, "ok": True, "kind": "crash_review",
                "no_logs": True, "culprits": [], "actions": [],
                "summary": "This instance has produced no log and no crash "
                           "report at all. That is itself the finding: the "
                           "server was rejected before the JVM started, which "
                           "means the EULA gate, the Java version, or memory.",
                "confidence": "medium", "findings": findings,
            }

        j.log_line(
            f"Crash report {context['crash_path'] or '(none)'}, "
            f"log {context['log_path'] or '(none)'}, "
            f"{len(combined):,} characters, {len(mods)} jars installed"
        )
        if found:
            j.log_line(
                "The log itself points at: "
                + ", ".join(f"{c['file']} ({c['confidence']})" for c in found[:5])
            )

        j.set_step("Model is reading the crash report", 35)
        started = time.time()
        first = {"seen": False}

        def on_token(piece: str) -> None:
            if not first["seen"]:
                first["seen"] = True
                j.set_step("Writing the review", 55)
                j.log_line(f"Model started writing after "
                           f"{time.time() - started:.0f}s")
            j.stream_chunk(piece)
            j.percent = min(90.0, 55 + (time.time() - started) * 0.8)

        result = await ai.review_crash(
            instance={
                "minecraft": quick.get("minecraft") or listing.get("minecraft"),
                "loader": quick.get("loader") or listing.get("loader"),
                "pack": quick.get("pack") or listing.get("pack"),
            },
            mods=mods,
            crash_text=context["crash_text"],
            log_text=context["log_text"],
            findings=findings,
            crash_path=context["crash_path"],
            log_path=context["log_path"],
            attributed=found,
            on_token=on_token,
        )
        j.stream_chunk("", flush=True)

        # Merge the two views. The log's own attributions are facts, so they
        # lead; the model's are added where it saw something the patterns
        # did not, and marked as such.
        merged = {c["file"]: {**c, "from": "log"} for c in found}
        for c in result.get("culprits", []):
            if c["file"] in merged:
                merged[c["file"]]["model_why"] = c["why"]
            else:
                merged[c["file"]] = {**c, "from": "model", "score": 30,
                                     "reasons": [c["why"]]}
        result["culprits"] = sorted(
            merged.values(), key=lambda c: -c.get("score", 0)
        )
        result["findings"] = findings
        result["crash_path"] = context["crash_path"]
        result["log_path"] = context["log_path"]
        j.set_step("Done", 100)
        return result

    registry.start(job, run)
    return {"job_id": job.id}


@app.post("/api/instances/{server_id}/ai/autofix")
async def ai_autofix(server_id: str, body: dict = Body(default={})) -> dict:
    """Review the instance and apply the reversible fixes without asking again.

    "Fix it" is the request; this is the whole of the consent. It is still
    bounded: only actions that can be undone from the Mods tab are applied,
    and deleting a jar never is unless `allow_destructive` is set explicitly.
    Everything held back comes home in `held` for the user to approve.
    """
    allow_destructive = bool(body.get("allow_destructive"))
    job = await _job_for("ai_fix", "AI auto-fix", server_id)

    async def run(j) -> dict:
        j.set_step("Gathering evidence", 6)
        quick = await diagnostics.quick_check(server_id)
        logs = await diagnostics.analyse_logs(server_id)
        listing = await modmgr.list_mods(server_id)
        mods = listing.get("mods", [])
        context = await diagnostics.read_crash_context(server_id)
        combined = "\n".join(
            t for t in (context["crash_text"], context["log_text"]) if t
        )
        findings = (quick.get("findings") or []) + logs["findings"]

        j.set_step("Model is reviewing the instance", 25)
        started = time.time()

        def on_token(piece: str) -> None:
            j.stream_chunk(piece)
            j.percent = min(70.0, 25 + (time.time() - started) * 0.8)

        if context["crash_text"]:
            plan = await ai.review_crash(
                instance={"minecraft": quick.get("minecraft"),
                          "loader": quick.get("loader"),
                          "pack": quick.get("pack")},
                mods=mods, crash_text=context["crash_text"],
                log_text=context["log_text"], findings=findings,
                crash_path=context["crash_path"], log_path=context["log_path"],
                attributed=diagnostics.attribute_crash(
                    combined, [m["file"] for m in mods]) if combined else [],
                on_token=on_token,
            )
        else:
            plan = await ai.analyse(
                instance={"minecraft": quick.get("minecraft"),
                          "loader": quick.get("loader"),
                          "pack": quick.get("pack"),
                          "mod_count": listing.get("count")},
                findings=findings, log_tail=logs.get("log_tail", ""),
                crash_tail=logs.get("crash_tail", ""), mods=mods,
                question=body.get("question", ""), on_token=on_token,
            )
        j.stream_chunk("", flush=True)

        if not plan.get("ok"):
            return {**plan, "applied": [], "held": [], "failed": []}

        auto, held = ai.split_auto_actions(
            plan, allow_destructive=allow_destructive
        )
        if not auto:
            j.log_line("Nothing here can be fixed automatically.")
            j.set_step("Done", 100)
            return {**plan, "applied": [], "held": held, "failed": []}

        j.set_step(f"Applying {len(auto)} fix(es)", 78)
        applied, failed = await _apply_actions(server_id, auto, job=j)
        j.set_step("Done", 100)
        return {**plan, "applied": applied, "held": held, "failed": failed,
                "auto_applied": True}

    registry.start(job, run)
    return {"job_id": job.id}


async def _apply_actions(server_id: str, actions: list[dict], job=None
                         ) -> tuple[list[dict], list[dict]]:
    """Execute a validated action list. Shared by /ai/apply and /ai/autofix."""
    # The assistant's plan is the change a user is least able to predict and
    # least able to reverse from memory, so it always gets a snapshot -- even
    # a one-mod plan, unlike a hand-driven toggle.
    if actions:
        snap = await backups.snapshot(
            server_id,
            "before the assistant applied "
            + ", ".join(a["action"] for a in actions[:4])
            + (f" and {len(actions) - 4} more" if len(actions) > 4 else ""))
        if job and snap.get("saved"):
            job.log_line(f"Snapshot {snap['id']} taken -- this is reversible "
                         "from the server's Backups list")
    applied, failed = [], []
    for action in actions:
        name, args = action["action"], action["args"]
        try:
            if name == "accept_eula":
                await crafty.write_file(server_id, "eula.txt", crafty.EULA_ACCEPTED)
                detail = "eula.txt rewritten in the exact form Crafty accepts"
            elif name == "set_java":
                result = await crafty.set_java_version(server_id, args["minecraft"])
                detail = f"Java {result.get('java_major', '?')} selected"
            elif name == "set_ram":
                await optimizer.apply(server_id, {"heap_gb": args["max_gb"],
                                                  "flags": []})
                detail = f"heap set to {args['max_gb']:g} GB"
            elif name == "disable_mods":
                for f in args["files"]:
                    await modmgr.set_enabled(server_id, f, False)
                detail = "disabled " + ", ".join(args["files"])
            elif name == "delete_mods":
                await modmgr.delete_mods(server_id, args["files"])
                detail = "deleted " + ", ".join(args["files"])
            elif name == "edit_property":
                await optimizer.apply(
                    server_id, {"properties": {args["key"]: args["value"]}})
                detail = f"{args['key']} = {args['value']}"
            else:
                # install_mod / switch_mod_version / inspect_config need a
                # choice of project and version, so they are handed back to
                # the UI rather than guessed at here.
                failed.append({"action": name,
                               "error": "needs to be completed in the UI"})
                continue
            applied.append({"action": name, "args": args, "detail": detail})
            if job:
                job.log_line(f"Applied {name}: {detail}")
        except Exception as e:
            failed.append({"action": name, "error": str(e)})
            if job:
                job.log_line(f"{name} failed: {e}", "error")
    return applied, failed


@app.get("/api/ai/endpoints")
async def ai_endpoints() -> dict:
    """Which Ollama endpoints exist and which one is in use."""
    return {"items": ai.endpoints(), "active": ai.current_endpoint()}


@app.post("/api/ai/endpoint")
async def ai_set_endpoint(body: dict = Body(...)) -> dict:
    """Switch endpoints. The choice is remembered across restarts."""
    try:
        url = ai.set_endpoint(body.get("url") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"active": url, "items": ai.endpoints(), "status": await ai.status()}


# --- the client-only whitelist -----------------------------------------


@app.get("/api/whitelist")
async def whitelist_list() -> dict:
    """Mods the operator has said are safe on a server, whatever the check says."""
    return {"items": whitelist.items()}


@app.post("/api/whitelist")
async def whitelist_add(body: dict = Body(...)) -> dict:
    file_name = (body.get("file") or body.get("file_name") or "").strip()
    if not file_name:
        raise HTTPException(400, "file is required")
    return whitelist.add(file_name, name=body.get("name") or "",
                         reason=body.get("reason") or "")


@app.delete("/api/whitelist/{key}")
async def whitelist_remove(key: str) -> dict:
    return {"removed": whitelist.remove(key)}


@app.get("/api/ai/models")
async def ai_models() -> dict:
    """What the configured Ollama endpoint has, and what we are using."""
    return await ai.status()


@app.post("/api/ai/pull")
async def ai_pull(body: dict = Body(default={})) -> dict:
    """Ask the Ollama host to fetch a model. Explicit, never automatic."""
    model = (body.get("model") or ai.AI_MODEL).strip()
    job = registry.create("ai_pull", f"Pull {model}")

    async def run(j) -> dict:
        def progress(chunk: dict) -> None:
            total, done = chunk.get("total"), chunk.get("completed")
            if total and done:
                j.set_step(f"{chunk.get('status', 'downloading')} "
                           f"({done / total * 100:.0f}%)", done / total * 100)
            elif chunk.get("status"):
                j.set_step(chunk["status"])

        result = await ai.pull_model(model, on_progress=progress)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "pull failed"))
        j.log_line(f"{model} is now available on {ai.OLLAMA_URL}")
        return result

    registry.start(job, run)
    return {"job_id": job.id}


@app.get("/api/instances/{server_id}/mods/dependencies")
async def mod_dependencies(server_id: str) -> dict:
    """Which mods pulled in which -- the Mods tab's dependency view."""
    try:
        return await modmgr.dependency_map(server_id)
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/fix/versions")
async def fix_versions(server_id: str, body: dict = Body(...)) -> dict:
    """Find compatible replacements for mods on the wrong game version."""
    try:
        return await diagnostics.suggest_compatible_versions(
            server_id, body.get("files") or []
        )
    except Exception as e:
        raise _err(e)


@app.post("/api/install/modpack")
async def install_modpack(body: dict = Body(...)) -> dict:
    ref = _pack_ref(body)
    default_name = (
        "Imported modpack" if "upload_id" in ref else f"Modpack {ref.get('mod_id')}"
    )
    name = body.get("server_name") or default_name
    target = body.get("server_id")
    job = registry.create(
        "install", f"Install {name}",
        server_id=target, server_name=await _instance_name(target) or name,
    )
    registry.start(
        job,
        lambda j: installer.install_modpack(
            j,
            **ref,
            server_name=name,
            port=int(body.get("port", 25565)),
            mem_min=body.get("mem_min"),
            mem_max=body.get("mem_max"),
            prefer_server_pack=bool(body.get("prefer_server_pack", True)),
            skip_client_only=bool(body.get("skip_client_only", True)),
            motd=body.get("motd"),
            existing_server_id=body.get("server_id"),
            exclude_files=body.get("exclude_files"),
            disable_files=body.get("disable_files"),
            client_reasons=body.get("client_reasons"),
            optimize=bool(body.get("optimize", True)),
        ),
    )
    return {"job_id": job.id}


@app.post("/api/instances/{server_id}/switch-pack-version")
async def switch_pack_version(server_id: str, body: dict = Body(...)) -> dict:
    ref = _pack_ref(body)
    await backups.snapshot(server_id, "before switching pack version")
    job = await _job_for("switch", "Switch pack version", server_id)
    registry.start(
        job,
        lambda j: installer.switch_pack_version(
            j,
            server_id=server_id,
            **ref,
            prefer_server_pack=bool(body.get("prefer_server_pack", True)),
            skip_client_only=bool(body.get("skip_client_only", True)),
            keep_world=bool(body.get("keep_world", True)),
            exclude_files=body.get("exclude_files"),
            disable_files=body.get("disable_files"),
            client_reasons=body.get("client_reasons"),
        ),
    )
    return {"job_id": job.id}


# --- jobs --------------------------------------------------------------


@app.get("/api/jobs")
async def list_jobs() -> dict:
    return {"items": registry.list()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = registry.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.snapshot()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    job = registry.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {"cancelled": job.cancel()}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = registry.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    async def stream():
        q = job.subscribe()
        try:
            yield f"data: {json.dumps({'event': 'snapshot', **job.snapshot()})}\n\n"
            while True:
                if job.status in ("done", "error", "cancelled") and q.empty():
                    yield f"data: {json.dumps({'event': 'end', **job.snapshot()})}\n\n"
                    return
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- static ------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # The design canvas addresses its images as `assets/NAME`, and index.html
    # is that canvas with its markup untouched. Serving img/ at /assets is what
    # lets the template keep its own paths rather than being rewritten.
    if (STATIC_DIR / "img").is_dir():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "img")),
                  name="assets")


# Everything the browser caches and that changes when the app is rebuilt.
# support.js and the vendored React are content-addressed by their own
# immutability -- they are pinned versions and never change -- but they are
# fingerprinted anyway so one stale copy cannot outlive a redeploy.
_VERSIONED = ("index.html", "support.js",
              "vendor/react.production.min.js",
              "vendor/react-dom.production.min.js",
              "vendor/fonts.css")


def _asset_version() -> str:
    """Fingerprint of the built frontend, used to bust browser caches.

    Without this, a rebuilt container still serves the browser's cached
    scripts: the fix is deployed but the user sees the old behaviour and
    concludes it did not work. Derived from file mtimes so it changes on
    every build without needing a manual version bump.
    """
    stamp = 0.0
    for name in _VERSIONED:
        try:
            stamp = max(stamp, (STATIC_DIR / name).stat().st_mtime)
        except OSError:
            continue
    return hashlib.sha1(f"{app.version}:{stamp}".encode()).hexdigest()[:10]


@app.get("/")
async def index() -> Response:
    """Serve the SPA shell with cache-busted asset URLs."""
    try:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(500, "frontend is missing from this image")

    version = _asset_version()
    html = re.sub(
        r'(/static/(?:support\.js|vendor/[A-Za-z0-9._/-]+?\.(?:js|css)))'
        r'(\?v=[^"\']*)?',
        lambda m: f"{m.group(1)}?v={version}",
        html,
    )
    return Response(
        content=html,
        media_type="text/html",
        # The shell itself must never be cached, or it would keep pointing at
        # the previous version string.
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
