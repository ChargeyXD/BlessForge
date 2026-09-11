"""Per-instance tuning: build a proposal, apply only what the user selects.

The proposal is deterministic. `app/specs.py` sizes a heap from what the host
actually has and lays out Aikar's flags; nothing here guesses.

On top of that sits an OPTIONAL, SILENT advisory pass (see "assisted tuning"
below). The assistant never replaces the optimizer -- it adjusts a number the
optimizer already produced, every adjustment is clamped against the same host
arithmetic that produced the baseline, and the whole thing is invisible when
it is off, unconfigured, unreachable, slow or wrong. The user never has to
talk to it.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from app import ai, config, crafty, specs

# server.properties keys we are willing to write. Anything outside this list
# is left alone -- tuning should never quietly change gameplay settings.
SAFE_PROPERTY_KEYS = {p[0] for p in specs.PROPERTY_TUNING}


async def build_plan(server_id: str) -> dict:
    """Everything the Optimize tab needs: host, current state, proposal.

    THIS FUNCTION NEVER TALKS TO THE MODEL. It reads advice out of a cache
    and nothing more, so the Tune screen renders at Crafty's speed whether
    the assistant is fast, slow, or on fire. The model is reached only from
    `advise()`, which the front end calls *after* its first paint.
    """
    host = specs.effective_host()
    manifest = await crafty.read_studio_manifest(server_id)
    server = await crafty.get_server(server_id)

    mod_count = 0
    mod_names: list[str] = []
    try:
        entries = await crafty.list_dir(server_id, "mods")
        for name, meta in entries.items():
            if name == "root_path" or not isinstance(meta, dict) or meta.get("dir"):
                continue
            if name.lower().endswith(".jar"):
                mod_count += 1
                mod_names.append(name)
    except crafty.CraftyError:
        pass
    mod_names.sort()

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

    plan = {
        "host": host,
        "pack": pack,
        "mod_count": mod_count,
        "mod_names": mod_names,
        "minecraft": manifest.get("minecraft"),
        "loader": loader,
        "memory": memory,
        "current": {
            "xmx_mb": current["xmx_mb"],
            "xms_mb": current["xms_mb"],
            "flags": current["flags"],
            "exists": current["exists"],
            "extra_flags": [],
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

    # Provenance, declared before anything can change it: every number in a
    # freshly built plan came from the deterministic optimizer, and says so.
    _stamp_source(plan, "optimizer")

    fingerprint = _fingerprint(plan)
    plan["fingerprint"] = fingerprint
    cached = _cache_get(server_id, fingerprint)
    if cached and ai.tuning_enabled():
        apply_advice(plan, cached["advice"], at=cached.get("at"))
    else:
        plan["advice"] = {
            "state": "off" if not ai.tuning_enabled() else "none",
            "enabled": ai.tuning_enabled(),
            "changes": [],
            "rejected": [],
            "summary": "",
            "fingerprint": fingerprint,
        }

    # Computed last: the AI pass may have swapped a parameterised flag for a
    # different value, and "flags set here that this proposal does not
    # manage" has to be measured against the plan as it finally reads.
    plan["current"]["extra_flags"] = sorted(
        applied - {e["flag"] for e in plan["flags"]}
    )
    return plan


def _stamp_source(plan: dict, source: str) -> None:
    plan["memory"]["source"] = source
    plan["memory"]["optimizer_heap_gb"] = plan["memory"]["heap_gb"]
    for entry in plan["flags"]:
        entry["source"] = source
        entry["optimizer_flag"] = entry["flag"]
        entry["optimizer_enabled"] = entry["enabled"]
    for entry in plan["properties"]:
        entry["source"] = source
        entry["optimizer_value"] = entry["value"]


# ======================================================================
# ASSISTED TUNING
# ======================================================================
#
# Three things make this safe enough to run without asking:
#
#   * **It cannot block a render.** `build_plan` reads `_cache_get` and
#     nothing else. The model is reached only from `advise()`, behind its
#     own POST, which the Tune screen fires after it has already painted.
#     A dead endpoint costs a background request, not a page.
#   * **It cannot exceed the host.** Every number goes through a clamp that
#     is written against `specs.recommend_memory`'s own output. The heap
#     clamp refuses to act at all on the `unmeasured` path, because a host
#     whose memory could not be read has no ceiling worth clamping to and a
#     confident guess there is exactly the failure that path exists to
#     prevent.
#   * **It cannot invent vocabulary.** Flags must already be in the plan, or
#     be a bounded re-value of a parameterised flag, or be one of a short
#     literal list. Properties must be in SAFE_PROPERTY_KEYS and inside a
#     per-key range. Everything else is recorded in `rejected` and dropped.

# Cached advice is keyed by instance + fingerprint, so it survives a restart
# and expires the moment the pack changes underneath it. The TTL is a
# backstop for the things the fingerprint cannot see (a newer model, a
# rewritten prompt), not the primary invalidation.
ADVICE_TTL_SECONDS = 14 * 24 * 3600
MAX_CACHED_INSTANCES = 40
# Jar names sent to the model. Enough to recognise a pack's character
# (Create, Immersive Engineering, Distant Horizons all imply different
# heaps) without turning a 400-mod list into a two-minute prompt.
MOD_SAMPLE = 60

_CACHE_PATH = config.state_path("ai-tuning-cache.json")
_cache: dict | None = None


def _load_cache() -> dict:
    """The advice cache, read once and then held.

    Held in memory as well as on disk for the reason every state file in
    this app is (see app/whitelist.py): /data is not reliably writable, and
    advice that cost 20 seconds should at least survive until restart.
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        data = {"schema": 1, "entries": {}}
    _cache = data
    return _cache


def _save_cache() -> bool:
    cache = _load_cache()
    entries = cache["entries"]
    if len(entries) > MAX_CACHED_INSTANCES:
        # Oldest first. These are kilobytes, so the cap is about keeping the
        # file readable by a human, not about disk.
        for key in sorted(entries, key=lambda k: entries[k].get("at") or 0)[
                :len(entries) - MAX_CACHED_INSTANCES]:
            entries.pop(key, None)
    # Still cached for this run if the write fails.
    return config.write_state(_CACHE_PATH, json.dumps(cache, indent=2))


def _fingerprint(plan: dict) -> str:
    """What the advice was computed against.

    Anything that would change the right answer belongs in here: the jars,
    the loader, the game version, the machine, and the ceiling the heap is
    clamped to. Anything that would not -- the port, the MOTD, whether the
    server happens to be running -- deliberately does not, or the cache
    would never hit.
    """
    host = plan.get("host") or {}
    memory = plan.get("memory") or {}
    material = "\n".join([
        str(plan.get("loader") or ""),
        str(plan.get("minecraft") or ""),
        str(plan.get("mod_count") or 0),
        str(host.get("cpu_count") or 0),
        str(host.get("total_ram_gb") or 0),
        str(memory.get("ceiling_gb") or 0),
        str(memory.get("heap_gb") or 0),
        "\n".join(plan.get("mod_names") or []),
    ])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _cache_get(server_id: str, fingerprint: str) -> dict | None:
    entry = _load_cache()["entries"].get(f"{server_id}:{fingerprint}")
    if not isinstance(entry, dict) or not isinstance(entry.get("advice"), dict):
        return None
    if time.time() - float(entry.get("at") or 0) > ADVICE_TTL_SECONDS:
        return None
    return entry


def _cache_put(server_id: str, fingerprint: str, advice: dict) -> bool:
    _load_cache()["entries"][f"{server_id}:{fingerprint}"] = {
        "at": time.time(), "advice": advice,
    }
    return _save_cache()


def forget_advice(server_id: str) -> int:
    """Drop every cached opinion about one instance."""
    entries = _load_cache()["entries"]
    doomed = [k for k in entries if k.split(":", 1)[0] == server_id]
    for key in doomed:
        entries.pop(key, None)
    if doomed:
        _save_cache()
    return len(doomed)


# --- the clamps --------------------------------------------------------

def _clamp_heap(value, memory: dict) -> tuple[float | None, str]:
    """A heap the host can actually give, or nothing at all.

    `specs.recommend_memory` is the authority, and it is the authority in
    both directions: `ceiling_gb` is the hard maximum, and on its
    `unmeasured` path -- where /proc was unreadable and the ceiling is just
    the configured default wearing a ceiling's clothes -- no advice is
    accepted at all. A model told "the ceiling is 6 GB" would happily
    propose 6 GB on a 2 GB box, and that is precisely the dead-server-with-
    no-log failure the unmeasured path was written to avoid.
    """
    if memory.get("unmeasured"):
        return None, ("this host's memory could not be measured, so the heap "
                      "is left to the configured default")
    try:
        wanted = float(value)
    except (TypeError, ValueError):
        return None, "the heap was not a number"
    if wanted != wanted:                       # NaN
        return None, "the heap was not a number"

    ceiling = float(memory.get("ceiling_gb") or 0)
    if ceiling < 1:
        return None, "there is no usable ceiling to clamp against"

    heap = max(1.0, min(wanted, ceiling))
    heap = round(heap * 2) / 2                 # whole or half gigabytes
    if heap > ceiling:                         # rounding must not overshoot
        heap = int(ceiling * 2) / 2
    if heap < 1:
        return None, "the clamped heap fell below 1 GB"
    why = ""
    if wanted > ceiling:
        why = (f"asked for {wanted:g} GB, which is above this host's "
               f"{ceiling:g} GB ceiling, so it was capped")
    return heap, why


# Parameterised flags and the range each number may take. The bounds are not
# decoration: -XX:ParallelGCThreads=64 on a 4-core box is a tick-killing
# misconfiguration that starts cleanly and gets blamed on the pack.
_NUMERIC_FLAGS: dict[str, tuple[float, float]] = {
    "-XX:MaxGCPauseMillis": (50, 500),
    "-XX:G1NewSizePercent": (10, 60),
    "-XX:G1MaxNewSizePercent": (20, 75),
    "-XX:G1ReservePercent": (5, 40),
    "-XX:InitiatingHeapOccupancyPercent": (10, 60),
    "-XX:G1HeapWastePercent": (2, 20),
    "-XX:G1MixedGCCountTarget": (2, 16),
    "-XX:G1MixedGCLiveThresholdPercent": (50, 95),
    "-XX:G1RSetUpdatingPauseTimePercent": (2, 20),
    "-XX:SurvivorRatio": (2, 64),
    "-XX:MaxTenuringThreshold": (1, 15),
    "-XX:SoftRefLRUPolicyMSPerMB": (1, 50000),
    "-Dfml.readTimeout": (30, 600),
}
# Bounded by the host rather than by a constant.
_CPU_FLAGS = ("-XX:ParallelGCThreads", "-XX:ConcGCThreads")
_REGION_SIZES = {"1M", "2M", "4M", "8M", "16M", "32M"}
# Flags the assistant may ADD that the baseline plan does not already offer.
# Short on purpose: everything genuinely useful is already in the plan, and
# an open vocabulary is an open door.
_ADDABLE_FLAGS = {
    "-XX:+UseStringDeduplication",
    "-XX:+PerfDisableSharedMem",
    "-XX:+OptimizeStringConcat",
    "-XX:+UseCompressedOops",
    "-Dlog4j2.formatMsgNoLookups=true",
}


def _flag_head(flag: str) -> str:
    return flag.split("=", 1)[0]


def _clamp_flag(flag: str, host: dict, known: set[str]) -> str | None:
    """A flag string safe to write, or None.

    Everything passes `_safe_flag` first -- the same shell-metacharacter
    gate `apply()` uses. After that a flag is acceptable in exactly three
    ways: it is already in this instance's plan, it is a parameterised flag
    whose number lands inside its range, or it is on the short addable list.
    There is no fourth way, which is the point.
    """
    if not _safe_flag(flag):
        return None
    if flag in known:
        return flag
    head, _, raw = flag.partition("=")

    if head == "-XX:G1HeapRegionSize":
        value = raw.strip().upper()
        if not value.endswith("M"):
            value = f"{value}M"
        return f"{head}={value}" if value in _REGION_SIZES else None

    bounds = _NUMERIC_FLAGS.get(head)
    if head in _CPU_FLAGS:
        cpus = int(host.get("cpu_count") or 2)
        bounds = (1, max(1, cpus)) if head == "-XX:ParallelGCThreads" \
            else (1, max(1, cpus // 2))
    if bounds:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        if number != number or not bounds[0] <= number <= bounds[1]:
            return None
        # Every one of these is an integer flag; the JVM rejects "8.0".
        return f"{head}={int(number)}"

    # Not in the plan and not parameterised: the short literal list is all
    # that is left.
    return flag if flag in _ADDABLE_FLAGS else None


# Per-key ranges for the tunable half of server.properties. A view distance
# of 32 is not a tuning, it is an outage.
_PROPERTY_BOUNDS: dict[str, tuple] = {
    "view-distance": ("int", 3, 16),
    "simulation-distance": ("int", 3, 16),
    "max-tick-time": ("ticktime", 0, 0),
    "sync-chunk-writes": ("bool", 0, 0),
    "network-compression-threshold": ("int", -1, 1024),
    "entity-broadcast-range-percentage": ("int", 10, 100),
}


def _clamp_property(key: str, value: str) -> str | None:
    if key not in SAFE_PROPERTY_KEYS:
        return None
    kind, low, high = _PROPERTY_BOUNDS.get(key, ("str", 0, 0))
    text = str(value).strip()
    if kind == "bool":
        return text.lower() if text.lower() in ("true", "false") else None
    if kind == "ticktime":
        # -1 disables the watchdog, which is the recommendation; any other
        # value has to be a plausible millisecond budget.
        try:
            number = int(float(text))
        except (TypeError, ValueError):
            return None
        if number == -1:
            return "-1"
        return str(number) if 1000 <= number <= 600000 else None
    if kind == "int":
        try:
            number = int(float(text))
        except (TypeError, ValueError):
            return None
        return str(number) if low <= number <= high else None
    return text[:60] or None


def apply_advice(plan: dict, advice: dict, *, at: float | None = None) -> dict:
    """Merge validated advice into a plan, recording who chose what.

    Every mutation here is paired with a `changes` entry, because "silent"
    is a promise about interruption, not about accountability: the Tune
    screen has to be able to say which numbers the assistant moved and why.
    """
    host = plan.get("host") or {}
    changes: list[dict] = []
    rejected: list[dict] = []

    # --- heap ---------------------------------------------------------
    if advice.get("heap_gb") is not None:
        heap, note = _clamp_heap(advice["heap_gb"], plan["memory"])
        baseline = plan["memory"]["heap_gb"]
        if heap is None:
            rejected.append({"what": "heap", "value": advice["heap_gb"],
                             "why": note})
        elif heap != baseline:
            plan["memory"]["heap_gb"] = heap
            plan["memory"]["source"] = "ai"
            plan["memory"]["ai_why"] = advice.get("heap_why") or ""
            changes.append({
                "what": "Heap",
                "from": f"{baseline:g} GB",
                "to": f"{heap:g} GB",
                "why": advice.get("heap_why") or "",
                "note": note,
            })

    # --- flags --------------------------------------------------------
    known_flags = {e["flag"] for e in plan["flags"]}
    by_head = {_flag_head(e["flag"]): e for e in plan["flags"]}
    for entry in advice.get("flags") or []:
        raw = (entry.get("flag") or "").strip()
        clean = _clamp_flag(raw, host, known_flags)
        if not clean:
            rejected.append({"what": "flag", "value": raw,
                             "why": "outside the flags and ranges this "
                                    "optimizer will write"})
            continue
        head = _flag_head(clean)
        target = by_head.get(head)
        on = bool(entry.get("on"))

        if target is None:
            if not on:
                rejected.append({"what": "flag", "value": clean,
                                 "why": "asked to turn off a flag this "
                                        "instance does not have"})
                continue
            plan["flags"].append({
                "flag": clean,
                "label": "Chosen for this pack",
                "group": "ai",
                "enabled": True,
                "recommended": True,
                "applied": clean in (plan.get("current") or {}).get("flags", []),
                "why": entry.get("why") or "",
                "source": "ai",
                "ai_why": entry.get("why") or "",
                "optimizer_flag": None,
                "optimizer_enabled": False,
            })
            by_head[head] = plan["flags"][-1]
            known_flags.add(clean)
            changes.append({"what": "Flag", "from": "not set", "to": clean,
                            "why": entry.get("why") or ""})
            continue

        was_flag, was_on = target["flag"], bool(target["enabled"])
        if clean == was_flag and on == was_on:
            continue                     # agreeing with the baseline is fine
        target["flag"] = clean
        target["enabled"] = on
        target["source"] = "ai"
        target["ai_why"] = entry.get("why") or ""
        changes.append({
            "what": "Flag",
            "from": was_flag if was_on else f"{was_flag} (off)",
            "to": clean if on else f"{clean} (off)",
            "why": entry.get("why") or "",
        })

    # --- server.properties -------------------------------------------
    by_key = {e["key"]: e for e in plan["properties"]}
    for entry in advice.get("properties") or []:
        key = entry.get("key") or ""
        clean = _clamp_property(key, entry.get("value"))
        if clean is None or key not in by_key:
            rejected.append({"what": "property", "value":
                             f"{key}={entry.get('value')}",
                             "why": "not a key and value this optimizer will "
                                    "write"})
            continue
        target = by_key[key]
        if clean == target["value"]:
            continue
        was = target["value"]
        target["value"] = clean
        target["source"] = "ai"
        target["ai_why"] = entry.get("why") or ""
        target["applied"] = target.get("current") == clean
        changes.append({"what": key, "from": was, "to": clean,
                        "why": entry.get("why") or ""})

    # A view distance the simulation distance now exceeds is a nonsense the
    # model produces occasionally and the JVM will not catch.
    _reconcile_distances(by_key, changes)
    changes = _settle(plan, changes)

    plan["advice"] = {
        "state": "ready",
        "enabled": True,
        "summary": advice.get("summary") or "",
        "model": advice.get("model") or "",
        "changes": changes,
        "rejected": rejected,
        "at": at or time.time(),
        "took_ms": advice.get("took_ms"),
        "fingerprint": plan.get("fingerprint"),
    }
    return plan


def _settle(plan: dict, changes: list[dict]) -> list[dict]:
    """Hand back only the numbers that actually moved.

    A clamp can land a value exactly where the optimizer already had it --
    the cross-field distance rule does it routinely. Crediting the
    assistant with a change it did not make would be a small lie told on
    every visit, so provenance is recomputed from the final values rather
    than from who touched what.
    """
    for entry in plan["flags"]:
        if entry.get("flag") == entry.get("optimizer_flag") \
           and bool(entry.get("enabled")) == bool(entry.get("optimizer_enabled")):
            entry["source"] = "optimizer"
            entry.pop("ai_why", None)
    for entry in plan["properties"]:
        if entry.get("value") == entry.get("optimizer_value"):
            entry["source"] = "optimizer"
            entry.pop("ai_why", None)
    if plan["memory"].get("heap_gb") == plan["memory"].get("optimizer_heap_gb"):
        plan["memory"]["source"] = "optimizer"
        plan["memory"].pop("ai_why", None)
    return [c for c in changes if str(c.get("from")) != str(c.get("to"))]


def _reconcile_distances(by_key: dict, changes: list[dict]) -> None:
    """Simulation distance may never exceed view distance.

    A cross-field rule, so neither per-key clamp can catch it, and the JVM
    will not either -- the server starts and simply burns cores simulating
    chunks no client is ever sent.
    """
    view, sim = by_key.get("view-distance"), by_key.get("simulation-distance")
    if not view or not sim:
        return
    try:
        v, s = int(view["value"]), int(sim["value"])
    except (TypeError, ValueError):
        return
    if s <= v:
        return
    sim["value"] = str(v)
    sim["source"] = "ai"
    sim["applied"] = sim.get("current") == sim["value"]
    # Rewrite the change the model asked for rather than adding a second
    # one: the Tune screen should show where the number ended up, not the
    # argument it had with itself on the way there.
    for change in changes:
        if change.get("what") == "simulation-distance":
            change["to"] = str(v)
            change["note"] = (f"asked for {s}, capped at the view distance")
            return
    changes.append({
        "what": "simulation-distance", "from": str(s), "to": str(v),
        "why": "capped at the view distance -- simulating further than the "
               "client is sent is work nobody sees.",
    })


async def advise(server_id: str, *, refresh: bool = False) -> dict:
    """Ask the assistant to sharpen this instance's plan, then clamp it.

    This is the ONLY path that reaches the model, and it is behind its own
    POST so the Tune screen can render first and fold the result in when it
    arrives. Every failure returns a usable plan -- the deterministic one.
    """
    plan = await build_plan(server_id)
    if not ai.tuning_enabled():
        return {"plan": plan, "advice": plan["advice"]}

    fingerprint = plan["fingerprint"]
    if not refresh:
        cached = _cache_get(server_id, fingerprint)
        if cached:
            apply_advice(plan, cached["advice"], at=cached.get("at"))
            plan["advice"]["cached"] = True
            return {"plan": plan, "advice": plan["advice"]}

    profile = {
        "minecraft": plan["minecraft"],
        "loader": plan["loader"],
        "pack": plan["pack"],
        "mod_count": plan["mod_count"],
        "mod_names": plan["mod_names"][:MOD_SAMPLE],
        "host": plan["host"],
        "memory": plan["memory"],
        "flags": plan["flags"],
        "properties": plan["properties"],
    }
    advice = await ai.advise_tuning(profile)
    if not advice.get("ok"):
        plan["advice"] = {
            "state": advice.get("state") or "unavailable",
            "enabled": True,
            "reason": advice.get("reason") or "",
            "changes": [], "rejected": [], "summary": "",
            "took_ms": advice.get("took_ms"),
            "fingerprint": fingerprint,
        }
        return {"plan": plan, "advice": plan["advice"]}

    persisted = _cache_put(server_id, fingerprint, advice)
    apply_advice(plan, advice, at=time.time())
    plan["advice"]["persisted"] = persisted
    plan["advice"]["cached"] = False
    return {"plan": plan, "advice": plan["advice"]}


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
