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


_QUOTED_VALUE = re.compile(r'^("[^"]*"|\'[^\']*\')\\s*(?:#.*)?$')


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
        # Strip an inline comment. mods.toml files routinely carry
        # `modId="neoforge" #mandatory`, and taking the whole tail as the
        # value produced a dependency on a mod id with a comment stuck to it.
        _q = _QUOTED_VALUE.match(val)
        if _q:
            val = _q.group(1)
        elif not val.startswith(("'''", '"""')) and '#' in val:
            val = val.split('#', 1)[0].strip()
        if val.startswith(("'''", '"""')):
            val = val.strip("'\"")
        else:
            val = val.strip().strip('"').strip("'")

        if dep_current is not None:
            dep_current[key] = val
        elif current is not None:
            current[key] = val

    return {"mods": mods, "dependencies": deps}


# Where each loader puts the jars it embeds.
_NESTED_DIRS = ("META-INF/jars/", "META-INF/jarjar/")


def _add_nested(z, names, info: dict, depth: int = 0) -> None:
    """Record the mod ids of jars bundled inside this one.

    NeoForge and Fabric both let a mod ship its dependencies inside itself, and
    most large packs rely on it. A scan that reads only the outer jar sees
    those requirements as unsatisfied and reports a wall of missing
    dependencies for a pack that starts perfectly well -- which is exactly what
    it did.

    Nesting can be two deep in practice (a library bundling a library), so this
    recurses once and then stops: deeper than that costs more than it finds.
    """
    if depth > 1:
        return
    for name in names:
        if not name.lower().endswith(".jar"):
            continue
        if not any(name.startswith(d) for d in _NESTED_DIRS):
            continue
        try:
            blob = z.read(name)
        except Exception:
            continue
        try:
            inner = zipfile.ZipFile(io.BytesIO(blob))
        except Exception:
            continue
        inner_names = set(inner.namelist())
        found: list[str] = []
        try:
            if "fabric.mod.json" in inner_names:
                fm = json.loads(re.sub(
                    r"[\x00-\x1f]", " ",
                    inner.read("fabric.mod.json").decode("utf-8", "replace")))
                if fm.get("id"):
                    found.append(fm["id"])
                found += [p for p in (fm.get("provides") or []) if isinstance(p, str)]
            for entry in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if entry in inner_names:
                    parsed = _toml_mods(
                        inner.read(entry).decode("utf-8", "replace"))
                    found += [m.get("modId") for m in parsed["mods"] if m.get("modId")]
                    break
        except Exception:
            continue
        info["provides"].extend(f for f in found if f)
        info["nested"] += 1
        # A bundled jar can itself bundle one.
        _add_nested(inner, inner_names, info, depth + 1)


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
        # Every id this jar satisfies: its own, anything it declares under
        # `provides`, and the mod ids of every jar nested inside it. That last
        # one is why this exists -- modern packs ship dependencies jar-in-jar,
        # so a mod whose requirement is bundled inside another jar looks
        # missing to anything that only reads top-level metadata.
        "provides": [],
        "nested": 0,
        "fml_type": None,      # NeoForge: LIBRARY / GAMELIBRARY / MOD
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
            for d in ql.get("provides", []) or []:
                pid = d.get("id") if isinstance(d, dict) else d
                if pid:
                    info["provides"].append(pid)
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
            for pid in (fm.get("provides") or []):
                if isinstance(pid, str):
                    info["provides"].append(pid)
        _add_nested(z, names, info)
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
            # NeoForge 1.21 states optionality as `type = "optional"`;
            # Forge used `mandatory = false`. Reading only the older key made
            # every optional integration -- JEI, JourneyMap, EMI -- look like a
            # hard requirement, so disabling one client mod produced a screen
            # of "missing dependency" for a pack that starts fine.
            dep_type = str(dep.get("type", "")).strip().lower()
            if dep_type in ("optional", "discouraged", "incompatible"):
                mandatory = False
            elif dep_type == "required":
                mandatory = True
            else:
                mandatory = str(dep.get("mandatory", "true")).lower() != "false"
            info["dependencies"].append(
                {"id": did, "version": dep.get("versionRange"), "mandatory": mandatory}
            )
        # A single jar can declare several [[mods]] blocks; all of them are ids
        # it satisfies.
        for m in mods:
            mid = m.get("modId")
            if mid and mid != mod_id:
                info["provides"].append(mid)
        _add_nested(z, names, info)
        return info

    # A jar with no mod metadata of its own is not necessarily unreadable.
    # NeoForge lets a jar declare `FMLModType: LIBRARY` in its manifest and
    # carry the actual mod nested inside -- kotlinforforge ships exactly that
    # way -- so the nested scan has to run here too, or the mod it provides
    # looks missing and its dependents look broken.
    _add_nested(z, names, info)
    if "META-INF/MANIFEST.MF" in names:
        try:
            mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
            m = re.search(r"^FMLModType:\s*(\S+)", mf, re.M)
            if m:
                info["fml_type"] = m.group(1).strip()
        except Exception:
            pass
    if info["provides"] or info.get("fml_type"):
        info["loader"] = info["loader"] or "library"
        info["name"] = info["name"] or filename
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
