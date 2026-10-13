"""Long-lived HTTP clients: one per target, kept for the agent's lifetime.

A new httpx client per call cost a new connection (and a TLS handshake
towards a panel on https) every time, and building one -- an SSL context --
blocks the event loop for 60-100 ms. Docker, the panel and the download
hosts each get one client, made on first use and closed on shutdown.

Keyed by event loop as well: a client's connections belong to the loop that
opened them, and the tests run more than one loop in a process.
"""
from __future__ import annotations

import asyncio
from typing import Callable

import httpx

_clients: dict[str, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def client(key: str, factory: Callable[[], httpx.AsyncClient]) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    held = _clients.get(key)
    if held and held[0] is loop and not held[1].is_closed:
        return held[1]
    made = factory()
    _clients[key] = (loop, made)
    return made


async def close_all() -> None:
    loop = asyncio.get_running_loop()
    for key, (owner, c) in list(_clients.items()):
        if owner is loop:
            await c.aclose()
        _clients.pop(key, None)


class Lent:
    """A shared client lent for one `async with`, with that use's timeout.
    Leaving the block does not close it."""

    def __init__(self, c: httpx.AsyncClient, timeout: float):
        self._c = c
        self._timeout = timeout

    async def __aenter__(self) -> "Lent":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def _kw(self, kw: dict) -> dict:
        kw.setdefault("timeout", self._timeout)
        return kw

    def get(self, url, **kw):
        return self._c.get(url, **self._kw(kw))

    def post(self, url, **kw):
        return self._c.post(url, **self._kw(kw))

    def put(self, url, **kw):
        return self._c.put(url, **self._kw(kw))

    def delete(self, url, **kw):
        return self._c.delete(url, **self._kw(kw))

    def stream(self, method, url, **kw):
        return self._c.stream(method, url, **self._kw(kw))
