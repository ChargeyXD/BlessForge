# mock/ — a stand-in for Crafty Controller, kept for one job

BlessForge no longer talks to Crafty: servers go through `app/engine/`, and
the in-memory engine (`app/engine/mock.py`) is what tests and a node-less dev
run use. This mock stays for exactly one reason: `dev/tools/import_from_crafty.py`
reads the old Crafty deployment one last time to move its servers onto
BlessForge's own nodes, and that importer needs something faithful to be
tested against.

`crafty.py` implements the handful of Crafty v2 endpoints the importer reads,
with the quirks that matter:

- files are listed by **POST**, not GET
- sizes come back as human strings (`"1.8MB"`), not integers
- there is a stray `root_path` key in every directory listing
- uploads are **chunked**, with a SHA-256 per chunk that the server verifies

Two servers exist in it: `Sakura SMP` (NeoForge 1.21.1, running, 3 players)
and `Paper Survival` (Paper 1.21.4, stopped).

```bash
python -m uvicorn dev.mock.crafty:app --host 127.0.0.1 --port 8999
```

## Why it is faithful rather than permissive

A mock that accepts anything proves nothing. This one **rejects** a request
missing any of the `fileId` / `fileName` / `location` / `fileSize` headers,
and **verifies every chunk hash** before accepting it. A gap in a test
harness reads exactly like a bug in the thing it is testing, which is the
argument for keeping the mock in the repo rather than rebuilding it from
memory each session.
