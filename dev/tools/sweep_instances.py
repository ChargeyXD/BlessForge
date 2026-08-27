import asyncio, os, pathlib, sys
sys.path.insert(0, os.environ.get("BF_REPO", str(pathlib.Path(__file__).resolve().parents[2])))
from app import crafty

async def main():
    servers = await crafty.list_servers()
    print(f"{len(servers)} instances\n")
    for s in servers:
        sid = s["server_id"]; name = s.get("server_name", "?")
        cmd = s.get("execution_command") or ""
        exe = s.get("executable") or ""
        try:
            entries = await crafty.list_dir(sid, ".")
            names = {k for k in entries if k != "root_path"}
        except Exception as e:
            print(f"  {name[:30]:30} ! {e}"); continue
        root = exe.split("/")[0]
        present = (root in names) if root else False
        broken = ("--installServer" in cmd) or (exe and not present)
        flag = "BROKEN" if broken else "ok    "
        print(f"  {flag} {name[:32]:32} exe={exe.split('/')[0][:34]:34} files={len(names)}")

asyncio.run(main())
