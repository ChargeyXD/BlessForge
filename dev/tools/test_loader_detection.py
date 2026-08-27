import os, pathlib, sys
sys.path.insert(0, os.environ.get("BF_REPO", str(pathlib.Path(__file__).resolve().parents[2])))
from app.installer import _loader_installed, _RUN_SCRIPT

ok = []
def check(name, cond, extra=""):
    ok.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if not cond else ''}")

# --- the state machine ---
inst_cmd = "java -Xms2000M -Xmx4000M -jar forge-installer-1.20.1.jar --installServer"
done_cmd = "java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.20.1-47.4.23/unix_args.txt nogui"

check("modded: installer command is NOT 'installed'",
      not _loader_installed("forge-installer", inst_cmd,
                            {"forge-installer-1.20.1.jar"}, "forge-installer-1.20.1.jar"))
check("modded: rewritten command IS 'installed'",
      _loader_installed("forge-installer", done_cmd,
                        {"libraries", "run.sh"}, "libraries/.../forge-server.jar"))
check("modded: empty dir with installer command is NOT installed (the stuck case)",
      not _loader_installed("forge-installer", inst_cmd,
                            {"crafty_managed.txt", "eula.txt", "server.properties"},
                            "forge-installer-1.20.1.jar"))
check("neoforge behaves the same",
      not _loader_installed("neoforge-installer", inst_cmd.replace("forge", "neoforge"),
                            {"eula.txt"}, "neoforge-installer-1.21.11.jar"))
check("fabric: jar present means installed",
      _loader_installed("fabric", "java -Xms1000M -Xmx2000M -jar fabric.jar nogui",
                        {"fabric.jar", "server.properties"}, "fabric.jar"))
check("fabric: no jar means not installed",
      not _loader_installed("fabric", "java -jar fabric.jar nogui",
                            {"server.properties", "eula.txt"}, "fabric.jar"))

# --- the run-script parse, against the real files Crafty's installer wrote ---
forge_sh = 'java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.20.1-47.4.23/unix_args.txt "$@"'
neo_sh = 'exec java @user_jvm_args.txt @libraries/net/neoforged/neoforge/21.11.45/unix_args.txt "$@"'

m = _RUN_SCRIPT.search(forge_sh)
check("forge run.sh parses", m)
if m:
    args, path, ver, txt = m.groups()
    cmd = f"java @{args} @{path}{ver}/{txt} nogui"
    check("forge command matches what Crafty writes",
          cmd == done_cmd, cmd)
    check("forge executable path",
          f"{path}{ver}/forge-{ver}-server.jar"
          == "libraries/net/minecraftforge/forge/1.20.1-47.4.23/forge-1.20.1-47.4.23-server.jar")

m = _RUN_SCRIPT.search(neo_sh)
check("neoforge run.sh parses", m)
if m:
    args, path, ver, txt = m.groups()
    check("neoforge executable path",
          f"{path}{ver}/neoforge-{ver}-server.jar"
          == "libraries/net/neoforged/neoforge/21.11.45/neoforge-21.11.45-server.jar",
          f"{path}{ver}/neoforge-{ver}-server.jar")

print(f"\n{sum(ok)}/{len(ok)} checks passed")
sys.exit(0 if all(ok) else 1)
