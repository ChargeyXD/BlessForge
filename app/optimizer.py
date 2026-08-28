"""Per-instance tuning: build a proposal, apply only what the user selects."""
from __future__ import annotations

import re

from app import crafty, specs

# server.properties keys we are willing to write. Anything outside this list
# is left alone -- tuning should never quietly change gameplay settings.
SAFE_PROPERTY_KEYS = {p[0] for p in specs.PROPERTY_TUNING}


async def build_plan(server_id: str) -> dict:
    """Everything the Optimize tab needs: host, current state, proposal."""
    host = specs.effective_host()
    manifest = await crafty.read_studio_manifest(server_id)
    server = await crafty.get_server(server_id)

    mod_count = 0
    try:
        entries = await crafty.list_dir(server_id, "mods")
        mod_count = sum(
            1 for n, m in entries.items()
            if n != "root_path" and isinstance(m, dict) and not m.get("dir")
            and n.lower().endswith(".jar")
        )
    except crafty.CraftyError:
        pass

    pack = manifest.get("pack") or {}
    pack_ram_mb = int(manifest.get("recommended_ram_mb") or 0)

    memory = specs.recommend_memory(
        pack_recommended_mb=pack_ram_mb, mod_count=mod_count, host=host
    )
    current = await specs.read_current_jvm_args(server_id)

    heap = memory["heap_gb"]
    flag_plan = specs.build_flag_plan(
        heap_gb=heap,
        host=host,
        mc_version=manifest.get("minecraft", ""),
        loader=manifest.get("loader", ""),
    )
    # Reflect what is already applied so the UI shows real state, not a wish.
    applied = set(current["flags"])
    for entry in flag_plan:
        entry["applied"] = entry["flag"] in applied
    extra_applied = sorted(applied - {e["flag"] for e in flag_plan})

    props_plan = specs.build_property_plan(host, mod_count)
    try:
        raw_props = await crafty.read_file(server_id, "server.properties")
        existing = dict(
            re.findall(r"^([\w.-]+)=(.*)$", raw_props, re.M)
        )
    except crafty.CraftyError:
        existing = {}
    for entry in props_plan:
        entry["current"] = existing.get(entry["key"])
        entry["applied"] = entry["current"] == entry["value"]

    loader = manifest.get("loader") or ""
    supports_jvm_file = loader in ("forge", "neoforge")

    return {
        "host": host,
        "pack": pack,
        "mod_count": mod_count,
        "minecraft": manifest.get("minecraft"),
        "loader": loader,
        "memory": memory,
        "current": {
            "xmx_mb": current["xmx_mb"],
            "xms_mb": current["xms_mb"],
            "flags": current["flags"],
            "exists": current["exists"],
            "extra_flags": extra_applied,
        },
        "flags": flag_plan,
        "properties": props_plan,
        "jvm_file_supported": supports_jvm_file,
        "note": None if supports_jvm_file else (
            "Fabric and vanilla instances take their memory settings from "
            "Crafty's launch command rather than user_jvm_args.txt, so heap "
            "changes here are written to the command instead."
        ),
        "execution_command": server.get("execution_command"),
    }


async def apply(server_id: str, selection: dict) -> dict:
    """Apply a user-approved subset of the proposal.

    selection = {
      "heap_gb": 6,
      "flags": ["-XX:+UseG1GC", ...],       # exact flag strings to write
      "properties": {"view-distance": "8"}, # keys must be in SAFE_PROPERTY_KEYS
      "xms_equals_xmx": true
    }
    """
    result: dict = {"applied": [], "skipped": []}
    manifest = await crafty.read_studio_manifest(server_id)
    loader = manifest.get("loader") or ""

    heap = float(selection.get("heap_gb") or 0)
    flags = [f for f in (selection.get("flags") or []) if _safe_flag(f)]
    rejected = [f for f in (selection.get("flags") or []) if not _safe_flag(f)]
    if rejected:
        result["skipped"].append(
            {"what": "flags", "why": "rejected unsafe JVM arguments",
             "items": rejected}
        )

    if heap:
        if loader in ("forge", "neoforge"):
            body = specs.render_jvm_args(
                flags, heap,
                xms_equals_xmx=bool(selection.get("xms_equals_xmx", True)),
            )
            await crafty.write_file(server_id, "user_jvm_args.txt", body)
            result["applied"].append(
                f"user_jvm_args.txt: {heap:g} GB heap, {len(flags)} flags"
            )
        else:
            # Fabric/vanilla: memory lives in Crafty's launch command.
            updated = await set_command_memory(server_id, heap, flags)
            result["applied"].append(
                f"launch command: {heap:g} GB heap, {len(flags)} flags"
            )
            result["execution_command"] = updated

    props = selection.get("properties") or {}
    safe = {k: v for k, v in props.items() if k in SAFE_PROPERTY_KEYS}
    unsafe = [k for k in props if k not in SAFE_PROPERTY_KEYS]
    if unsafe:
        result["skipped"].append(
            {"what": "properties", "why": "not in the tunable allow-list",
             "items": unsafe}
        )
    if safe:
        await _patch_properties(server_id, safe)
        result["applied"].append(
            f"server.properties: {', '.join(f'{k}={v}' for k, v in safe.items())}"
        )

    result["restart_required"] = bool(result["applied"])
    return result


def _safe_flag(flag: str) -> bool:
    """Only accept JVM arguments, never shell metacharacters."""
    if not isinstance(flag, str) or not flag.startswith("-"):
        return False
    if any(c in flag for c in ('"', "'", "`", "$", ";", "|", "&", "\n", "\r", " ")):
        return False
    return bool(re.match(r"^-[\w:.+=/-]+$", flag))


async def _patch_properties(server_id: str, updates: dict) -> None:
    try:
        raw = await crafty.read_file(server_id, "server.properties")
    except crafty.CraftyError:
        raw = ""
    lines = raw.splitlines()
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^([\w.-]+)=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    await crafty.write_file(server_id, "server.properties", "\n".join(out) + "\n")


async def set_command_memory(
    server_id: str, heap_gb: float, flags: list[str]
) -> str:
    """Swap -Xms/-Xmx (and our flags) inside Crafty's launch command.

    Used for Fabric and vanilla, which have no user_jvm_args.txt. The java
    path and everything after the flags is preserved exactly.

    Public because the installer needs it too: a fresh Fabric instance keeps
    whatever heap Crafty derived from the pack's requested RAM until someone
    rewrites this command, and "someone" used to mean the user visiting the
    Optimize tab and pressing a button.
    """
    server = await crafty.get_server(server_id)
    command = server.get("execution_command") or ""
    if not command:
        raise RuntimeError("this instance has no execution command yet")

    tokens = command.split()
    if not tokens:
        raise RuntimeError("could not parse the execution command")

    java = tokens[0]
    rest = []
    for token in tokens[1:]:
        # Drop the old memory and any JVM tuning flags we previously wrote;
        # keep -jar/@argfiles/nogui and anything else the launcher needs.
        if re.match(r"^-Xm[sx]", token):
            continue
        if token.startswith("-XX:") or token.startswith("-Dusing.aikars") \
           or token.startswith("-Daikars") or token == "-Dlog4j2.formatMsgNoLookups=true" \
           or token.startswith("-Dfml.readTimeout"):
            continue
        rest.append(token)

    heap_mb = int(heap_gb * 1024)
    new_command = " ".join(
        [java, f"-Xms{heap_mb}M", f"-Xmx{heap_mb}M", *flags, *rest]
    ) + " "
    await crafty.patch_server(server_id, {"execution_command": new_command})
    return new_command
