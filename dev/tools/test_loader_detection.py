import os, pathlib, sys
sys.path.insert(0, os.environ.get("BF_REPO", str(pathlib.Path(__file__).resolve().parents[2])))
from app.provision import loader_installed as _loader_installed, _RUN_SCRIPT
from app.engine import java_in_command, java_of, required_java_major, uptime_seconds

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
                            {"eula.txt", "server.properties"},
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

# --- the run-script parse, against the real files the installers wrote ---
forge_sh = 'java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.20.1-47.4.23/unix_args.txt "$@"'
neo_sh = 'exec java @user_jvm_args.txt @libraries/net/neoforged/neoforge/21.11.45/unix_args.txt "$@"'

m = _RUN_SCRIPT.search(forge_sh)
check("forge run.sh parses", m)
if m:
    args, path, ver, txt = m.groups()
    cmd = f"java @{args} @{path}{ver}/{txt} nogui"
    check("forge command is the args-file launch line",
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
# The fallback for a record that names no runtime (an imported server): the
# Java is parsed back out of its launch command. A shape the parser misses is
# reported to the user as "unknown" rather than as the wrong version it is.
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
    ("", None, False, "a server with no command built yet"),
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
# Mojang's own version JSON says javaVersion.majorVersion 25 for every 26.x
# release (checked 2026-10-11 against 26.1 to 26.3). The old table fell
# through to 21 for anything that was not 1.x, which would have started
# every year-based server on a Java it refuses.
check("the year-based releases need Java 25",
      required_java_major("26.1") == 25 and required_java_major("26.3") == 25,
      required_java_major("26.1"))
check("before 1.17 is Java 8", required_java_major("1.16.5") == 8
      and required_java_major("1.12.2") == 8)
check("a record's runtime wins over its command",
      java_of({"java_major": 21, "execution_command": "java -jar s.jar"})["major"] == 21)
check("a record without one falls back to the command",
      java_of({"execution_command":
               "/usr/lib/jvm/java-17-openjdk-amd64/bin/java -jar s.jar"})["major"] == 17)

# --- uptime, across a timezone boundary -------------------------------------
# An imported record can carry `started` as an offset-less UTC string. This
# host runs IST, so anything that parses that against the local clock reports
# a server that started an hour ago as starting four and a half hours from now.
import datetime as _dt  # noqa: E402

_utc_ago = lambda s: (_dt.datetime.now(_dt.timezone.utc)
                      - _dt.timedelta(seconds=s)).strftime("%Y-%m-%d %H:%M:%S")

import time as _time  # noqa: E402
up = uptime_seconds({"started": _time.time() - 600})
check("uptime reads an epoch `started` (what a node sends)",
      up is not None and abs(up - 600) <= 2, up)
check("an epoch in the future is unknown, not negative",
      uptime_seconds({"started": _time.time() + 3600}) is None)
check("a stopped server's None is no uptime", uptime_seconds({"started": None}) is None)

up = uptime_seconds({"started": _utc_ago(15120)})
check("uptime reads a UTC `started` as UTC", up is not None and abs(up - 15120) <= 2, up)
check("a stopped server has no uptime", uptime_seconds({"started": False}) is None)
check("a server that has never run has no uptime", uptime_seconds({}) is None)
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

# Sinytra Connector runs Fabric mods on Forge 1.20.1 and NeoForge. BMC4 ships
# BCLib, Better End and Better Nether that way.
check("with Connector, a Fabric jar loads on Forge",
      _jm.fits_loader("forge", ["fabric"], "1.20.1", connector=True))
check("...and on NeoForge", _jm.fits_loader("neoforge", ["fabric"], "1.21.1", connector=True))
check("...but Connector does not make a NeoForge jar fit Forge",
      not _jm.fits_loader("forge", ["neoforge"], "1.21.1", connector=True))
check("Connector's jar is recognised by its name",
      _jm.is_connector_jar("mods/Connector-1.0.0-beta.46+1.20.1.jar")
      and _jm.is_connector_jar("connector-2.0.0-beta.3+1.21.1-full.jar")
      and not _jm.is_connector_jar("connectivity-1.20.1-5.3.jar"))

from app.installer import PackEntry, connector_pass  # noqa: E402

_records = [
    {"file": "mods/Connector-1.0.0-beta.46+1.20.1.jar", "wrong_loader": False},
    {"file": "mods/bclib-3.0.14.jar", "wrong_loader": True,
     "wrong_loader_reason": "built for fabric, but this server runs forge"},
    {"file": "mods/somemod-neo.jar", "wrong_loader": True,
     "wrong_loader_reason": "built for neoforge, but this server runs forge"},
    {"file": "mods/sodium-fabric.jar", "wrong_loader": True, "client_only": True,
     "wrong_loader_reason": "built for fabric, but this server runs forge"},
]
_results = [PackEntry("Connector-1.0.0-beta.46+1.20.1.jar", blob=b""),
            PackEntry("bclib-3.0.14.jar.disabled", blob=b""),
            PackEntry("somemod-neo.jar.disabled", blob=b""),
            PackEntry("sodium-fabric.jar.disabled", blob=b"")]
_back = connector_pass("forge", _records, _results)
check("a Forge pack with Connector keeps its Fabric jars enabled",
      _back == {"bclib-3.0.14.jar"} and _results[1].target == "bclib-3.0.14.jar"
      and _records[1]["via_connector"] and not _records[1]["wrong_loader"], _back)
check("...but not a NeoForge-only jar, nor a client-only Fabric one",
      _results[2].target.endswith(".disabled") and _results[3].target.endswith(".disabled"))
_records[0]["file"] = "mods/connectivity-1.20.1-5.3.jar"
_records[1].update(wrong_loader=True, wrong_loader_reason="built for fabric, but x")
check("without Connector nothing changes",
      connector_pass("forge", _records, _results) == set())

print(f"\n{sum(ok)}/{len(ok)} checks passed")
sys.exit(0 if all(ok) else 1)
