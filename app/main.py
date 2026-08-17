"""HTTP API + static frontend for Crafty Modpack Studio."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import (
    ai,
    config,
    configs,
    crafty,
    curseforge,
    deps,
    diagnostics,
    installer,
    modrinth,
    mods as modmgr,
    optimizer,
    properties,
    specs,
)
from app.jobs import registry

app = FastAPI(title="BlessForge", version="2.0.0")

STATIC_DIR = Path(__file__).parent / "static"


def _err(e: Exception) -> HTTPException:
    if isinstance(e, crafty.CraftyError):
        return HTTPException(status_code=e.status or 502, detail=str(e))
    if isinstance(e, curseforge.CurseForgeError):
        return HTTPException(status_code=502, detail=str(e))
    if isinstance(e, modrinth.ModrinthError):
        return HTTPException(status_code=502, detail=str(e))
    if isinstance(e, (ValueError, KeyError)):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


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
            servers = await asyncio.wait_for(crafty.list_servers(), timeout=5.0)
            checks["crafty"] = {"ok": True, "servers": len(servers)}
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

    ready = bool(checks["crafty"] and checks["crafty"].get("ok"))
    return {"ready": ready, "config": state, "checks": checks}


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
            info["cpu"] = stats.get("cpu")
            info["mem"] = stats.get("mem")
        except Exception:
            info["running"] = None
        return info

    return {"items": await asyncio.gather(*(enrich(s) for s in servers))}


@app.get("/api/instances/{server_id}")
async def instance_detail(server_id: str) -> dict:
    try:
        server = await crafty.get_server(server_id)
        manifest = await crafty.read_studio_manifest(server_id)
        try:
            stats = await crafty.get_stats(server_id)
        except Exception:
            stats = {}
        return {"server": server, "manifest": manifest, "stats": stats}
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/action/{action}")
async def instance_action(server_id: str, action: str) -> dict:
    allowed = {"start_server", "stop_server", "restart_server", "kill_server"}
    if action not in allowed:
        raise HTTPException(400, f"action must be one of {sorted(allowed)}")

    prepared: dict = {}
    if action in ("start_server", "restart_server"):
        # Crafty regenerates the launch command whenever its loader installer
        # finishes, which can quietly drop a Java version we pinned earlier.
        # Re-assert it here so a start is always correct, and make sure the
        # EULA is in the byte-exact form Crafty's start check demands.
        prepared = await _prepare_for_start(server_id)
    try:
        await crafty.server_action(server_id, action)
        return {"ok": True, "action": action, "prepared": prepared}
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
    results, errors = [], []
    for f in files:
        try:
            results.append(await modmgr.set_enabled(
                server_id, f, enabled, body.get("directory", "mods")))
        except Exception as e:
            errors.append({"file": f, "error": str(e)})
    return {"changed": results, "errors": errors}


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
        return await configs.write_config(server_id, body["path"], body["content"])
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
        return result
    except Exception as e:
        raise _err(e)


@app.post("/api/instances/{server_id}/deep-scan")
async def deep_scan(server_id: str) -> dict:
    job = registry.create("deep_scan", f"Dependency scan for {server_id[:8]}")
    registry.start(job, lambda j: diagnostics.deep_scan(j, server_id))
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


# --- installs ----------------------------------------------------------


@app.post("/api/install/preflight")
async def install_preflight(body: dict = Body(...)) -> dict:
    """Analyse a pack and list the mods that look client-only, for review."""
    try:
        mod_id = int(body["mod_id"])
        file_id = int(body["file_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "mod_id and file_id are required")
    job = registry.create("preflight", "Analysing pack")
    registry.start(
        job,
        lambda j: installer.preflight(
            j, mod_id=mod_id, file_id=file_id,
            prefer_server_pack=bool(body.get("prefer_server_pack", True)),
        ),
    )
    return {"job_id": job.id}


# --- optimizer ---------------------------------------------------------


@app.get("/api/host/specs")
async def host_specs() -> dict:
    return specs.effective_host()


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
    job = registry.create("ai", "AI analysis")

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

    applied, failed = [], []
    for action in plan["actions"]:
        name, args = action["action"], action["args"]
        try:
            if name == "accept_eula":
                await crafty.write_file(server_id, "eula.txt", crafty.EULA_ACCEPTED)
            elif name == "set_java":
                await crafty.set_java_version(server_id, args["minecraft"])
            elif name == "set_ram":
                await optimizer.apply(server_id, {"heap_gb": args["max_gb"],
                                                  "flags": []})
            elif name == "disable_mods":
                for f in args["files"]:
                    await modmgr.set_enabled(server_id, f, False)
            elif name == "delete_mods":
                await modmgr.delete_mods(server_id, args["files"])
            elif name == "edit_property":
                await optimizer.apply(
                    server_id, {"properties": {args["key"]: args["value"]}})
            else:
                # install_mod / switch_mod_version / inspect_config need a
                # choice of project and version, so they are handed back to
                # the UI rather than guessed at here.
                failed.append({"action": name,
                               "error": "needs to be completed in the UI"})
                continue
            applied.append({"action": name, "args": args})
        except Exception as e:
            failed.append({"action": name, "error": str(e)})

    return {"applied": applied, "failed": failed,
            "rejected": plan.get("rejected", [])}


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
    try:
        mod_id = int(body["mod_id"])
        file_id = int(body["file_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "mod_id and file_id are required")

    name = body.get("server_name") or f"Modpack {mod_id}"
    job = registry.create("install", f"Install {name}")
    registry.start(
        job,
        lambda j: installer.install_modpack(
            j,
            mod_id=mod_id,
            file_id=file_id,
            server_name=name,
            port=int(body.get("port", 25565)),
            mem_min=body.get("mem_min"),
            mem_max=body.get("mem_max"),
            prefer_server_pack=bool(body.get("prefer_server_pack", True)),
            skip_client_only=bool(body.get("skip_client_only", True)),
            motd=body.get("motd"),
            existing_server_id=body.get("server_id"),
            exclude_files=body.get("exclude_files"),
            optimize=bool(body.get("optimize", True)),
        ),
    )
    return {"job_id": job.id}


@app.post("/api/instances/{server_id}/switch-pack-version")
async def switch_pack_version(server_id: str, body: dict = Body(...)) -> dict:
    try:
        mod_id = int(body["mod_id"])
        file_id = int(body["file_id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "mod_id and file_id are required")

    job = registry.create("switch", f"Switch pack version for {server_id[:8]}")
    registry.start(
        job,
        lambda j: installer.switch_pack_version(
            j,
            server_id=server_id,
            mod_id=mod_id,
            file_id=file_id,
            prefer_server_pack=bool(body.get("prefer_server_pack", True)),
            skip_client_only=bool(body.get("skip_client_only", True)),
            keep_world=bool(body.get("keep_world", True)),
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


def _asset_version() -> str:
    """Fingerprint of the built frontend, used to bust browser caches.

    Without this, a rebuilt container still serves the browser's cached
    app.js: the fix is deployed but the user sees the old behaviour and
    concludes it did not work. Derived from file mtimes so it changes on
    every build without needing a manual version bump.
    """
    stamp = 0.0
    for name in ("app.js", "style.css", "index.html"):
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
        r'(/static/(?:app\.js|style\.css))(\?v=[^"\']*)?',
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
