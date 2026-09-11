# mock/ — a stand-in for Crafty Controller

`crafty.py` implements the handful of Crafty v2 endpoints BlessForge
actually calls, with the quirks that matter:

- files are listed by **POST**, not GET
- sizes come back as human strings (`"1.8MB"`), not integers
- there is a stray `root_path` key in every directory listing
- uploads are **chunked**, with a SHA-256 per chunk that the server verifies

Two servers exist in it: `Sakura SMP` (NeoForge 1.21.1, running, 3 players)
and `Paper Survival` (Paper 1.21.4, stopped).

```bash
python -m uvicorn dev.mock.crafty:app --host 127.0.0.1 --port 8999
```

then point BlessForge at it:

```bash
CRAFTY_URL=http://127.0.0.1:8999 CRAFTY_TOKEN=mock \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8724
```

## Why it is faithful rather than permissive

A mock that accepts anything proves nothing. This one **rejects** a request
missing any of the `fileId` / `fileName` / `location` / `fileSize` headers,
and **verifies every chunk hash** before accepting it — so a client that
stopped sending a header, or computed a hash over the wrong bytes, fails
here instead of on someone's real server.

That is not hypothetical. Mod installs were reported failing with

    upload modernfix-neoforge-5.27.24+mc1.21.1.jar failed: {'detail': 'Not Found'}

which looked like a BlessForge bug and was not: this mock simply had no
upload route at all, so FastAPI 404'd it. The client was correct the whole
time. A gap in a test harness reads exactly like a bug in the thing it is
testing, which is the argument for keeping the mock in the repo rather than
rebuilding it from memory each session.

## What it does NOT do

No authentication, no job queue, no real process control — `start` and
`stop` flip a flag. It is enough to drive the interface and to exercise the
client's own protocol handling, and deliberately not more. Anything about
real server lifecycle has to be tested against a real Crafty.
