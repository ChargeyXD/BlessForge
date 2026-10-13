"""The heap a person types is the heap the server gets, and the container
follows it.

That is how BlessForge worked before the commercial rework, and how it works
again. It advises, warns and sizes everything around the number, but never
clamps it, shrinks it or refuses it. What had gone wrong:

  * the container's memory limit was set once, at install, and nothing
    resized it: Tune's heap change only rewrote -Xmx, so a bigger heap was
    OOM-killed or flagged "does not fit the container";
  * on an OOM kill, repair shrank the heap to fit the container -- for an
    extra-large pack, a heap too small to load;
  * the install form and Tune capped the heap at what the host had free at
    that moment.

Every path that sets a heap now ends in `resize()`, which sets the container
to the heap plus what the JVM needs around it for this many mods, and
records whether a person chose the heap (`heap_set_by_user`): repair may
lower a heap BlessForge picked itself, never one a person set. `set_heap()`
writes -Xmx/-Xms too, for the paths that have not already.
"""
from __future__ import annotations

import logging

from app import engine, specs

log = logging.getLogger(__name__)

MIN_HEAP_MB = 512
# Above this share of the machine's memory a server gets a plain warning.
WARN_SHARE = 0.85
# Repair's step when the kernel kills a server for memory: a quarter more
# room around the heap, and at least a gigabyte more.
GROW_SHARE = 0.25
GROW_MIN_MB = 1024


async def mod_count(server_id: str) -> int:
    try:
        return len([n for n in await engine.list_dir(server_id, "mods")
                    if n.lower().endswith(".jar")])
    except engine.EngineError:
        return 0


def host_total_mb(host: dict) -> int:
    return int(float(host.get("total_ram_gb") or 0) * 1024)


def check(heap_mb: int, host: dict) -> None:
    """The only refusals: under 512 MB, or more than the machine has."""
    if heap_mb < MIN_HEAP_MB:
        raise ValueError(f"a heap under {MIN_HEAP_MB} MB cannot run a server")
    total = host_total_mb(host)
    if total and heap_mb > total:
        raise ValueError(f"this machine has {total // 1024} GB; a {heap_mb // 1024} GB "
                         "heap cannot fit on it")


def warnings(memory_mb: int, host: dict) -> list[str]:
    """Plain words about a container this size on this machine. Never a cap."""
    total = float(host.get("total_ram_gb") or 0)
    if not total:
        return []
    available = float(host.get("available_ram_gb") or 0)
    others = max(0.0, total - available) if available else 0.0
    need = memory_mb / 1024
    if need > total * WARN_SHARE:
        tail = f"; other apps use about {others:.0f}" if others >= 0.5 else ""
        return [f"This machine has {total:.0f} GB{tail}. A server holding "
                f"{need:.1f} GB leaves little for anything else, and the kernel kills "
                "whichever process it must when memory runs out."]
    if available and need > available:
        return [f"About {available:.1f} GB is free on this machine right now and this "
                f"server will hold up to {need:.1f} GB: stop something else first, or "
                "expect it to be squeezed."]
    return []


def plan(heap_mb: int, mods: int, host: dict) -> dict:
    """What a heap means for the container, for a control to show."""
    memory = specs.container_memory_mb(heap_mb / 1024, mods)
    return {"heap_mb": int(heap_mb), "memory_mb": memory,
            "overhead_mb": memory - int(heap_mb), "mod_count": mods,
            "host_total_mb": host_total_mb(host),
            "warnings": warnings(memory, host)}


async def resize(server_id: str, heap_mb: int, *, by_user: bool,
                 mods: int | None = None, host: dict | None = None) -> dict:
    """The container follows the heap: memory_mb = heap + overhead for its
    mods, written with the heap and who chose it."""
    host = host if host is not None else await specs.host_for(server_id)
    mods = mods if mods is not None else await mod_count(server_id)
    out = plan(heap_mb, mods, host)
    server = await engine.get_server(server_id)
    limits = {**(server.get("limits") or {}), "memory_mb": out["memory_mb"],
              "heap_mb": int(heap_mb), "heap_set_by_user": bool(by_user)}
    await engine.update_server(server_id, {"memory_mb": out["memory_mb"],
                                           "limits": limits})
    return {**out, "by_user": bool(by_user)}


async def set_heap(server_id: str, heap_mb: int, *, by_user: bool) -> dict:
    """Write -Xmx/-Xms (user_jvm_args.txt or the startup command, wherever
    the heap is set now) and resize the container to match."""
    from app import perf
    host = await specs.host_for(server_id)
    check(int(heap_mb), host)
    await perf.apply(server_id, jvm_fixes=[{"set_heap_mb": int(heap_mb)}],
                     yaml_ids=[], by_user=by_user)
    server = await engine.get_server(server_id)
    mods = await mod_count(server_id)
    return {**plan(int(heap_mb), mods, host), "by_user": bool(by_user),
            "memory_mb": int(server.get("memory_mb") or 0)}


def set_by_user(server: dict) -> bool:
    return bool((server.get("limits") or {}).get("heap_set_by_user"))


async def grow_for_oom(server_id: str, heap_mb: int, mods: int) -> tuple[int | None, str]:
    """After an OOM kill: a container one step larger around the same heap.

    The overhead it had was measured the hard way -- the kernel killed it
    there -- so the next one gets a quarter more, and at least a gigabyte
    more, up to the machine's total. Returns the new limit, or None and why
    when the machine has no more to give.
    """
    host = await specs.host_for(server_id)
    server = await engine.get_server(server_id)
    limit = int(server.get("memory_mb") or 0)
    total = host_total_mb(host)
    overhead = max(0, limit - heap_mb)
    grown = heap_mb + max(int(overhead * (1 + GROW_SHARE)), overhead + GROW_MIN_MB)
    grown = max(grown, specs.container_memory_mb(heap_mb / 1024, mods))
    if total:
        grown = min(grown, total)
    if grown <= limit:
        return None, (f"the server was killed for memory in a {limit} MB container and "
                      f"this machine has {total} MB in all, so there is no more to give "
                      "it. Lower the heap (Tune), run fewer mods, or move it to a bigger "
                      "machine")
    limits = {**(server.get("limits") or {}), "memory_mb": grown}
    await engine.update_server(server_id, {"memory_mb": grown, "limits": limits})
    return grown, ""
