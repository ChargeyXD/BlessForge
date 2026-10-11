"""List every server and flag any whose launcher is missing from disk.

The "empty server, no launcher" state: a record whose startup command still
runs a loader installer, or names a jar that is not in the folder. Finish
setup on the server's Overview retries the loader, so treat a hit here as
something to click rather than something to fix by hand.

    ENGINE=agent python dev/tools/sweep_instances.py
"""
import asyncio, os, pathlib, sys
sys.path.insert(0, os.environ.get("BF_REPO", str(pathlib.Path(__file__).resolve().parents[2])))
from app import engine


async def main():
    servers = await engine.list_servers()
    print(f"{len(servers)} servers\n")
    for s in servers:
        sid = s["server_id"]; name = s.get("server_name", "?")
        cmd = s.get("execution_command") or ""
        exe = s.get("executable") or ""
        try:
            names = set(await engine.list_dir(sid, "."))
        except Exception as e:
            print(f"  {name[:30]:30} ! {e}"); continue
        root = exe.split("/")[0]
        present = (root in names) if root else False
        broken = ("--installServer" in cmd) or not cmd or (exe and not present)
        flag = "BROKEN" if broken else "ok    "
        print(f"  {flag} {name[:32]:32} exe={exe.split('/')[0][:34]:34} "
              f"files={len(names)} node={s.get('node')}")

asyncio.run(main())
