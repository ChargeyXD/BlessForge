"""What the agent tells the panel: status changes, crashes, installs, stats.

Batched and posted to `/api/agent/events` with the node token: every couple
of seconds, or sooner once fifty have queued. If the panel is unreachable
the batch is kept and retried with backoff, and past `MAX_QUEUED` the oldest
events are dropped -- a node must keep running its servers whether or not
the control plane is answering, and an unbounded queue is how it would run
out of memory waiting.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time

import httpx

from agent import clients
from agent.config import AgentConfig

log = logging.getLogger("blessforge.agent.events")

FLUSH_EVERY = 2.0
FLUSH_AT = 50
MAX_QUEUED = 5000


class Events:
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.queue: collections.deque[dict] = collections.deque(maxlen=MAX_QUEUED)
        self.wake = asyncio.Event()
        self.sent = 0
        self.failures = 0
        # Events lost to a full queue while the panel was away: counted,
        # and told to the panel with the next batch that gets through.
        self.dropped = 0
        self.dropped_reported = 0

    def emit(self, kind: str, server: str | None = None, **data) -> None:
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1
        self.queue.append({"type": kind, "server": server, "at": time.time(), **data})
        if len(self.queue) >= FLUSH_AT:
            self.wake.set()

    def panel(self) -> httpx.AsyncClient:
        """The agent's one client towards its panel."""
        return clients.client("panel", lambda: httpx.AsyncClient(
            timeout=30, verify=self.cfg.verify_panel_tls))

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.token_id}.{self.cfg.token}"}

    async def flush(self) -> bool:
        if not self.queue:
            return True
        batch = list(self.queue)[:500]
        lost = self.dropped - self.dropped_reported
        try:
            r = await self.panel().post(
                f"{self.cfg.panel_url.rstrip('/')}/api/agent/events",
                json={"node": self.cfg.node_id,
                      "events": batch + ([{"type": "events_dropped", "server": None,
                                           "at": time.time(), "count": lost}]
                                         if lost else [])},
                headers=self.headers(), timeout=15)
            if r.status_code >= 400:
                raise RuntimeError(f"the panel answered {r.status_code}")
        except Exception as e:      # noqa: BLE001 -- kept and retried
            self.failures += 1
            if self.failures in (1, 10, 100):
                log.warning("could not reach the panel (%s); %d event(s) waiting",
                            e, len(self.queue))
            return False
        for _ in batch:
            self.queue.popleft()
        self.dropped_reported += lost
        self.sent += len(batch)
        self.failures = 0
        return True

    async def run(self) -> None:
        backoff = FLUSH_EVERY
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            ok = await self.flush()
            backoff = FLUSH_EVERY if ok else min(60.0, backoff * 2)
