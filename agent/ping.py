"""Server List Ping: ask a running server who is on, the way a client does.

The same query the multiplayer menu sends, answered by the server itself
from its own player list -- so the count is exact whatever the log says.
Join and leave lines are the fast signal, but they are not reliable on
their own: chat and join-message mods rewrite or suppress them, and the
count drifts the first time one is missed. The ping is the correction.

Works on every server since 1.7, modded or not. `sample` is the names the
server chooses to list -- up to a dozen, and none at all where the owner set
hide-online-players -- so it is used for names only when it covers everyone.
"""
from __future__ import annotations

import asyncio
import json
import re

_NAME = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_NO_ID = "00000000-0000-0000-0000-000000000000"


def _varint(n: int) -> bytes:
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for shift in range(0, 35, 7):
        b = (await reader.readexactly(1))[0]
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value
    raise ValueError("varint too long")


async def ping(host: str, port: int, timeout: float = 3.0) -> dict | None:
    """{online, max, sample, version}, or None if the server did not answer."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        host_b = host.encode()
        handshake = (b"\x00" + _varint(47) + _varint(len(host_b)) + host_b
                     + int(port).to_bytes(2, "big") + _varint(1))
        writer.write(_varint(len(handshake)) + handshake + b"\x01\x00")
        await writer.drain()

        async def read() -> dict:
            await _read_varint(reader)              # packet length
            if await _read_varint(reader) != 0:     # packet id: status response
                raise ValueError("not a status response")
            size = await _read_varint(reader)
            return json.loads((await reader.readexactly(size)).decode("utf-8", "replace"))

        status = await asyncio.wait_for(read(), timeout)
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None
    finally:
        writer.close()
    players = status.get("players") or {}
    sample = [p.get("name") for p in players.get("sample") or []
              if isinstance(p, dict) and _NAME.match(str(p.get("name") or ""))
              and p.get("id") != _NO_ID]
    version = status.get("version")
    return {"online": int(players.get("online") or 0), "max": int(players.get("max") or 0),
            "sample": sample,
            "version": version.get("name") if isinstance(version, dict) else None}
