#!/usr/bin/env python3
"""Files whose authors turned off third-party distribution are never fetched.

CurseForge gives such a file no `downloadUrl`. BlessForge used to build the
CDN address by hand and download it regardless; that path is gone, and this
suite is what keeps it gone:

  * a file with a null `downloadUrl` is never requested -- not through
    `download`, not through `download_to`, not through the jar cache, and not
    served from a copy an older version left in the cache;
  * the install lands it in `blocked_by_author` instead of `problems`, the
    pre-install review lists it, and the Mods tab keeps listing it until a
    jar with its name is uploaded;
  * the same jar from Modrinth (by SHA-1), or the same project there (by
    slug, only when the names agree), is used instead when it exists;
  * Mod Roulette drops such mods rather than dealing them;
  * and, from the same package: no CurseForge key in any tracked file, and
    the assistant has no default endpoint.

Offline: every catalogue call is stubbed, and the one real httpx client used
sits on a transport that records requests and refuses all of them.
"""
import asyncio
import io
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import (blocked, config, curseforge, engine, installer,  # noqa: E402
                 modrinth, mods, preflight, roulette)
from app.engine.mock import MockEngine  # noqa: E402
from app.jobs import Job  # noqa: E402
from app.packs import PackPlan  # noqa: E402

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def cf_file(fid, name, *, url=True, sha1=None, mod_id=None):
    """A CurseForge file the way the API sends it, slimmed."""
    raw = {
        "id": fid, "modId": mod_id or fid * 10, "displayName": name,
        "fileName": name, "fileLength": 100, "releaseType": 1,
        "downloadUrl": f"https://edge.forgecdn.net/files/x/{name}" if url else None,
        "gameVersions": ["1.20.1", "Fabric"],
        "hashes": [{"algo": 1, "value": sha1}] if sha1 else [],
    }
    return curseforge._slim_file(raw)


def fabric_jar(mod_id):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json",
                   '{"id": "%s", "version": "1.0", "environment": "*"}' % mod_id)
    return buf.getvalue()


class Recorder:
    """An httpx transport that writes down every request and serves none."""

    def __init__(self):
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(500)


async def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    config.CACHE_DIR = tmp / "cache"

    # --- the client itself ------------------------------------------------
    blocked_meta = cf_file(5001, "secret-mod-1.0.jar", url=False, sha1="aa" * 20)
    ok_meta = cf_file(5002, "open-mod-1.0.jar")
    check("a null downloadUrl is marked distribution_blocked",
          blocked_meta["distribution_blocked"] is True
          and ok_meta["distribution_blocked"] is False)
    check("the file's SHA-1 is carried for the Modrinth lookup",
          blocked_meta["sha1"] == "aa" * 20)

    for gone in ("cdn_url", "resolve_download_url", "_download_urls"):
        check(f"curseforge.{gone} no longer exists", not hasattr(curseforge, gone))
    hits = [p.relative_to(ROOT).as_posix() for p in (ROOT / "app").rglob("*.py")
            if "forgecdn" in p.read_text(encoding="utf-8")]
    check("nothing under app/ builds a forgecdn address", not hits, ", ".join(hits))

    rec = Recorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(rec)) as client:
        for label, coro in (
            ("download", curseforge.download(blocked_meta, client)),
            ("download_to", curseforge.download_to(
                blocked_meta, tmp / "x.jar", client)),
            ("cache_jar", curseforge.cache_jar(blocked_meta, client)),
            ("download_cached", curseforge.download_cached(blocked_meta, client)),
        ):
            try:
                await coro
                raised = None
            except Exception as e:      # noqa: BLE001
                raised = e
            check(f"{label} raises DistributionBlocked for a blocked file",
                  isinstance(raised, curseforge.DistributionBlocked),
                  type(raised).__name__)
    check("and not one HTTP request was made for it", rec.urls == [], rec.urls)

    # A copy an older version fetched through the CDN path is still a copy
    # its author did not agree to hand out.
    stale = curseforge._jar_cache_path(blocked_meta)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x" * 100)
    try:
        await curseforge.cache_jar(blocked_meta)
        served = True
    except curseforge.DistributionBlocked:
        served = False
    check("a blocked jar already in the cache is not served from it", not served)

    err = curseforge.DistributionBlocked(blocked_meta, name="Secret Mod",
                                         slug="secret-mod")
    check("the refusal names the mod and links its CurseForge page",
          "Secret Mod" in str(err) and err.url.startswith(
              "https://www.curseforge.com/minecraft/mc-mods/secret-mod/files/5001"))
    check("without a slug the link still reaches the project",
          curseforge.page_url({"mod_id": 77}) == "https://www.curseforge.com/projects/77")

    # --- Modrinth stand-ins ----------------------------------------------
    hash_meta = cf_file(6001, "hashed-1.0.jar", url=False, sha1="bb" * 20, mod_id=61)
    slug_meta = cf_file(6002, "sluggy-2.3.jar", url=False, sha1="cc" * 20, mod_id=62)
    clash_meta = cf_file(6003, "clash-1.0.jar", url=False, sha1="dd" * 20, mod_id=63)
    none_meta = cf_file(6004, "nowhere-1.0.jar", url=False, mod_id=64)
    projects = {
        61: {"id": 61, "name": "Hashed", "slug": "hashed"},
        62: {"id": 62, "name": "Sluggy Mod", "slug": "sluggy"},
        63: {"id": 63, "name": "Clash", "slug": "clash"},
        64: {"id": 64, "name": "Nowhere", "slug": "nowhere"},
    }
    asked: list = []

    async def by_hash(hashes):
        asked.append(("hash", sorted(hashes)))
        return {"bb" * 20: {"file_id": "mrH", "mod_id": "MRH",
                            "file_name": "hashed-1.0.jar",
                            "download_url": "https://cdn.modrinth.com/h.jar",
                            "version_number": "1.0"}}

    async def get_project(slug):
        return {"sluggy": {"name": "Sluggy Mod"},
                "clash": {"name": "Something Else Entirely"}}.get(slug)

    async def list_versions(slug, **kw):
        if slug == "sluggy":
            return [
                {"file_id": "mrS1", "mod_id": "MRS", "file_name": "sluggy-2.4.jar",
                 "download_url": "https://cdn.modrinth.com/s24.jar",
                 "version_number": "2.4", "release_type": "release"},
                {"file_id": "mrS2", "mod_id": "MRS", "file_name": "sluggy-2.3.jar",
                 "download_url": "https://cdn.modrinth.com/s23.jar",
                 "version_number": "2.3", "release_type": "release"},
            ]
        return [{"file_id": "mrC", "file_name": "clash.jar",
                 "download_url": "https://cdn.modrinth.com/c.jar",
                 "version_number": "1.0", "release_type": "release"}]

    saved_mr = (modrinth.versions_from_hashes, modrinth.get_project,
                modrinth.list_versions, modrinth.download)
    modrinth.versions_from_hashes = by_hash
    modrinth.get_project = get_project
    modrinth.list_versions = list_versions
    fetched_mr: list[str] = []

    async def mr_download(meta):
        fetched_mr.append(meta.get("download_url"))
        return fabric_jar(meta.get("file_id") or "x")

    modrinth.download = mr_download
    try:
        found, still = await blocked.substitutes(
            [hash_meta, slug_meta, clash_meta, none_meta, ok_meta], projects,
            mc_version="1.20.1", loader="fabric")
        check("the identical jar is found on Modrinth by SHA-1",
              found.get(6001, {}).get("substitute_via") == "hash")
        check("one bulk hash call covers every blocked file",
              len([a for a in asked if a[0] == "hash"]) == 1)
        check("a slug match picks the build with the same version number",
              found.get(6002, {}).get("file_id") == "mrS2"
              and found[6002]["substitute_via"] == "slug")
        check("a slug whose project name disagrees is not trusted",
              6003 not in found)
        check("files that are not blocked are left alone", 5002 not in found)
        check("what is left over is listed with a CurseForge link",
              sorted(b["file_id"] for b in still) == [6003, 6004]
              and all(b["url"].startswith("https://www.curseforge.com/")
                      for b in still))

        # --- the install ---------------------------------------------------
        plan = PackPlan(name="T", mc_version="1.20.1", loader="fabric",
                        source="manifest", manifest_files=[
                            {"projectID": 50020, "fileID": 5002, "required": True},
                            {"projectID": 61, "fileID": 6001, "required": True},
                            {"projectID": 64, "fileID": 6004, "required": False},
                        ])
        metas = {5002: ok_meta, 6001: hash_meta, 6004: none_meta}
        cf_fetched: list[str] = []

        async def get_files(ids):
            return {i: metas[i] for i in ids}

        async def get_mods(ids):
            return {i: projects.get(i, {"id": i, "name": f"mod {i}"}) for i in ids}

        async def fake_download(meta, client=None):
            curseforge.download_url(meta)       # the real gate
            cf_fetched.append(meta["file_name"])
            return fabric_jar("open")

        saved_cf = (curseforge.get_files, curseforge.get_mods, curseforge.download)
        curseforge.get_files, curseforge.get_mods = get_files, get_mods
        curseforge.download = fake_download
        try:
            job = Job("install", "t")
            (entries, records, problems, matched, disabled,
             waiting) = await installer._download_manifest_mods(
                job, plan, skip_client_only=False)

            # --- the review ------------------------------------------------
            cf_fetched_review_start = len(cf_fetched)
            review = await preflight.analyse_manifest_pack(Job("pre", "t"), plan)
            review_fetched = cf_fetched[cf_fetched_review_start:]
        finally:
            curseforge.get_files, curseforge.get_mods, curseforge.download = saved_cf

        targets = sorted(e.target for e in entries)
        check("the open file is downloaded from CurseForge",
              "open-mod-1.0.jar" in cf_fetched)
        check("the blocked files are never requested from CurseForge",
              not {"hashed-1.0.jar", "nowhere-1.0.jar"} & set(cf_fetched),
              cf_fetched)
        check("the hash stand-in is fetched from Modrinth instead",
              "https://cdn.modrinth.com/h.jar" in fetched_mr)
        check("so the server gets the open jar and the stand-in, nothing else",
              targets == ["hashed-1.0.jar", "open-mod-1.0.jar"], targets)
        sub_rec = next((r for r in records if r["file"] == "mods/hashed-1.0.jar"), {})
        check("the stand-in's record says where it really came from",
              sub_rec.get("source") == "modrinth"
              and sub_rec.get("substituted_for", {}).get("file_id") == 6001
              and sub_rec["substituted_for"]["via"] == "hash")
        check("the unfetchable file is in blocked_by_author, not problems",
              [w["file_id"] for w in waiting] == [6004]
              and not any(p.get("file_id") == 6004 for p in problems))
        check("and keeps whether the pack marked it required",
              waiting and waiting[0].get("required") is False)
        check("the review lists it too",
              [b["file_id"] for b in review.get("blocked_by_author", [])] == [6004])
        check("and reads no blocked jar from CurseForge while reviewing",
              not {"hashed-1.0.jar", "nowhere-1.0.jar"} & set(review_fetched),
              review_fetched)
    finally:
        (modrinth.versions_from_hashes, modrinth.get_project,
         modrinth.list_versions, modrinth.download) = saved_mr

    # --- the Mods tab -----------------------------------------------------
    eng = MockEngine(seed=False)
    engine.configure(drivers={"mock": eng})
    sid = await engine.create_server(name="T", port=25570, memory_mb=2048,
                                     mc_version="1.20.1", loader="fabric")
    await engine.ensure_dir(sid, "mods")
    await engine.upload_file(sid, "mods", "open-mod-1.0.jar", b"jar")
    await engine.upload_file(sid, "mods", "gone-1.0.jar.disabled", b"jar")
    await engine.write_manifest(sid, {
        "minecraft": "1.20.1", "loader": "fabric", "mods": [],
        "blocked_by_author": [
            {"name": "Nowhere", "file_name": "nowhere-1.0.jar", "file_id": 6004,
             "url": "https://www.curseforge.com/projects/64"},
            {"name": "Gone", "file_name": "gone-1.0.jar", "file_id": 6005,
             "url": "https://www.curseforge.com/projects/65"},
        ]})
    listing = await mods.list_mods(sid, "mods")
    check("the Mods tab lists a blocked jar until it is uploaded",
          [b["file_name"] for b in listing["blocked_by_author"]] == ["nowhere-1.0.jar"])
    check("an uploaded one clears itself, even uploaded disabled",
          not any(b["file_name"] == "gone-1.0.jar"
                  for b in listing["blocked_by_author"]))

    # --- adding a single blocked mod ---------------------------------------
    before = set(await engine.list_dir(sid, "mods"))
    saved = (curseforge.get_file, curseforge.get_mod,
             modrinth.versions_from_hashes)

    async def get_file(pid, fid):
        return none_meta

    async def get_mod(pid):
        return {"id": pid, "name": "Nowhere", "slug": "nowhere"}

    async def no_hits(hashes):
        return {}

    curseforge.get_file, curseforge.get_mod = get_file, get_mod
    modrinth.versions_from_hashes = no_hits
    saved_gp = modrinth.get_project

    async def no_project(slug):
        return None

    modrinth.get_project = no_project
    try:
        await mods.add_mod(sid, source="curseforge", project_id=64, file_id=6004)
        refused = None
    except curseforge.DistributionBlocked as e:
        refused = e
    finally:
        (curseforge.get_file, curseforge.get_mod,
         modrinth.versions_from_hashes) = saved
        modrinth.get_project = saved_gp
    uploads = set(await engine.list_dir(sid, "mods")) - before
    check("adding a blocked mod by hand is refused with a link",
          refused is not None and "curseforge.com/minecraft/mc-mods/nowhere" in str(refused))
    check("and nothing was uploaded", not uploads, sorted(uploads))

    # --- Mod Roulette --------------------------------------------------------
    async def search(**kw):
        if kw.get("index"):
            return {"items": []}
        return {"items": [
            {"id": 901, "name": "Fetchable", "latest_files": [
                {"file_id": 1, "game_versions": ["1.20.1"], "loaders": ["fabric"],
                 "download_url": "https://x/1.jar",
                 "distribution_blocked": False}]},
            {"id": 902, "name": "Withheld", "latest_files": [
                {"file_id": 2, "game_versions": ["1.20.1"], "loaders": ["fabric"],
                 "download_url": None, "distribution_blocked": True}]},
        ]}

    saved = curseforge.search
    curseforge.search = search
    try:
        pool = await roulette._curseforge_pool("1.20.1", "fabric")
    finally:
        curseforge.search = saved
    ids = {m["project_id"] for m in pool}
    check("a project that blocks distribution never enters the pool",
          901 in ids and 902 not in ids, sorted(ids))

    async def list_files(pid, **kw):
        return [cf_file(7000 + int(pid) % 10, f"m{pid}.jar", url=(pid != 902))]

    saved = curseforge.list_files
    curseforge.list_files = list_files
    try:
        c = roulette.merge_constraints({"minecraft": "1.20.1", "loader": "Fabric"})
        hand = [{"source": "curseforge", "project_id": pid, "name": f"m{pid}"}
                for pid in (901, 902)]
        resolved, dropped = await roulette.resolve_hand(hand, c)
    finally:
        curseforge.list_files = saved
    check("a cached pool's blocked mod is dropped when the hand is pinned",
          [m["project_id"] for m in resolved] == [901]
          and dropped and dropped[0]["kind"] == "distribution-blocked")

    # --- no key in the repo, no default AI endpoint ------------------------
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    bcrypt = re.compile(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{20,}")
    b64key = re.compile(r"JDJ[hiy]JDEw[A-Za-z0-9+/=]{20,}")   # base64 of "$2a$10"
    leaks = []
    for rel in tracked:
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if bcrypt.search(text) or b64key.search(text):
            leaks.append(rel)
    check("no CurseForge key value in any tracked file", not leaks, ", ".join(leaks))
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    check("docker-compose.yml leaves CURSEFORGE_API_KEY_B64 empty",
          re.search(r'CURSEFORGE_API_KEY_B64:\s*""', compose) is not None)

    env = {k: v for k, v in os.environ.items() if not k.startswith("OLLAMA")}
    probe = subprocess.run(
        [sys.executable, "-c",
         "import asyncio, sys; sys.path.insert(0, '.');"
         "from app import ai;"
         "s = asyncio.run(ai.status());"
         "print(repr(ai.OLLAMA_URL), s.get('configured'), s.get('available'),"
         " [e['label'] for e in ai.endpoints()])"],
        cwd=ROOT, env={**env, "STATE_DIR": str(tmp / "state")},
        capture_output=True, text=True, timeout=60)
    out = probe.stdout.strip()
    check("OLLAMA_URL defaults to empty", out.startswith("''"), out or probe.stderr[-300:])
    check("and the assistant reports itself off without calling anyone",
          "False False" in out, out)
    check("the only endpoint left to choose is a local one", "['local']" in out, out)
    check("nobody else's endpoint is named anywhere in app/",
          not any("shadowco" in p.read_text(encoding="utf-8")
                  for p in (ROOT / "app").rglob("*.py")))

    # --- bringing a blocked jar by hand --------------------------------------------------------
    import hashlib
    from app import mods as modmgr

    def jar(mod_id):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("fabric.mod.json", '{"schemaVersion":1,"id":"%s","version":"1"}' % mod_id)
        return buf.getvalue()

    wanted, other = jar("waystones"), jar("somethingelse")
    hand = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": hand})
    sid = await engine.create_server(name="Blocked", port=25705, memory_mb=4096,
                                     mc_version="1.20.1", loader="fabric")
    await engine.ensure_dir(sid, "mods")
    await engine.write_manifest(sid, {"schema": 1, "mods": [], "blocked_by_author": [
        {"name": "Waystones", "file_name": "waystones-fabric-1.20-14.1.3.jar",
         "project_id": 245755, "file_id": 5005, "required": True,
         "fingerprint": curseforge.fingerprint(wanted),
         "sha1": hashlib.sha1(wanted).hexdigest()},
        {"name": "Balm", "file_name": "balm-fabric-1.20.1-7.2.2.jar",
         "project_id": 531761, "file_id": 5006, "required": True,
         "fingerprint": 1, "sha1": "00"}]})
    result = await modmgr.accept_blocked(sid, "waystones-fabric-1.20-14.1.3 (1).jar", wanted)
    files = set(await engine.list_dir(sid, "mods"))
    check("a hand-downloaded jar is recognised by its content, whatever the browser "
          "named it, and filed under the name the pack expects",
          result["matched"] == "waystones-fabric-1.20-14.1.3.jar"
          and "waystones-fabric-1.20-14.1.3.jar" in files, (result, files))
    listing = await modmgr.list_mods(sid, "mods")
    check("...which takes it off the waiting list",
          [b["name"] for b in listing["blocked_by_author"]] == ["Balm"],
          listing["blocked_by_author"])
    try:
        await modmgr.accept_blocked(sid, "balm-fabric-1.20.1-7.2.2.jar", other)
        refused = False
    except modmgr.NotBlockedFile as e:
        refused = "not one of the files" in str(e)
    check("a jar with the right name and the wrong content is refused", refused)

    # The install's first boot waits for the jars a person brings, and runs
    # by itself when the last one lands.
    balm = jar("balm")
    manifest = await engine.read_manifest(sid)
    manifest["blocked_by_author"][1].update(fingerprint=curseforge.fingerprint(balm),
                                            sha1=hashlib.sha1(balm).hexdigest())
    manifest["boot_pending"] = True
    await engine.write_manifest(sid, manifest)
    again = await modmgr.accept_blocked(sid, "waystones (2).jar", wanted)
    check("bringing one again while another is still missing does not start the boot",
          again["waiting"] == 1 and not again.get("resume_boot"), again)
    last = await modmgr.accept_blocked(sid, "balm.jar", balm)
    check("...the last one does, once",
          last["waiting"] == 0 and last.get("resume_boot")
          and not (await engine.read_manifest(sid)).get("boot_pending"), last)
    source = (ROOT / "app" / "installer.py").read_text(encoding="utf-8")
    check("an install that leaves required mods to bring holds its first boot for them",
          "if boot_check and must_bring:" in source and 'manifest["boot_pending"] = True' in source)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
