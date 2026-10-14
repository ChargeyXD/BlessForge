"""Fleet changes, pushed to the browser as they happen.

From the server-manager comparison in design/ (item 8). The fleet page used
to find out a server had crashed on its next poll, up to twenty seconds
later. The node agents already push their events to the panel
(app/agent_api.py); this relays the ones a browser cares about -- a server
starting, stopping, crashing, moving port, and the live numbers -- to every
open fleet page, over server-sent events (GET /api/fleet/events).

In-process and best effort: a subscriber that falls behind loses events
rather than holding memory, and is told so (`{"type": "lagged"}`) so the
page can re-read the fleet once instead of trusting a gap.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Callable

QUEUE = 256                     # events a slow subscriber may fall behind by
KEEPALIVE = 20                  # seconds between comments on a quiet stream

_subs: set[asyncio.Queue] = set()


def publish(event: dict[str, Any]) -> None:
    """Hand one event to every open stream. Never blocks, never raises."""
    event = {**event, "at": event.get("at") or time.time()}
    for q in list(_subs):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the backlog and say so: one re-read beats a silent gap.
            while not q.empty():
                q.get_nowait()
            q.put_nowait({"type": "lagged", "at": time.time()})


def subscribers() -> int:
    return len(_subs)


async def stream(allowed: Callable[[str], bool]) -> AsyncIterator[bytes]:
    """Server-sent events for one browser, filtered to the servers it may
    see. `allowed(server_id)` is the caller's scope check."""
    q: asyncio.Queue = asyncio.Queue(QUEUE)
    _subs.add(q)
    try:
        yield b"retry: 5000\n\n"
        yield _frame({"type": "hello", "at": time.time()})
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), KEEPALIVE)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            if ev.get("type") == "stats":
                servers = {sid: v for sid, v in (ev.get("servers") or {}).items()
                           if allowed(sid)}
                if not servers:
                    continue
                ev = {**ev, "servers": servers}
            elif ev.get("server") and not allowed(ev["server"]):
                continue
            yield _frame(ev)
    finally:
        _subs.discard(q)


def _frame(ev: dict[str, Any]) -> bytes:
    return f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev)}\n\n".encode()
