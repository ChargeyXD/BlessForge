"""Boot a server, and fix what its own boot proves is broken. Repeat.

A modpack that installs is not a modpack that runs. The commonest reasons
are mechanical and the loader says so in plain words:

  * a mod needs a library that is missing -- because it sits disabled, was
    never in the pack, or its author blocks third-party downloads;
  * a client-side mod crashes the dedicated server -- a server pack whose
    author got it wrong, or a client manifest built into a server;
  * the server runs on the wrong Java.

This loop boots the server with the smoke test, reads THAT boot's log and
crash report (never an older one), and applies only fixes the evidence
proves. Each round can apply several (Fabric lists every missing library at
once), no action is ever repeated, and the loop stops -- saying exactly why
-- the moment there is nothing it can prove.

What it will and will not do:

  * re-enable a disabled jar that provides a missing library -- unless that
    jar is itself client-side, in which case the mod that needs it is a
    client-side mod too, and that one is disabled instead;
  * install a missing library: CurseForge first, Modrinth second, a build
    for this Minecraft version and loader;
  * disable a mod a crash names with high confidence *and* client evidence
    (the loader blames it, and it is client-side by its own metadata, by
    name, or by the client classes in the crash);
  * as a last resort, disable a mod whose required library cannot be had at
    all -- its author blocks downloads and no other source carries it --
    because a server that boots without that mod beats one that never does.
    Said loudly, in the install log and on the Diagnose tab;
  * never disable a content mod on a guess, and never touch a server pack's
    mods unless its boot points at them.
"""
from __future__ import annotations

import io
import posixpath
import re
import time
import zipfile

from app import curseforge, diagnostics, engine, jarmeta, loaders, modrinth, packs, smoketest
from app import mods as modmgr
from app.jobs import Job

MAX_ROUNDS = 8
MOD_DIR = "mods"

# A crash that mentions these was a client-side class on a dedicated server.
_CLIENT_HINT = re.compile(
    r"net[/.]minecraft[/.]client|invalid dist DEDICATED_SERVER|in environment type SERVER"
    r"|com[/.]mojang[/.]blaze3d|org[/.]lwjgl", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _jar_key(file: str | None) -> str | None:
    """A jar's identity whatever its state on disk."""
    return file.removesuffix(".disabled") if file else None


def missing_dependencies(text: str) -> list[dict]:
    return diagnostics.missing_dependencies(text)


# --- what is on disk ---------------------------------------------------------


class Disk:
    """The mods folder, with jar metadata read lazily and remembered."""

    def __init__(self, server_id: str):
        self.server_id = server_id
        self.files: list[str] = []
        self._meta: dict[str, dict] = {}
        self._names: dict[str, set[str]] = {}

    async def refresh(self) -> None:
        try:
            entries = await engine.list_dir(self.server_id, MOD_DIR)
        except engine.EngineError:
            entries = {}
        self.files = sorted(
            n for n, m in entries.items()
            if n != "root_path" and isinstance(m, dict) and not m.get("dir")
            and n.lower().endswith((".jar", ".jar.disabled")))

    @property
    def enabled(self) -> list[str]:
        return [f for f in self.files if f.lower().endswith(".jar")]

    @property
    def disabled(self) -> list[str]:
        return [f for f in self.files if f.lower().endswith(".disabled")]

    async def meta(self, file: str) -> dict:
        if file not in self._meta:
            try:
                blob = await engine.download_file(self.server_id, f"{MOD_DIR}/{file}")
                self._meta[file] = jarmeta.parse(blob, file.removesuffix(".disabled"))
                with zipfile.ZipFile(io.BytesIO(blob)) as z:
                    self._names[file] = set(z.namelist())
            except Exception:       # noqa: BLE001
                self._meta.setdefault(file, {})
        return self._meta[file]

    async def holds(self, file: str, entry: str) -> bool:
        """Whether the jar carries `entry` (a mixin config, say) at its root."""
        await self.meta(file)
        names = self._names.get(file, set())
        return entry in names or any(n.endswith("/" + entry) for n in names)

    async def provides(self, file: str, mod_id: str) -> bool:
        info = await self.meta(file)
        ids = {(info.get("mod_id") or "").lower()} | {
            str(p).lower() for p in info.get("provides") or []}
        return mod_id.lower() in ids

    async def is_client_side(self, file: str) -> bool:
        if packs.is_client_only_jar(file):
            return True
        info = await self.meta(file)
        return (info.get("side") == "client"
                or info.get("side_inferred") == "client")

    async def find(self, mod_id: str, pool: list[str]) -> str | None:
        """The jar in `pool` that is mod `mod_id`: by name first, then metadata."""
        key = _norm(mod_id)
        ranked = sorted((f for f in pool if key and key in _norm(f.split("-")[0] + f)),
                        key=len)
        for f in ranked[:6]:
            if await self.provides(f, mod_id):
                return f
        guess = diagnostics._mod_id_to_file(mod_id, pool)
        if guess and await self.provides(guess, mod_id):
            return guess
        # Names lie (a jar called "ftb-library" is mod id "ftblibrary"):
        # read the rest, cheapest-looking first, but not the whole pack.
        for f in [f for f in pool if f not in ranked][:40]:
            if key and key[:4] in _norm(f) and await self.provides(f, mod_id):
                return f
        return guess


# --- finding a library to install --------------------------------------------


_LOADER_TAG = re.compile(r"\((?:[^()]|\([^()]*\))*\)|\[[^\]]*\]|\b(?:neo)?forge\b|\bfabric\b"
                         r"|\bquilt\b|\bmod\b", re.I)


def _project_key(text: str | None) -> str:
    """A catalogue name or slug as the mod id it stands for: loader tags,
    brackets and the word "mod" dropped. "FerriteCore (Fabric)",
    "ferritecore-fabric" and "FerriteCore ((Neo)Forge)" are all ferritecore."""
    return _norm(_LOADER_TAG.sub(" ", (text or "").replace("-", " ").replace("_", " ")))


def _is_project(mod_id: str, slug: str | None, name: str | None) -> bool:
    want = _norm(mod_id)
    return bool(want) and want in {_norm(slug), _norm(name), _project_key(slug),
                                   _project_key(name)}


async def find_library(mod_id: str, *, mc: str, loader: str) -> dict | None:
    """A build of `mod_id` for this Minecraft version and loader.

    CurseForge first -- it is where packs come from -- then Modrinth. A
    CurseForge file whose author blocks third-party downloads is passed over,
    not fetched (that is the author's call to make, and the API's terms).
    Catalogue names carry loader tags ("FerriteCore (Fabric)") and slugs
    carry them too ("c2me-fabric"); both are compared with those removed.
    """
    try:
        res = await curseforge.search(query=mod_id.replace("_", " ").replace("-", " "),
                                      class_id=curseforge.CLASS_MODS, game_version=mc,
                                      mod_loader=loader, page_size=10)
        for p in res.get("items") or []:
            if not _is_project(mod_id, p.get("slug"), p.get("name")):
                continue
            files = await curseforge.list_files(int(p["id"]), game_version=mc,
                                                mod_loader=loader)
            files = [f for f in files if not f.get("distribution_blocked")]
            files.sort(key=lambda f: (f.get("release_type") == "release", f.get("date") or ""),
                       reverse=True)
            if files:
                return {"do": "install", "source": "curseforge", "project_id": p["id"],
                        "file_id": files[0]["file_id"], "name": p.get("name") or mod_id,
                        "file": files[0].get("file_name")}
    except Exception:       # noqa: BLE001 -- Modrinth is still worth asking
        pass
    try:
        candidates = []
        for slug in dict.fromkeys((mod_id, mod_id.replace("_", "-"), f"{mod_id}-{loader}")):
            proj = await modrinth.get_project(slug)
            if proj:
                candidates.append(proj)
                break
        if not candidates:
            hits = (await modrinth.search(query=mod_id.replace("_", " "), game_version=mc,
                                          loader=loader, page_size=10))["items"]
            candidates = [h for h in hits if _is_project(mod_id, h.get("slug"), h.get("name"))]
        for proj in candidates[:2]:
            versions = await modrinth.list_versions(proj.get("slug") or proj["id"],
                                                    game_version=mc, loader=loader)
            versions = [v for v in versions if v.get("download_url")]
            versions.sort(key=lambda v: (v.get("release_type") == "release", v.get("date") or ""),
                          reverse=True)
            if versions:
                return {"do": "install", "source": "modrinth",
                        "project_id": versions[0].get("mod_id") or proj["id"],
                        "file_id": versions[0]["file_id"], "name": proj.get("name") or mod_id,
                        "file": versions[0].get("file_name")}
    except Exception:       # noqa: BLE001
        pass
    return None


# --- deciding -------------------------------------------------------------------


async def _crash_text(server_id: str, since: float) -> str:
    """This boot's log, plus a crash report only if this boot wrote it."""
    ctx = await diagnostics.read_crash_context(server_id)
    text = ctx.get("log_text") or ""
    crash_path = ctx.get("crash_path")
    if crash_path and ctx.get("crash_text"):
        try:
            entries = await engine.list_dir(server_id, "crash-reports")
            meta = entries.get(posixpath.basename(crash_path)) or {}
            modified = float(meta.get("modified") or 0)
        except Exception:       # noqa: BLE001
            modified = 0
        if modified >= since - 5:
            text = ctx["crash_text"] + "\n" + text
    trail = await diagnostics.mixin_context(server_id, text)
    if trail:
        text += "\n" + trail
    return text


async def decide(server_id: str, text: str, *, mc: str, loader: str, disk: Disk,
                 tried: set) -> tuple[list[dict], list[str]]:
    """The actions this boot's evidence proves, and what could not be fixed."""
    actions: list[dict] = []
    unresolved: list[str] = []
    planned: set[str] = set()

    def plan(action: dict) -> None:
        key = (action["do"], _jar_key(action.get("file")) or str(action.get("project_id")))
        # Never undo this session's own repair: a jar disabled is not
        # re-enabled, and one enabled is not disabled, or the loop flip-flops.
        undo = ({"disable": "enable", "enable": "disable"}.get(action["do"]), key[1])
        if key in tried or key in planned or undo in tried:
            return
        planned.add(key)
        actions.append(action)

    # 1. The wrong Java is one fault, whatever mod reported it.
    for f in diagnostics.scan_text(text):
        fix = f.get("fix") or {}
        if fix.get("action") == "set_java" and fix.get("java_major"):
            plan({"do": "set_java", "major": int(fix["java_major"]), "file": None,
                  "project_id": f"java{fix['java_major']}",
                  "why": f["title"]})
            return actions, unresolved

    # 2. Missing libraries.
    for miss in missing_dependencies(text):
        dep, by = miss["dep"], miss["by"]
        provider = await disk.find(dep, disk.disabled) if disk.disabled else None
        dependent = await disk.find(by, disk.enabled)
        if provider:
            if await disk.is_client_side(provider):
                # A mod that needs a client-side library is itself client-side.
                if dependent:
                    plan({"do": "disable", "file": dependent,
                          "why": f"it needs {dep}, which is client-side ({provider}) -- so "
                                 "this mod only does anything on a client"})
                else:
                    unresolved.append(f"{by} needs the client-side {dep}")
            else:
                plan({"do": "enable", "file": provider,
                      "why": f"{by} needs {dep}, and this disabled jar provides it"})
            continue
        found = await find_library(dep, mc=mc, loader=loader)
        if found:
            found["why"] = (f"{by} needs {dep}" + (f" {miss['min']}+" if miss.get("min")
                                                    and miss["min"] not in ("0", "*") else "")
                            + f"; installing {found['name']} from {found['source']}")
            plan(found)
            continue
        if dependent:
            plan({"do": "disable", "file": dependent, "last_resort": True,
                  "why": f"it requires {dep}, which cannot be obtained -- not published "
                         f"for {loader} {mc} anywhere BlessForge may download from (its "
                         "author may block third-party downloads). Upload that jar to "
                         "the mods folder to bring this mod back."})
        else:
            unresolved.append(f"{by} needs {dep}, which could not be found")
    if actions:
        return actions, unresolved

    # 3. A client-side mod the crash names.
    for c in diagnostics.attribute_crash(text, disk.enabled):
        if c["score"] < 70:
            continue
        jar = c["file"]
        # Named from a mixin config's name: act only if the jar holds it.
        if c.get("mixin_config") and not await disk.holds(jar, c["mixin_config"]):
            unresolved.append(f"a mixin from {c['mixin_config']} pulls a client class onto "
                              "the server, and no installed jar could be matched to it")
            continue
        evidence = (c.get("evidence") or "") + " " + " ".join(c.get("reasons") or [])
        client = (c["score"] == 70 or bool(_CLIENT_HINT.search(evidence))
                  or await disk.is_client_side(jar))
        if client:
            plan({"do": "disable", "file": jar,
                  "why": f"the boot crashed on it ({c['why'][:160]}) and it is a "
                         "client-side mod"})
        elif c["score"] >= 90:
            unresolved.append(f"{jar}: {c['why'][:200]}")
    if not actions:
        for f in diagnostics.scan_text(text):
            if f["severity"] == "critical" and f["title"] not in unresolved:
                unresolved.append(f["title"])
    return actions, unresolved


# --- doing ---------------------------------------------------------------------


async def apply(job: Job, server_id: str, action: dict, *, mc: str) -> bool:
    try:
        if action["do"] == "disable":
            await modmgr.set_enabled(server_id, action["file"], False, MOD_DIR)
        elif action["do"] == "enable":
            await modmgr.set_enabled(server_id, action["file"], True, MOD_DIR)
        elif action["do"] == "install":
            await modmgr.add_mod_with_dependencies(
                job, server_id, source=action["source"], project_id=action["project_id"],
                file_id=action["file_id"], directory=MOD_DIR)
        elif action["do"] == "set_java":
            await engine.set_runtime(server_id, mc, action["major"])
        job.log_line(f"Repair: {action['do']} {action.get('file') or action.get('name') or ''}"
                     f" -- {action['why']}", "warn" if action.get("last_resort") else "info")
        return True
    except Exception as e:      # noqa: BLE001 -- reported, the loop decides again
        job.log_line(f"Repair failed: {action['do']} {action.get('file') or ''}: {e}", "warn")
        return False


def boot_timeout(mod_count: int) -> int:
    """Long enough for a big pack on a modest node: its first boot also
    generates the world."""
    return int(min(2400, max(300, 300 + 4 * mod_count)))


async def boot_and_repair(job: Job, server_id: str, *, rounds: int = MAX_ROUNDS,
                          timeout: int | None = None) -> dict:
    """Boot; on failure apply what the boot proves; boot again. Stops when up."""
    manifest = await engine.read_manifest(server_id)
    mc = manifest.get("minecraft") or ""
    loader = loaders.family_of(manifest.get("loader") or "")
    disk = Disk(server_id)
    await disk.refresh()
    timeout = timeout or boot_timeout(len(disk.enabled))
    done: list[dict] = []
    tried: set = set()
    last: dict = {}
    for n in range(1, rounds + 1):
        job.log_line(f"First boot, attempt {n}" + (f" (after {len(done)} repair(s))"
                                                   if done else ""))
        started = time.time()
        last = await smoketest.run(job, server_id, timeout=timeout)
        if last["outcome"] == "ready":
            return _result("ready", n, done, last)
        if last["outcome"] == "timeout":
            # Slow is not broken: a big pack on a small node can take this long.
            return _result("timeout", n, done, last,
                           f"not up within {timeout}s -- it may simply be slow here")
        text = await _crash_text(server_id, started)
        await disk.refresh()
        actions, unresolved = await decide(server_id, text, mc=mc, loader=loader,
                                           disk=disk, tried=tried)
        if not actions:
            return _result("failed", n, done, last,
                           "; ".join(unresolved[:4]) or
                           "the boot failed for a reason BlessForge cannot fix by itself "
                           "-- the Diagnose tab has the log")
        for action in actions:
            tried.add((action["do"], _jar_key(action.get("file"))
                       or str(action.get("project_id"))))
            if await apply(job, server_id, action, mc=mc):
                done.append({k: v for k, v in action.items() if k != "project_id"}
                            | {"round": n})
        await _record(server_id, done)
    return _result("failed", rounds, done, last, f"still failing after {rounds} boots")


def _result(outcome: str, rounds: int, actions: list[dict], boot: dict,
            why: str | None = None) -> dict:
    return {"outcome": outcome, "rounds": rounds, "repairs": actions, "why": why,
            "seconds": boot.get("seconds"), "failure_line": boot.get("failure_line"),
            "findings": [{k: f.get(k) for k in ("severity", "title")}
                         for f in boot.get("findings") or []][:8]}


async def _record(server_id: str, repairs: list[dict]) -> None:
    """Keep the repairs in the manifest: the Mods and Diagnose tabs say why."""
    try:
        manifest = await engine.read_manifest(server_id)
        manifest["repairs"] = repairs
        by_file = {r["file"]: r for r in repairs if r.get("file")}
        for rec in manifest.get("mods") or []:
            name = posixpath.basename(rec.get("file") or "")
            r = by_file.get(name) or by_file.get(name.removesuffix(".disabled"))
            if r:
                rec["repair"] = {"do": r["do"], "why": r["why"]}
        await engine.write_manifest(server_id, manifest)
    except Exception:       # noqa: BLE001 -- the repairs happened; recording is a courtesy
        pass


async def fix_dependency(job: Job, server_id: str, mod_id: str,
                         required_by: str | None = None) -> dict:
    """Diagnose's "fix this missing library": enable it, or install a build."""
    manifest = await engine.read_manifest(server_id)
    mc = manifest.get("minecraft") or ""
    loader = loaders.family_of(manifest.get("loader") or "")
    disk = Disk(server_id)
    await disk.refresh()
    provider = await disk.find(mod_id, disk.disabled) if disk.disabled else None
    if provider:
        action = {"do": "enable", "file": provider,
                  "why": f"{required_by or 'a mod'} needs {mod_id}; this disabled jar "
                         "provides it"}
    else:
        action = await find_library(mod_id, mc=mc, loader=loader)
        if not action:
            raise RuntimeError(
                f"No build of {mod_id} for {loader} {mc} could be found on CurseForge or "
                "Modrinth that BlessForge may download. If its author blocks third-party "
                "downloads, get it from its project page and upload it to mods/.")
        action["why"] = f"{required_by or 'a mod'} needs {mod_id}"
    ok = await apply(job, server_id, action, mc=mc)
    if not ok:
        raise RuntimeError(f"could not {action['do']} {mod_id}")
    return {"action": action["do"], "file": action.get("file"), "name": action.get("name"),
            "source": action.get("source"), "why": action["why"]}
