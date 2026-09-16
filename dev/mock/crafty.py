"""A stand-in for Crafty Controller, just real enough to drive the UI.

Implements the handful of v2 endpoints BlessForge actually calls, with the
same quirks that matter: files are listed by POST, sizes come back as human
strings, and there is a stray `root_path` key in every directory listing.
"""
from __future__ import annotations

import json
import posixpath
import time

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()

SERVERS = {
    "aaaa1111": {
        "server_id": "aaaa1111", "server_name": "Sakura SMP",
        "server_port": 25565, "path": "/servers/sakura", "type": "minecraft-java",
        "executable": "libraries/net/neoforged/neoforge/21.1.209/neoforge-21.1.209-server.jar",
        "execution_command": "/usr/lib/jvm/java-21-openjdk-amd64/bin/java "
                             "@user_jvm_args.txt @libraries/.../unix_args.txt nogui",
        "auto_start": True, "created": "2026-08-01",
    },
    "bbbb2222": {
        "server_id": "bbbb2222", "server_name": "Paper Survival",
        "server_port": 25566, "path": "/servers/paper", "type": "minecraft-java",
        "executable": "paper-1.21.4-232.jar",
        "execution_command": "java -Xms2G -Xmx4G -jar paper-1.21.4-232.jar nogui",
        "auto_start": False, "created": "2026-08-20",
    },
}

STATS = {
    "aaaa1111": {"running": True, "cpu": 41.5, "mem": 3_650_000_000,
                 "mem_percent": 46.2, "online": 3, "max": 20,
                 "players": "['Notch', 'Alex', 'Steve']", "world_name": "world",
                 "world_size": "1.4GB", "crashed": False,
                 "started": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(time.time() - 7400))},
    "bbbb2222": {"running": False, "cpu": 0, "mem": 0, "mem_percent": 0,
                 "online": 0, "max": 40, "players": "[]", "world_name": "world",
                 "world_size": "312MB", "crashed": False},
}

MANIFEST_A = {
    "schema": 1, "complete": True, "installed_at": time.time() - 86400 * 3,
    "pack": {"source": "curseforge", "name": "Cozy Sakura Adventure",
             "version": "1.4.2", "install_source": "manifest"},
    "minecraft": "1.21.1", "loader": "neoforge", "loader_version": "21.1.209",
    "crafty_loader": "neoforge-installer", "recommended_ram_mb": 6144,
    "mods": [
        {"file": "mods/jei-1.21.1-19.21.0.jar", "name": "Just Enough Items",
         "source": "curseforge", "project_id": 238222, "file_id": 5555,
         "version": "19.21.0"},
        {"file": "mods/sophisticatedcore-1.21.1-1.2.jar",
         "name": "Sophisticated Core", "source": "curseforge",
         "project_id": 618298, "file_id": 5556, "version": "1.2"},
        {"file": "mods/figura-0.1.4.jar", "name": "Figura", "source": "modrinth",
         "project_id": "Nk1jHUcH", "file_id": "abc", "version": "0.1.4",
         "client_only": True,
         "client_only_reasons": ["the jar declares environment=client"]},
    ],
    "problems": [],
}
MANIFEST_B = {
    "schema": 1, "complete": True, "installed_at": time.time() - 3600,
    "pack": {"name": "Paper Survival", "source": "empty",
             "install_source": "blank", "version": "paper 1.21.4"},
    "minecraft": "1.21.4", "loader": "paper", "mod_directory": "plugins",
    "mods": [], "problems": [],
}

# path -> (is_dir, content or size)
FILES: dict[str, dict[str, dict]] = {
    "aaaa1111": {
        ".": {
            "mods": {"dir": True}, "config": {"dir": True},
            "world": {"dir": True}, "logs": {"dir": True},
            "libraries": {"dir": True},
            "server.properties": {"dir": False, "size": "1.4KB"},
            "eula.txt": {"dir": False, "size": "10B"},
            "user_jvm_args.txt": {"dir": False, "size": "820B"},
            ".blessforge.json": {"dir": False, "size": "2.1KB"},
            "whitelist.json": {"dir": False, "size": "180B"},
            "ops.json": {"dir": False, "size": "210B"},
            "banned-players.json": {"dir": False, "size": "190B"},
            "banned-ips.json": {"dir": False, "size": "2B"},
            "usercache.json": {"dir": False, "size": "340B"},
        },
        "mods": {
            "jei-1.21.1-19.21.0.jar": {"dir": False, "size": "1.8MB"},
            "sophisticatedcore-1.21.1-1.2.jar": {"dir": False, "size": "2.4MB"},
            "figura-0.1.4.jar.disabled": {"dir": False, "size": "5.1MB"},
        },
        "config": {
            "jei": {"dir": True},
            "sophisticatedcore-common.toml": {"dir": False, "size": "3.2KB"},
        },
        "config/jei": {"jei-client.ini": {"dir": False, "size": "1.1KB"}},
        "world": {"region": {"dir": True}, "level.dat": {"dir": False, "size": "12KB"}},
        "world/region": {"r.0.0.mca": {"dir": False, "size": "8.4MB"}},
        "logs": {"latest.log": {"dir": False, "size": "94KB"}},
        "libraries": {},
    },
    "bbbb2222": {
        ".": {
            "plugins": {"dir": True}, "logs": {"dir": True},
            "server.properties": {"dir": False, "size": "1.4KB"},
            "eula.txt": {"dir": False, "size": "10B"},
            ".blessforge.json": {"dir": False, "size": "600B"},
        },
        "plugins": {}, "logs": {},
    },
}

TEXT: dict[str, dict[str, str]] = {
    "aaaa1111": {
        "server.properties": (
            "#Minecraft server properties\nmotd=A Sakura server\n"
            "server-port=25565\nquery.port=25565\ndifficulty=normal\n"
            "gamemode=survival\nmax-players=20\nonline-mode=true\npvp=true\n"
            "white-list=true\nenforce-whitelist=false\nview-distance=10\n"
            "simulation-distance=8\nspawn-protection=16\n"
        ),
        "eula.txt": "eula=true",
        "user_jvm_args.txt": "# Managed\n-Xms3G\n-Xmx6G\n-XX:+UseG1GC\n",
        ".blessforge.json": json.dumps(MANIFEST_A, indent=2),
        "whitelist.json": json.dumps([
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5", "name": "Notch"},
            {"uuid": "ec561538-f3fd-461d-aff5-086b22154bce", "name": "Alex"},
        ], indent=2),
        "ops.json": json.dumps([
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5", "name": "Notch",
             "level": 4, "bypassesPlayerLimit": False},
        ], indent=2),
        "banned-players.json": json.dumps([
            {"uuid": "11111111-2222-3333-4444-555555555555", "name": "Griefer",
             "created": "2026-08-14 12:00:00 +0000", "source": "Server",
             "expires": "forever", "reason": "broke the spawn"},
        ], indent=2),
        "banned-ips.json": "[]",
        "usercache.json": json.dumps([
            {"name": "Notch", "uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5",
             "expiresOn": "2026-10-01 00:00:00 +0000"},
            {"name": "Alex", "uuid": "ec561538-f3fd-461d-aff5-086b22154bce",
             "expiresOn": "2026-10-01 00:00:00 +0000"},
            {"name": "Steve", "uuid": "8667ba71-b85a-4004-af54-457a9734eed7",
             "expiresOn": "2026-10-01 00:00:00 +0000"},
            {"name": "Griefer", "uuid": "11111111-2222-3333-4444-555555555555",
             "expiresOn": "2026-09-01 00:00:00 +0000"},
        ], indent=2),
        "config/sophisticatedcore-common.toml":
            "[general]\n\t# how many upgrades\n\tmaxUpgrades = 5\n",
    },
    "bbbb2222": {
        "server.properties": (
            "motd=Paper Survival\nserver-port=25566\ndifficulty=easy\n"
            "gamemode=survival\nmax-players=40\nonline-mode=true\n"
            "white-list=false\npvp=false\n"
        ),
        "eula.txt": "eula=true",
        ".blessforge.json": json.dumps(MANIFEST_B, indent=2),
    },
}

LOG = [
    "[12:01:02] [main/INFO]: Starting minecraft server version 1.21.1",
    "[12:01:04] [main/INFO]: Loading 214 mods",
    "[12:01:31] [main/WARN]: Mod figura is client-only and was skipped",
    "[12:02:10] [Server thread/INFO]: Preparing level \"world\"",
    "[12:02:44] [Server thread/INFO]: Done (41.522s)! For help, type \"help\"",
    "[12:14:03] [Server thread/INFO]: Notch joined the game",
    "[12:14:09] [Server thread/INFO]: <Notch> anyone seen my pickaxe",
    "[12:15:41] [Server thread/INFO]: Alex joined the game",
    "[12:19:02] [Server thread/ERROR]: Encountered an unexpected exception",
    "[12:31:00] [Server thread/INFO]: There are 3 of a max of 20 players online: "
    "Notch, Alex, Steve",
]


def ok(data):
    return JSONResponse({"status": "ok", "data": data})


@app.get("/api/v2/servers")
async def servers():
    return ok(list(SERVERS.values()))


@app.get("/api/v2/servers/{sid}")
async def server(sid: str):
    return ok(SERVERS.get(sid, {}))


@app.get("/api/v2/servers/{sid}/stats")
async def stats(sid: str):
    return ok(STATS.get(sid, {}))


@app.post("/__inject")
async def inject(body: dict = Body(...)):
    """Append a line to the log, so console latency can be measured.

    Not part of Crafty's API -- it is the seam that makes "does a line
    reach the browser promptly" a question this harness can answer at all.
    Without it the console can only be eyeballed, which is how a 1.5s
    poll interval went unnoticed.
    """
    LOG.append(str(body.get("line") or ""))
    return ok({"lines": len(LOG)})


@app.get("/api/v2/servers/{sid}/logs")
async def logs(sid: str, raw: str = "true", file: str = ""):
    return ok(LOG if STATS.get(sid, {}).get("running") else [])


@app.post("/api/v2/servers/{sid}/files")
async def files(sid: str, body: dict = Body(...)):
    path = (body.get("path") or ".").replace("\\", "/").strip("/") or "."
    text = TEXT.get(sid, {}).get(path)
    if text is not None:
        return ok({"content": text, "path": path})
    tree = FILES.get(sid, {}).get(path)
    if tree is None:
        return JSONResponse({"status": "error", "error": "NOT_FOUND"},
                            status_code=404)
    out = {"root_path": {"path": path, "top": path == "."}}
    for name, meta in tree.items():
        rel = name if path == "." else f"{path}/{name}"
        out[name] = {**meta, "path": rel,
                     "modified": "2026-09-09 14:21:00"}
    return ok(out)


@app.patch("/api/v2/servers/{sid}/files")
async def write(sid: str, body: dict = Body(...)):
    TEXT.setdefault(sid, {})[body["path"].strip("/")] = body.get("contents", "")
    return ok({"written": True})


@app.patch("/api/v2/servers/{sid}/files/create")
async def rename(sid: str, body: dict = Body(...)):
    return ok({"renamed": True})


@app.put("/api/v2/servers/{sid}/files/create")
async def create(sid: str, body: dict = Body(...)):
    parent = (body.get("parent") or ".").strip("/") or "."
    name = body["name"]
    FILES.setdefault(sid, {}).setdefault(parent, {})[name] = {
        "dir": bool(body.get("directory")), "size": "0B"}
    if body.get("directory"):
        rel = name if parent == "." else f"{parent}/{name}"
        FILES[sid].setdefault(rel, {})
    return ok({"created": True})


@app.delete("/api/v2/servers/{sid}/files")
async def delete_file(sid: str, body: dict = Body(...)):
    rel = (body.get("path") or "").replace(chr(92), "/").strip("/")
    folder, _, name = rel.rpartition("/")
    tree = FILES.get(sid, {}).get(folder or ".", {})
    tree.pop(name, None)
    TEXT.get(sid, {}).pop(rel, None)
    return ok({"deleted": True})


@app.post("/api/v2/servers/{sid}/files/upload")
async def upload(sid: str, request: Request):
    """Crafty's chunked upload, as BlessForge actually drives it.

    Faithful on the two things that matter for testing the client:
    every header it demands is required here too, and each chunk's
    SHA-256 is verified rather than trusted. A mock that accepts
    anything would have let a broken hash or a missing header ship.
    """
    import hashlib as _h

    head = request.headers
    for required in ("fileid", "filename", "location", "filesize"):
        if not head.get(required):
            return JSONResponse(
                {"status": "error", "error": f"missing header {required}"},
                status_code=400)

    file_id = head["fileid"]
    name = head["filename"]
    loc = (head.get("location") or ".").replace(chr(92), "/").strip("/") or "."
    body = await request.body()

    if head.get("chunked", "").lower() == "true":
        if head.get("chunkId") is None and head.get("chunkid") is None:
            # The initiation request carries no chunk and no body.
            _CHUNKS[file_id] = {"parts": {}, "total": int(head.get("totalchunks", 1)),
                                "name": name, "loc": loc}
            return ok({"status": "ok", "fileId": file_id})

        rec = _CHUNKS.get(file_id)
        if rec is None:
            return JSONResponse({"status": "error", "error": "unknown fileId"},
                                status_code=400)
        want = head.get("chunkhash") or ""
        got = _h.sha256(body).hexdigest()
        if want and want != got:
            return JSONResponse(
                {"status": "error", "error": "chunk hash mismatch"},
                status_code=400)
        rec["parts"][int(head.get("chunkid", 0))] = body
        if len(rec["parts"]) < rec["total"]:
            return ok({"status": "ok", "received": len(rec["parts"])})
        body = b"".join(rec["parts"][i] for i in sorted(rec["parts"]))
        _CHUNKS.pop(file_id, None)

    # Land it, so the Files and Mods tabs actually show what was uploaded.
    tree = FILES.setdefault(sid, {}).setdefault(loc, {})
    size = len(body)
    tree[name] = {"dir": False,
                  "size": f"{size / 1024:.1f}KB" if size >= 1024 else f"{size}B"}
    return ok({"status": "ok", "fileName": name, "size": size})


@app.post("/api/v2/servers/{sid}/stdin")
async def stdin(sid: str, request: Request):
    line = (await request.body()).decode()
    if not STATS.get(sid, {}).get("running"):
        return JSONResponse({"status": "error", "error": "SERVER_NOT_RUNNING"})
    LOG.append(f"[now] [Server thread/INFO]: (mock) ran: {line}")
    return JSONResponse({"status": "ok"})


@app.post("/api/v2/servers/{sid}/action/{action}")
async def action(sid: str, action: str):
    st = STATS.setdefault(sid, {})
    if action in ("stop_server", "kill_server"):
        st.update(running=False, online=0, cpu=0, mem=0, mem_percent=0)
    else:
        st.update(running=True, online=3, cpu=38.0, mem=3_200_000_000,
                  mem_percent=41.0)
    return ok({"ok": True})


@app.patch("/api/v2/servers/{sid}")
async def patch_server(sid: str, body: dict = Body(...)):
    SERVERS.get(sid, {}).update(body)
    return ok({"patched": True})


@app.get("/api/v2/crafty/JarCache")
async def jarcache():
    return ok({"mc_java_servers": {"types": {
        "neoforge-installer": {"versions": {"1.21.1": {
            "url": ["https://example.invalid/neoforge.jar"], "sha256": ""}}},
        "paper": {"versions": {"1.21.4": {
            "url": ["https://example.invalid/paper.jar"], "sha256": ""}}},
    }}})


@app.get("/api/v2/servers/{sid}/files/{path:path}/download")
async def download(sid: str, path: str):
    import urllib.parse
    rel = urllib.parse.unquote(path)
    text = TEXT.get(sid, {}).get(rel)
    return Response(content=(text or "binary-placeholder").encode(),
                    media_type="application/octet-stream")
