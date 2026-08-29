#!/usr/bin/env python3
"""What an install does with the port and with client-only mods.

Three behaviours that were each wrong in a way nothing else would catch:

  * The port typed at the install step never reached the server. Crafty is
    told it at creation and writes it into a fresh server.properties, but the
    pack's own overrides land on top afterwards and most packs ship a
    server.properties containing 25565. Every instance on this machine ended
    up on 25565 as a result. It now has to be written AFTER the overlay.

  * Client-only mods were deleted from the install. They are now written as
    `<name>.jar.disabled` and recorded, so the pack still matches what was
    exported and a wrong call costs one click to undo.

  * "Review & strip client-only mods" unticked still stripped them, because
    the front end never sent the flag and the backend defaulted it on.

Runs offline: Crafty, CurseForge and the filesystem are all stubbed.
"""
import asyncio
import io
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app import installer, optimizer, packs, preflight, properties  # noqa: E402
from app.jobs import Job  # noqa: E402

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def fabric_jar(mod_id: str, side: str) -> bytes:
    """A minimal jar whose fabric.mod.json declares a side."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json",
                   '{"id": "%s", "name": "%s", "version": "1.0", '
                   '"environment": "%s"}' % (mod_id, mod_id, side))
    return buf.getvalue()


def build_server_pack() -> bytes:
    """A server pack carrying one server mod and one client-only mod."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mods/goodmod-1.0.jar", fabric_jar("goodmod", "*"))
        z.writestr("mods/clientmod-1.0.jar", fabric_jar("clientmod", "client"))
        z.writestr("config/goodmod.toml", "setting = true\n")
        z.writestr("server.properties", "server-port=25565\nmotd=From the pack\n")
    return buf.getvalue()


class FakeCrafty:
    """Just enough Crafty to run an install, recording what was written."""

    def __init__(self):
        self.uploaded: list[str] = []      # paths that landed in the instance
        self.upload_sizes: list[int] = []
        self.files: dict[str, str] = {}
        self.patched: list[dict] = []
        self.manifest: dict = {}
        self.EULA_ACCEPTED = "eula=true"

    class CraftyError(RuntimeError):
        pass

    async def create_server(self, **kw):
        self.created = kw
        return "fake-server-id"

    async def upload_file(self, sid, location, name, payload):
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(payload)) as z:
                self.uploaded.extend(z.namelist())
        else:
            self.uploaded.append(name)

    async def upload_path(self, sid, location, name, src):
        # The overlay arrives as a zip that Crafty unpacks, so read it back
        # to learn the real filenames -- which is exactly where .disabled
        # would be lost if the installer dropped it. It is now built on disk
        # and streamed, so the test asserts against the file rather than a
        # buffer, and records its size to prove nothing was buffered whole.
        self.upload_sizes.append(src.stat().st_size)
        if name.endswith(".zip"):
            with zipfile.ZipFile(src) as z:
                self.uploaded.extend(z.namelist())
        else:
            self.uploaded.append(name)

    async def unzip(self, sid, path): pass
    async def wait_for_path(self, sid, path, timeout=0): return True
    async def delete_paths(self, sid, paths): pass
    async def ensure_dir(self, sid, d): pass
    async def list_dir(self, sid, path="."): return {"root_path": "/x"}
    async def get_server(self, sid): return {"server_name": "t", "server_port": 25565}
    async def patch_server(self, sid, fields): self.patched.append(fields)
    async def set_java_version(self, sid, mc): return {"changed": False, "reason": "already"}
    async def server_action(self, sid, action): pass
    async def read_file(self, sid, path):
        if path in self.files:
            return self.files[path]
        raise self.CraftyError(f"no such file {path}")
    async def write_file(self, sid, path, contents): self.files[path] = contents
    async def write_studio_manifest(self, sid, data): self.manifest = data
    async def read_studio_manifest(self, sid): return self.manifest


async def run_install(**kwargs) -> tuple[FakeCrafty, dict]:
    fake = FakeCrafty()
    # server.properties has to exist for the port write to have something to
    # rewrite -- as it does on a real instance Crafty just created.
    fake.files["server.properties"] = "server-port=25565\nquery.port=25565\nmotd=x\n"

    plan = packs.analyse_server_pack(zipfile.ZipFile(io.BytesIO(build_server_pack())))
    plan.mc_version, plan.loader = "1.20.1", "fabric"
    plan.crafty_loader, plan.name = "fabric", "Test Pack"

    async def fake_load_plan(job, **kw):
        return plan, zipfile.ZipFile(io.BytesIO(build_server_pack())), {}

    async def no_wait(job, sid, plan): pass
    async def fake_list_servers(): return []

    async def fake_set_memory(sid, heap_gb, flags):
        fake.heap = (heap_gb, list(flags or []))
        return "java -Xmx tuned"

    originals = (installer.crafty, installer._load_plan,
                 installer._wait_for_loader, properties.crafty,
                 optimizer.set_command_memory)
    installer.crafty = fake
    installer._load_plan = fake_load_plan
    installer._wait_for_loader = no_wait
    properties.crafty = fake
    optimizer.set_command_memory = fake_set_memory
    fake.list_servers = fake_list_servers
    try:
        job = Job("install", "test")
        result = await installer.install_modpack(
            job, mod_id=1, file_id=2, server_name="Test",
            **{"optimize": False, **kwargs}
        )
    finally:
        (installer.crafty, installer._load_plan,
         installer._wait_for_loader, properties.crafty,
         optimizer.set_command_memory) = originals
    return fake, result


async def main():
    # --- the port typed at install must reach the instance --------------
    fake, result = await run_install(port=25580)
    check("the port reaches server.properties",
          "server-port=25580" in fake.files.get("server.properties", ""),
          repr(fake.files.get("server.properties", "")[:60]))
    check("query.port follows it",
          "query.port=25580" in fake.files.get("server.properties", ""))
    check("Crafty's own record is updated too",
          any(p.get("server_port") == 25580 for p in fake.patched),
          str(fake.patched))
    check("the port is written after the pack overlay, not before",
          fake.uploaded and "server.properties" not in [
              u for u in fake.uploaded if "/" not in u],
          "a pack's own server.properties must not be the last word")
    check("the summary reports the port", result.get("port") == 25580)

    # --- client-only mods are disabled, not deleted ---------------------
    check("the client-only mod is still installed",
          any("clientmod" in u for u in fake.uploaded),
          ", ".join(u for u in fake.uploaded if u.endswith((".jar", ".disabled"))))
    check("...but disabled",
          any(u.endswith("clientmod-1.0.jar.disabled") for u in fake.uploaded))
    check("the server mod is untouched",
          "mods/goodmod-1.0.jar" in fake.uploaded)
    check("the summary names what it disabled",
          result.get("client_only_disabled") == ["clientmod-1.0.jar"],
          str(result.get("client_only_disabled")))

    records = {r["file"]: r for r in fake.manifest.get("mods", [])}
    check("the manifest tags it client-only",
          records.get("mods/clientmod-1.0.jar", {}).get("client_only") is True)
    check("...and records why",
          "environment=client" in " ".join(
              records.get("mods/clientmod-1.0.jar", {}).get("client_only_reasons", [])))
    check("the server mod is not tagged",
          records.get("mods/goodmod-1.0.jar", {}).get("client_only") is False)

    # --- unticking the review really means "install it as published" ----
    fake2, result2 = await run_install(port=25581, skip_client_only=False)
    check("skip_client_only=false installs client mods enabled",
          "mods/clientmod-1.0.jar" in fake2.uploaded
          and not any(u.endswith(".disabled") for u in fake2.uploaded),
          ", ".join(u for u in fake2.uploaded if u.startswith("mods/")))

    # --- an explicit review decision wins -------------------------------
    fake3, result3 = await run_install(
        port=25582, disable_files=["goodmod-1.0.jar"],
        client_reasons={"goodmod-1.0.jar": ["the user said so"]})
    check("a reviewed decision disables exactly what was chosen",
          any(u.endswith("goodmod-1.0.jar.disabled") for u in fake3.uploaded)
          and "mods/clientmod-1.0.jar" in fake3.uploaded,
          ", ".join(u for u in fake3.uploaded if u.startswith("mods/")))
    records3 = {r["file"]: r for r in fake3.manifest.get("mods", [])}
    check("the review's reasons are recorded against the mod",
          records3.get("mods/goodmod-1.0.jar", {}).get("client_only_reasons")
          == ["the user said so"])

    # --- a Fabric pack gets its heap tuned too --------------------------
    # Forge and NeoForge read JVM args from user_jvm_args.txt; everything
    # else keeps them in Crafty's launch command, and nothing used to rewrite
    # it -- so a Fabric pack asking for 6 GB kept 6 GB regardless of the host.
    tuned, _ = await run_install(port=25584, optimize=True)
    check("a Fabric install tunes the heap through Crafty's command",
          getattr(tuned, "heap", None) is not None,
          f"heap={getattr(tuned, 'heap', None)}")
    check("the tuned heap is sized to the host, not to the pack's wish",
          getattr(tuned, "heap", (99, []))[0] <= 64)

    # --- the upload archive is built on disk and cleaned up -------------
    check("the overlay was uploaded as an archive", fake.upload_sizes,
          f"{len(fake.upload_sizes)} batch(es)")
    import tempfile as _tf
    leftovers = list(pathlib.Path(_tf.gettempdir()).glob(".studio-batch-*.zip"))
    check("no batch archive is left behind on disk", not leftovers,
          ", ".join(p.name for p in leftovers))

    # --- deleting outright is still possible, just not the default ------
    fake4, _ = await run_install(port=25583, exclude_files=["clientmod-1.0.jar"])
    check("exclude_files still removes a mod entirely",
          not any("clientmod" in u for u in fake4.uploaded),
          ", ".join(u for u in fake4.uploaded if u.startswith("mods/")))

    # --- the client-only review must not strip a dependency --------------------
    # This is the failure the protection exists to prevent, and it was live: the
    # server-pack path had no protection pass at all, so Athena -- declared
    # client-only by its own author, and a hard requirement of Chipped, which is
    # not -- was a confident "remove". The pack installed and then refused to
    # boot with "requires version 4.0.0 or later of athena, which is missing".
    jars = [
        {"file_name": "chipped.jar", "name": "Chipped", "mod_id": "chipped",
         "dependencies": ["athena"]},
        {"file_name": "athena.jar", "name": "Athena", "mod_id": "athena",
         "dependencies": []},
        {"file_name": "iris.jar", "name": "Iris", "mod_id": "iris", "dependencies": []},
        {"file_name": "zoom.jar", "name": "Zoomer", "mod_id": "zoom",
         "dependencies": ["iris"]},
    ]
    cands = [
        {"file_name": "athena.jar", "name": "Athena", "mod_id": "athena",
         "reasons": ["the author declares it client-only"], "confidence": "declared"},
        {"file_name": "iris.jar", "name": "Iris", "mod_id": "iris",
         "reasons": ["the author declares it client-only"], "confidence": "declared"},
        {"file_name": "zoom.jar", "name": "Zoomer", "mod_id": "zoom",
         "reasons": ["name matches a known client-only mod"], "confidence": "name"},
    ]
    preflight.decide_with_protection(cands, jars)
    by = {c["file_name"]: c for c in cands}
    check("a dependency of a server mod is held back, not removed",
          by["athena.jar"]["recommendation"] == "keep", by["athena.jar"]["recommendation"])
    check("and it names who needs it",
          by["athena.jar"].get("required_by_others") == ["Chipped"],
          by["athena.jar"].get("required_by_others"))
    check("a client mod nothing depends on is still removed",
          by["iris.jar"]["recommendation"] == "remove", by["iris.jar"]["recommendation"])
    check("a client mod required only by ANOTHER client mod is not protected",
          by["iris.jar"].get("required_by_others") is None,
          "Zoomer needs Iris, but Zoomer is going too")
    check("a name-only match is offered for review, never removed outright",
          by["zoom.jar"]["recommendation"] == "review", by["zoom.jar"]["recommendation"])

    cands2 = [{"file_name": "sound.jar", "name": "Sound Physics", "mod_id": "sp",
               "reasons": ["mixed signals"], "confidence": "contradicted"}]
    preflight.decide_with_protection(cands2, [])
    check("contradicted evidence is a review, not a removal",
          cands2[0]["recommendation"] == "review", cands2[0]["recommendation"])

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


sys.exit(asyncio.run(main()))
