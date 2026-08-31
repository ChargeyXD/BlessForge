import os, pathlib, sys
sys.path.insert(0, os.environ.get("BF_REPO", str(pathlib.Path(__file__).resolve().parents[2])))
from app.installer import _loader_installed, _RUN_SCRIPT
from app.crafty import java_in_command, required_java_major, uptime_seconds

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

# --- which Java a launch command actually invokes ---------------------------
# Crafty stores no java_version: the runtime is baked into execution_command,
# and Crafty rewrites that command whenever a loader install finishes. Every
# path here is one java_candidates() can actually produce, because a shape the
# parser misses is reported to the user as "unknown" rather than as the wrong
# version it really is.
for cmd, major, pinned, why in [
    ("/usr/lib/jvm/java-21-openjdk-amd64/bin/java -Xms2G -jar s.jar nogui",
     21, True, "the standard Debian path"),
    ('"/usr/lib/jvm/java-8-openjdk-amd64/jre/bin/java" -jar s.jar',
     8, True, "quoted, and Java 8 keeps its jre/ level"),
    ("/usr/lib/jvm/java-1.8.0-openjdk-amd64/jre/bin/java -jar s.jar",
     8, True, "the 1.x spelling of the same runtime"),
    ("/usr/lib/jvm/java-17-openjdk/bin/java -jar s.jar",
     17, True, "no -amd64 suffix"),
    ("/opt/java/openjdk-21/bin/java -jar s.jar",
     21, True, "the official image's own layout"),
    ("java -Xmx4G -jar forge.jar nogui",
     None, False, "the container default -- unknown, and must not be guessed"),
    ("/usr/bin/java -jar s.jar",
     None, False, "a path with no version in it"),
    ("", None, False, "a server Crafty has not built a command for yet"),
]:
    got = java_in_command(cmd)
    check(f"java in command: {why}",
          got["major"] == major and got["pinned"] is pinned, got)

check("an unpinned java reports the executable it found, not nothing",
      java_in_command("java -jar s.jar")["path"] == "java")
check("a pack on 1.21.1 needs Java 21", required_java_major("1.21.1") == 21)
check("a pack on 1.20.1 needs Java 17", required_java_major("1.20.1") == 17)
check("1.20.5 is where the line moves", required_java_major("1.20.5") == 21
      and required_java_major("1.20.4") == 17)

# --- uptime, across a timezone boundary -------------------------------------
# The Crafty container runs UTC and this host runs IST. Crafty writes `started`
# in UTC but with no offset in the string, so anything that parses it against
# the local clock reports a server that started an hour ago as starting four
# and a half hours from now.
import datetime as _dt  # noqa: E402

_utc_ago = lambda s: (_dt.datetime.now(_dt.timezone.utc)
                      - _dt.timedelta(seconds=s)).strftime("%Y-%m-%d %H:%M:%S")

up = uptime_seconds({"started": _utc_ago(15120)})
check("uptime reads a UTC `started` as UTC", up is not None and abs(up - 15120) <= 2, up)
check("a stopped server has no uptime", uptime_seconds({"started": False}) is None)
check("a server Crafty has never run has no uptime", uptime_seconds({}) is None)
check("an unparseable timestamp is unknown, not zero",
      uptime_seconds({"started": "yesterday"}) is None)
check("a `started` in the future reads as unknown, not negative",
      uptime_seconds({"started": (_dt.datetime.now(_dt.timezone.utc)
                                  + _dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}) is None,
      "a clock skew must not render as a negative uptime")

# --- a jar built for the wrong loader -------------------------------------
#
# A Forge server ignores a jar whose only descriptor is neoforge.mods.toml:
# no crash, no log line, the mod is simply absent. One roulette-built server
# had eleven of them and Diagnose reported no findings at all.
#
# The rule has to stay permissive. Measured over every jar BlessForge had
# cached, 194 of 1960 (9.9%) ship metadata for more than one loader and 13
# ship none -- so "the jar's one detected loader differs" would condemn a
# tenth of a real pack.
from app import jarmeta as _jm

for _loader, _mc, _markers, _want, _why in [
    ("forge",    "1.21.1", ["neoforge"],        False, "Forge cannot read neoforge.mods.toml"),
    ("forge",    "1.21.1", ["forge"],           True,  "the ordinary case"),
    ("forge",    "1.21.1", ["fabric", "forge"], True,  "multi-loader jar, one marker fits"),
    ("forge",    "1.21.1", [],                  True,  "a library jar declares no loader"),
    ("neoforge", "1.21.1", ["forge"],           False, "descriptor renamed at 1.20.2"),
    ("neoforge", "1.20.1", ["forge"],           True,  "NeoForge 1.20.1 still reads mods.toml"),
    ("neoforge", "1.21.1", ["neoforge"],        True,  "the ordinary case"),
    ("fabric",   "1.21.1", ["forge"],           False, "a different runtime entirely"),
    ("fabric",   "1.21.1", ["fabric"],          True,  "the ordinary case"),
    ("quilt",    "1.21.1", ["fabric"],          True,  "Quilt runs Fabric mods"),
    ("quilt",    "1.21.1", ["forge"],           False, "a different runtime entirely"),
]:
    check(f"{_loader} {_mc} + {_markers or 'no markers'} -> "
          f"{'loads' if _want else 'does not load'}",
          _jm.fits_loader(_loader, _markers, _mc) is _want, _why)

print(f"\n{sum(ok)}/{len(ok)} checks passed")
sys.exit(0 if all(ok) else 1)
