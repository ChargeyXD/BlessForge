"""Read mod metadata out of a jar without unpacking it to disk.

Three ecosystems, three formats:
  * NeoForge 1.20.5+   META-INF/neoforge.mods.toml
  * Forge 1.13+        META-INF/mods.toml
  * Fabric / Quilt     fabric.mod.json / quilt.mod.json

Only the fabric/quilt formats declare a side ("environment": "client"), so
that is the one hard signal we get for client-only mods. Forge-family mods
have to be judged on their declared dependencies plus the crash log.
"""
from __future__ import annotations

import io
import json
import re
import zipfile

# Dependency ids that are the loader/game itself rather than another mod.
_NOT_A_MOD = {
    "minecraft", "forge", "neoforge", "fabric", "fabricloader",
    "fabric-api", "quilt_loader", "java", "quilt_base",
}

# Fabric entrypoint names that only ever run client-side.
_CLIENT_ENTRYPOINTS = {
    "client", "modmenu", "emi", "rei_client", "jei_client", "clothconfig",
    "fabric-client-tags-api-v1", "preLaunchClient",
}


def _toml_mods(text: str) -> dict:
    """Minimal TOML reader for mods.toml.

    A real TOML parser is overkill here and tomllib chokes on some of the
    hand-edited files in the wild, so we scan for the handful of keys we
    actually use and tolerate anything else.
    """
    mods: list[dict] = []
    deps: dict[str, list[dict]] = {}
    current: dict | None = None
    dep_owner: str | None = None
    dep_current: dict | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("[[mods]]"):
            current = {}
            mods.append(current)
            dep_current = None
            continue

        m = re.match(r"\[\[dependencies\.([^\]]+)\]\]", line)
        if m:
            dep_owner = m.group(1).strip().strip('"')
            dep_current = {}
            deps.setdefault(dep_owner, []).append(dep_current)
            current = None
            continue

        kv = re.match(r'^([A-Za-z_][\w-]*)\s*=\s*(.+)$', line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val.startswith(("'''", '"""')):
            val = val.strip("'\"")
        else:
            val = val.strip().strip('"').strip("'")

        if dep_current is not None:
            dep_current[key] = val
        elif current is not None:
            current[key] = val

    return {"mods": mods, "dependencies": deps}


def parse(data: bytes, filename: str = "") -> dict:
    """Return normalised metadata for a mod jar."""
    info: dict = {
        "mod_id": None,
        "name": None,
        "version": None,
        "loader": None,
        "side": None,          # "client" | "server" | "both" | None
        "side_inferred": None, # our guess when the jar's own claim is unhelpful
        "side_reason": None,
        "entrypoints": [],
        "dependencies": [],    # [{id, version, mandatory}]
        "description": None,
        "parse_error": None,
    }
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        info["parse_error"] = f"not a readable jar: {e}"
        return info

    names = set(z.namelist())

    # --- Fabric / Quilt ------------------------------------------------
    for entry, loader in (("fabric.mod.json", "fabric"), ("quilt.mod.json", "quilt")):
        if entry not in names:
            continue
        try:
            raw = z.read(entry).decode("utf-8", "replace")
            # Some mods ship fabric.mod.json with control chars or trailing commas.
            fm = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
        except Exception as e:
            info["parse_error"] = f"{entry}: {e}"
            continue
        if loader == "quilt":
            ql = fm.get("quilt_loader", {})
            info.update(
                mod_id=ql.get("id"),
                name=(ql.get("metadata") or {}).get("name"),
                version=ql.get("version"),
                loader="quilt",
            )
            for d in ql.get("depends", []) or []:
                did = d.get("id") if isinstance(d, dict) else d
                if did and did not in _NOT_A_MOD:
                    info["dependencies"].append(
                        {"id": did, "version": (d.get("versions") if isinstance(d, dict) else None),
                         "mandatory": True}
                    )
        else:
            env = fm.get("environment")
            entrypoints = fm.get("entrypoints") or {}
            info.update(
                mod_id=fm.get("id"),
                name=fm.get("name"),
                version=fm.get("version"),
                loader="fabric",
                description=fm.get("description"),
                side={"client": "client", "server": "server", "*": "both"}.get(env),
                entrypoints=sorted(entrypoints.keys()),
            )
            # A mod whose only runnable entrypoints are client ones has
            # nothing for a server to run, whatever `environment` claims.
            # Requires every entrypoint to be a *known* client one: mods
            # routinely register custom entrypoints for their own libraries
            # (e.g. "glitchcore"), and treating those as client-side wrongly
            # condemns perfectly good server mods.
            if info["side"] != "client":
                runnable = {k for k, v in entrypoints.items() if v}
                if runnable and runnable <= _CLIENT_ENTRYPOINTS:
                    info["side_inferred"] = "client"
                    info["side_reason"] = (
                        "declares only client entrypoints ("
                        + ", ".join(sorted(runnable)) + ")"
                    )
            for did, ver in (fm.get("depends") or {}).items():
                if did not in _NOT_A_MOD:
                    info["dependencies"].append(
                        {"id": did, "version": ver, "mandatory": True}
                    )
        return info

    # --- Forge / NeoForge ----------------------------------------------
    for entry, loader in (
        ("META-INF/neoforge.mods.toml", "neoforge"),
        ("META-INF/mods.toml", "forge"),
    ):
        if entry not in names:
            continue
        try:
            parsed = _toml_mods(z.read(entry).decode("utf-8", "replace"))
        except Exception as e:
            info["parse_error"] = f"{entry}: {e}"
            continue
        mods = parsed["mods"]
        if not mods:
            continue
        first = mods[0]
        mod_id = first.get("modId")
        version = first.get("version")
        # mods.toml often carries "${file.jarVersion}" -- recover the real
        # version from the manifest when that happens.
        if version and version.startswith("${"):
            version = _manifest_version(z) or version
        info.update(
            mod_id=mod_id,
            name=first.get("displayName") or mod_id,
            version=version,
            loader=loader,
            description=first.get("description"),
        )
        for dep in parsed["dependencies"].get(mod_id or "", []):
            did = dep.get("modId")
            if not did or did in _NOT_A_MOD:
                continue
            mandatory = str(dep.get("mandatory", "true")).lower() != "false"
            info["dependencies"].append(
                {"id": did, "version": dep.get("versionRange"), "mandatory": mandatory}
            )
        return info

    info["parse_error"] = "no recognised mod metadata"
    return info


def _manifest_version(z: zipfile.ZipFile) -> str | None:
    try:
        mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"Implementation-Version:\s*(.+)", mf)
    return m.group(1).strip() if m else None


_VERSION_RE = re.compile(
    r"^(?P<name>.+?)[-_](?P<version>v?\d[\w.+]*(?:-[A-Za-z0-9.]+)*)$"
)


def guess_from_filename(filename: str) -> dict:
    """Best-effort name/version split for jars we cannot download."""
    stem = re.sub(r"\.jar(\.disabled)?$", "", filename, flags=re.I)
    # Strip common loader/mc-version noise so the name reads cleanly.
    cleaned = re.sub(
        r"[-_](forge|fabric|neoforge|quilt|mc)?[-_]?\d+\.\d+(\.\d+)?", "", stem,
        flags=re.I,
    )
    m = _VERSION_RE.match(stem)
    return {
        "name": (m.group("name") if m else cleaned or stem).replace("_", " ").strip(),
        "version": m.group("version") if m else None,
    }
