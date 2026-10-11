"""Server templates: what each loader needs to install, run and boot.

One JSON file per loader in `templates/` at the repo root -- Vanilla, Paper,
Purpur, Folia, Fabric, Forge and NeoForge -- read once and checked on load.
A template says:

  * what the create screen shows (title, blurb, logo, mods or plugins);
  * **java**: which runtime it runs on. `{"from": "minecraft"}` means what
    Mojang's own version JSON says (`javaVersion.majorVersion`), rounded up
    to a runtime the nodes have (8, 17, 21, 25) -- 1.17 asks for 16 and gets
    17, 26.x asks for 25. Offline, `required_java_major()` answers instead;
    the two agree on every release so far;
  * **install**: `server_jar` (download one jar, done) or `installer` (download
    the installer and run it once on the node, in a short-lived installer
    container with the same lock-down as the server);
  * **startup**: the command, with `{jvm}` (heap and flags from the
    optimizer) and `{jar}`, or for Forge/NeoForge `{args_file}` read from the
    run.sh the installer wrote -- flags then go in `user_jvm_args.txt`;
  * **boot**: what a finished boot prints, and how long to wait for it.

The resolving and downloading itself stays in `app/loaders.py` and the
ladder in `app/provision.py`; a template only says which rung applies.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from app.engine.base import required_java_major

DIR = Path(__file__).resolve().parents[1] / "templates"
RUNTIMES = (8, 17, 21, 25)
_FETCH = {"server_jar", "installer"}
_KINDS = {"mods", "plugins", "none"}


class TemplateError(ValueError):
    pass


def _check(doc: dict, path: Path) -> dict:
    def need(cond: bool, what: str) -> None:
        if not cond:
            raise TemplateError(f"{path.name}: {what}")

    need(doc.get("schema") == 1, "schema must be 1")
    need(re.fullmatch(r"[a-z]+", str(doc.get("key") or "")) is not None, "bad key")
    need(path.stem == doc["key"], "the file name must be the key")
    for field in ("title", "blurb", "logo", "mod_directory", "source"):
        need(isinstance(doc.get(field), str) and doc[field], f"{field} is required")
    need(doc.get("kind") in _KINDS, f"kind must be one of {sorted(_KINDS)}")
    need((doc.get("java") or {}).get("from") == "minecraft"
         or isinstance((doc.get("java") or {}).get("major"), int),
         'java must be {"from": "minecraft"} or {"major": N}')
    install = doc.get("install") or {}
    need(install.get("fetch") in _FETCH, f"install.fetch must be one of {sorted(_FETCH)}")
    startup = doc.get("startup") or {}
    need(isinstance(startup.get("command"), str) and startup["command"].startswith("java "),
         "startup.command must start with java")
    if install["fetch"] == "installer":
        run = install.get("run") or []
        need(isinstance(run, list) and run[:1] == ["java"] and "{installer}" in run,
             "install.run must be a java argv naming {installer}")
        need("{args_file}" in startup["command"] and startup.get("legacy_command"),
             "an installer template needs {args_file} and a legacy_command")
    else:
        need("{jar}" in startup["command"], "startup.command must name {jar}")
    boot = doc.get("boot") or {}
    try:
        re.compile(boot.get("ready") or "")
    except re.error as e:
        raise TemplateError(f"{path.name}: boot.ready is not a regex ({e})")
    need(int(boot.get("timeout") or 0) >= 60, "boot.timeout must be at least 60")
    return doc


@lru_cache(maxsize=1)
def all_templates() -> dict[str, dict]:
    out = {}
    for path in sorted(DIR.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        out[doc.get("key")] = _check(doc, path)
    if not out:
        raise TemplateError(f"no templates in {DIR}")
    return dict(sorted(out.items(), key=lambda kv: kv[1].get("order", 99)))


def get(key: str) -> dict:
    tpl = all_templates().get((key or "").lower())
    if tpl is None:
        raise TemplateError(f"'{key}' is not a loader this can create. Choose one of: "
                            + ", ".join(all_templates()))
    return tpl


def catalogue() -> list[dict]:
    """The create screen's list, in the order it shows them."""
    return [{k: t[k] for k in ("key", "title", "kind", "logo", "blurb")}
            for t in all_templates().values()]


# --- Java ---------------------------------------------------------------------


def runtime_for(major: int) -> int:
    """The runtime a node has for a Java requirement: the same or the next newer."""
    return next((r for r in RUNTIMES if r >= int(major)), RUNTIMES[-1])


async def java_major(key: str, mc_version: str) -> int:
    """Which Java this template runs `mc_version` on."""
    java = get(key).get("java") or {}
    if isinstance(java.get("major"), int):
        return runtime_for(java["major"])
    fallback = runtime_for(required_java_major(mc_version))
    try:
        from app import loaders
        manifest = await loaders._json(loaders.MOJANG_MANIFEST, timeout=10)
        entry = next((v for v in manifest.get("versions", []) if v["id"] == mc_version),
                     None)
        if entry is None:
            return fallback
        detail = await loaders._json(entry["url"], timeout=10)
        want = int((detail.get("javaVersion") or {}).get("majorVersion") or 0)
    except Exception:       # noqa: BLE001 -- offline: the table answers
        return fallback
    return runtime_for(want) if want else fallback


# --- install and startup ---------------------------------------------------------


def installer_steps(key: str, installer: str) -> dict:
    """The installer run for an installer template: argv, cleanup, timeout."""
    install = get(key)["install"]
    fill = lambda s: s.replace("{installer}", installer)  # noqa: E731
    return {"run": [fill(a) for a in install["run"]],
            "remove_after": [fill(a) for a in install.get("remove_after") or []],
            "timeout": int(install.get("timeout") or 900)}


def startup(key: str, *, jar: str | None = None, args_file: str | None = None,
            jvm: str = "-Xms1G -Xmx2G") -> str:
    """The startup command. `args_file` for a modern Forge/NeoForge install,
    `jar` for everything else (and for a pre-1.17 Forge)."""
    st = get(key)["startup"]
    if args_file:
        return st["command"].replace("{args_file}", args_file)
    pattern = st["command"] if "{jar}" in st["command"] else st["legacy_command"]
    return pattern.replace("{jvm}", jvm).replace("{jar}", jar or "server.jar")


def jvm_args_file(key: str) -> str | None:
    return get(key)["startup"].get("jvm_args_file")


def boot(key: str) -> dict:
    b = get(key)["boot"]
    return {"ready": re.compile(b["ready"]), "timeout": int(b["timeout"])}
