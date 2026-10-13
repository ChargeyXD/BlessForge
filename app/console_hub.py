"""The console, relayed through the panel: one upstream socket per server.

The browser used to be handed the agent's own websocket address. In the
one-box install that is ws://agent:8443, a name only Docker's network
resolves, on a port nothing publishes; and from an https page (a Cloudflare
tunnel) a ws:// address throws before it is even tried, so the console
showed its first snapshot and nothing after. Now the browser only ever talks
to this panel -- wss:// works behind any proxy or tunnel -- and the panel
follows each server's console once, however many people are watching:

  * `join()` gives a viewer a queue of frames, starting with the backlog;
    the first viewer of a server opens the upstream socket to its agent;
  * every frame from the agent goes to every viewer's queue. A viewer too
    slow to keep up is not allowed to hold the others: its queue is emptied
    and it gets the backlog afresh, and the drop is counted;
  * the upstream stays open `GRACE` seconds after the last viewer leaves (a
    page reload or a tab switch should not reconnect), then closes. If the
    agent goes away, it reconnects with backoff while anybody is watching.

Frames are the agent's own: backlog, lines, stats, error.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import time

log = logging.getLogger(__name__)

GRACE = 30.0
VIEWER_QUEUE = 500
RING = 2000
RECV_TIMEOUT = 5.0


class Feed:
    """One server's console, followed once for every viewer."""

    def __init__(self, server_id: str):
        self.server_id = server_id
        self.lines: collections.deque[str] = collections.deque(maxlen=RING)
        self.seq = 0
        self.state: str | None = None
        self.ready = False
        self.viewers: set[asyncio.Queue] = set()
        self.last_left = time.monotonic()
        self.task: asyncio.Task | None = None
        self.dropped = 0
        self.upstreams = 0          # connections opened, for the tests

    def backlog(self) -> dict:
        return {"type": "backlog", "lines": list(self.lines), "seq": self.seq,
                "state": self.state}

    def _give(self, q: asyncio.Queue, frame: dict) -> None:
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1
            while not q.empty():
                q.get_nowait()
            q.put_nowait(self.backlog())

    def _send(self, frame: dict) -> None:
        for q in list(self.viewers):
            self._give(q, frame)

    def _take(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "backlog":
            self.lines.clear()
            self.lines.extend(frame.get("lines") or [])
            self.seq = int(frame.get("seq") or 0)
            self.state = frame.get("state")
            self.ready = True
            self._send(self.backlog())
        elif kind == "lines":
            fresh = list(frame.get("lines") or [])
            self.lines.extend(fresh)
            self.seq = int(frame.get("seq") or self.seq + len(fresh))
            self._send({"type": "lines", "lines": fresh, "seq": self.seq})
        elif kind == "stats":
            self.state = frame.get("state", self.state)
            self._send(frame)

    def _wanted(self) -> bool:
        return bool(self.viewers) or time.monotonic() - self.last_left < GRACE

    async def run(self, connect) -> None:
        """Follow the agent while anybody is watching (or was, recently)."""
        delay = 1.0
        told = False
        while self._wanted():
            try:
                async with await connect(self.server_id) as ws:
                    self.upstreams += 1
                    delay, told = 1.0, False
                    while self._wanted():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), RECV_TIMEOUT)
                        except asyncio.TimeoutError:
                            continue
                        try:
                            self._take(json.loads(raw))
                        except (ValueError, TypeError):
                            continue
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:      # noqa: BLE001 -- retried while watched
                log.info("console upstream for %s: %s", self.server_id, e)
                if not told:
                    told = True
                    self._send({"type": "error", "message":
                                "The node's console is not answering; retrying."})
            await asyncio.sleep(delay)
            delay = min(15.0, delay * 2)


class Hub:
    def __init__(self, connect):
        # connect(server_id) -> an async context manager over a websocket
        # with recv(); the panel's opens the agent's /v1/servers/{id}/ws.
        self.connect = connect
        self.feeds: dict[str, Feed] = {}
        self.closing = False

    def join(self, server_id: str) -> asyncio.Queue:
        feed = self.feeds.get(server_id)
        if feed is None:
            feed = self.feeds[server_id] = Feed(server_id)
        q: asyncio.Queue = asyncio.Queue(maxsize=VIEWER_QUEUE)
        feed.viewers.add(q)
        if feed.ready:
            feed._give(q, feed.backlog())
        if feed.task is None or feed.task.done():
            feed.task = asyncio.create_task(self._follow(feed))
        return q

    def leave(self, server_id: str, q: asyncio.Queue) -> None:
        feed = self.feeds.get(server_id)
        if feed is None:
            return
        feed.viewers.discard(q)
        if not feed.viewers:
            feed.last_left = time.monotonic()

    async def _follow(self, feed: Feed) -> None:
        try:
            await feed.run(self.connect)
        finally:
            if self.feeds.get(feed.server_id) is feed and not self.closing:
                if feed.viewers:
                    # Somebody joined while it was closing: follow again.
                    feed.task = asyncio.create_task(self._follow(feed))
                else:
                    self.feeds.pop(feed.server_id, None)

    async def close(self) -> None:
        self.closing = True
        for feed in list(self.feeds.values()):
            if feed.task is not None:
                feed.task.cancel()
                try:
                    await feed.task
                except (asyncio.CancelledError, Exception):     # noqa: BLE001
                    pass
        self.feeds.clear()
        self.closing = False


async def _agent_socket(server_id: str):
    """The agent's console websocket for one server, opened by the panel."""
    from websockets.asyncio.client import connect
    from app import engine, nodes
    from app.engine.agent import AgentEngine
    drv = await engine._driver(server_id)
    if not isinstance(drv, AgentEngine):
        raise RuntimeError("this server's engine has no console socket")
    token, _exp = await nodes.sign_browser_token(
        user={"id": "panel"}, server_uuid=server_id, node_id=drv.node_id,
        perms=["console.read"], seconds=600)
    url = drv.socket_url(server_id, token, drv.url) + "&backlog=2000"
    return connect(url, open_timeout=10, ping_interval=20, ping_timeout=20,
                   max_size=16 * 1024 * 1024)


hub = Hub(_agent_socket)
