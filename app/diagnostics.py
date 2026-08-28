"""Instance troubleshooting.

Three layers, cheapest first:

  quick_check  - config/state checks that need only a few file reads.
  analyse_logs - pattern-match latest.log and the newest crash report
                 against the failure modes modded servers actually hit.
  deep_scan    - pull every jar, read its declared dependencies and build
                 the graph, so missing/duplicate mods are found *before*
                 a launch attempt.

Findings carry a machine-readable `fix` so the UI can offer one-click
remediation rather than just describing the problem.
"""
from __future__ import annotations

import asyncio
import posixpath
import re
from collections import defaultdict

from app import crafty, curseforge, jarmeta, modrinth, packs
from app.jobs import Job

LOG_CANDIDATES = ["logs/latest.log", "logs/debug.log", "server.log"]

SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _finding(severity, title, detail, fix=None, evidence=None, category="general"):
    return {
        "severity": severity,
        "category": category,
        "title": title,
        "detail": detail,
        "fix": fix,
        "evidence": (evidence or "")[:1500],
    }


# --- log patterns ------------------------------------------------------
# Each entry: (compiled regex, builder(match, context) -> finding)

_MISSING_DEP_BLOCK = re.compile(
    r"Missing or unsupported mandatory dependencies:(.*?)(?:\n\s*\n|\Z)", re.S
)
_MISSING_DEP_LINE = re.compile(
    r"Mod ID: '([^']+)', Requested by: '([^']+)', Expected range: '([^']*)'"
    r"(?:, Actual version: '([^']*)')?"
)


def _scan_missing_deps(text: str) -> list[dict]:
    findings = []
    for block in _MISSING_DEP_BLOCK.findall(text):
        for mod_id, requested_by, rng, actual in _MISSING_DEP_LINE.findall(block):
            missing = actual in ("", "[MISSING]", "null")
            findings.append(
                _finding(
                    "critical",
                    f"Missing dependency: {mod_id}",
                    f"'{requested_by}' requires '{mod_id}' {rng}"
                    + ("" if missing else f" but found {actual}"),
                    fix={
                        "action": "install_dependency",
                        "mod_id": mod_id,
                        "required_by": requested_by,
                        "version_range": rng,
                    },
                    evidence=block.strip(),
                    category="dependency",
                )
            )
    return findings


_PATTERNS = [
    (
        re.compile(r"You need to agree to the EULA", re.I),
        lambda m, t: _finding(
            "critical", "EULA not accepted",
            "The server refuses to start until eula.txt says eula=true.",
            fix={"action": "accept_eula"}, evidence=m.group(0), category="config",
        ),
    ),
    (
        re.compile(r"FAILED TO BIND TO PORT.*?(\d{2,5})?", re.I),
        lambda m, t: _finding(
            "critical", "Port already in use",
            "Another process is already listening on this server's port. "
            "Change the port in server.properties or stop the conflicting server.",
            fix={"action": "edit_file", "path": "server.properties", "key": "server-port"},
            evidence=m.group(0), category="network",
        ),
    ),
    (
        re.compile(r"java\.lang\.OutOfMemoryError:?\s*(.*)", re.I),
        lambda m, t: _finding(
            "critical", "Out of memory",
            "The JVM ran out of heap. Raise the maximum RAM for this instance "
            "(modpacks commonly need 6-10 GB).",
            fix={"action": "raise_ram"}, evidence=m.group(0), category="resources",
        ),
    ),
    (
        re.compile(
            r"(has been compiled by a more recent version of the Java Runtime"
            r".*?class file version ([\d.]+).*?up to ([\d.]+))", re.I | re.S,
        ),
        lambda m, t: _finding(
            "critical", "Wrong Java version",
            "A mod was built for a newer Java than this server runs. "
            "Modern packs (1.20.5+) need Java 21; 1.17-1.20.4 need Java 17.",
            fix={"action": "change_java"}, evidence=m.group(1), category="runtime",
        ),
    ),
    # NOTE: a plain "ClassNotFoundException: net.minecraft.client..." is NOT
    # evidence of a problem. Mixin logs those as WARNs on every healthy modded
    # server while probing for optional client classes. Only the fatal forms
    # are handled, in _scan_missing_client_class below.
    (
        re.compile(r"Found a duplicate mod ([\w\-.]+)", re.I),
        lambda m, t: _finding(
            "critical", f"Duplicate mod: {m.group(1)}",
            "Two copies of the same mod are present. Delete the older jar.",
            fix={"action": "find_duplicates", "mod_id": m.group(1)},
            evidence=m.group(0), category="mods",
        ),
    ),
    (
        re.compile(r"(zip END header not found|Invalid or corrupt jarfile|"
                   r"error reading .*\.jar)", re.I),
        lambda m, t: _finding(
            "error", "Corrupt mod jar",
            "A jar failed to open, usually a truncated download. Re-download "
            "the affected mod.",
            evidence=m.group(0), category="mods",
        ),
    ),
    (
        re.compile(r"Mixin apply(?:ing)? failed:?\s*(\S+)", re.I),
        lambda m, t: _finding(
            "error", "Mixin failure",
            f"A mixin from '{m.group(1)}' failed to apply -- usually a version "
            "mismatch between two mods, or a mod built for a different loader build.",
            evidence=m.group(0), category="mods",
        ),
    ),
    (
        re.compile(r"requires (neoforge|forge|fabric)\s*([\d.\[\],)( ]*)", re.I),
        lambda m, t: _finding(
            "error", "Loader version mismatch",
            f"A mod requires {m.group(1)} {m.group(2).strip()}, which differs from "
            "the build installed here.",
            evidence=m.group(0), category="loader",
        ),
    ),
    (
        re.compile(r"Incompatible mod set!|Mod file .*? is not compatible", re.I),
        lambda m, t: _finding(
            "critical", "Incompatible mod set",
            "The loader rejected the mod set outright. The full reason is in "
            "the crash report below.",
            evidence=m.group(0), category="mods",
        ),
    ),
    (
        re.compile(r"Unable to detect a valid JRE|no java found", re.I),
        lambda m, t: _finding(
            "critical", "Java not found",
            "Crafty could not locate a Java runtime for this instance.",
            category="runtime",
        ),
    ),
]


# Fabric refuses to load a client class on a server and names both the class
# and the jar it came from, e.g.
#   Cannot load class dev.architectury...ClientTooltipEvent in environment type SERVER
#   ... at knot/com.euphony...BCClientEvents.init [better_client-fabric-1.10.2.jar:?]
_FABRIC_WRONG_ENV = re.compile(
    r"Cannot load class ([\w.$]+) in environment type SERVER", re.I
)
_JAR_IN_TRACE = re.compile(r"\[([\w.\-+]+\.jar):")


def _scan_wrong_environment(text: str) -> list[dict]:
    m = _FABRIC_WRONG_ENV.search(text)
    if not m:
        return []
    # The offending jar is whichever one appears in the trace below the error.
    tail = text[m.end() : m.end() + 4000]
    jars = [
        j for j in _JAR_IN_TRACE.findall(tail)
        if not j.startswith(("fabric-loader", "server-intermediary", "fabric."))
    ]
    culprit = jars[0] if jars else None
    detail = (
        f"A mod tried to load the client-only class {m.group(1)} on a dedicated "
        "server."
    )
    if culprit:
        detail += f" The mod responsible is {culprit}."
    return [
        _finding(
            "critical",
            f"Client-only mod crashes the server: {culprit or 'unknown mod'}",
            detail,
            fix=({"action": "disable_mods", "files": [culprit]} if culprit
                 else {"action": "find_client_only"}),
            evidence=m.group(0),
            category="mods",
        )
    ]


_FATAL_CLIENT_CLASS = re.compile(
    r"(?:Caused by:\s*)?java\.lang\.(?:NoClassDefFoundError|ClassNotFoundException):\s*"
    r"(net[/.]minecraft[/.]client[\w/.$]*)"
)


def _scan_missing_client_class(text: str) -> list[dict]:
    """Catch a client class that actually killed the server.

    Mixin emits `[mixin/]: Error loading class: ...ClassNotFoundException` as
    a WARN constantly during normal startup, so matching the exception alone
    produces a false positive on every healthy modded server. Only lines that
    are fatal -- an ERROR/FATAL level, or a `Caused by:` inside a stack --
    count here.
    """
    for line in text.splitlines():
        m = _FATAL_CLIENT_CLASS.search(line)
        if not m:
            continue
        if "Error loading class" in line or "/WARN]" in line or "[mixin/]" in line:
            continue
        if not ("Caused by" in line or "ERROR" in line or "FATAL" in line
                or line.lstrip().startswith("java.lang.")):
            continue
        return [_finding(
            "critical", "Client-only mod on a server",
            f"A mod fatally required the client-only class {m.group(1)}. It has "
            "to be removed or disabled before the server will start.",
            fix={"action": "find_client_only"}, evidence=line.strip(),
            category="mods",
        )]
    return []


_JAVA_FEATURE_LOG = re.compile(
    r"([\w\-.]+)\s*\([\w\-.]+\) is missing a feature it requires to run"
    r".*?requires\s+javaVersion\s+([\d.]+)\s+or above.*?but\s+([\d.]+)\s+is"
    r"\s+available",
    re.I | re.S,
)


def _scan_java_feature(text: str) -> list[dict]:
    """A mod rejecting the JVM it was handed.

    Forge and NeoForge check the Java version per mod and abort the whole
    load when one refuses, naming that mod. The mod is not the problem: the
    instance is running a Java the pack never supported -- most often because
    the host's default `java` is newer than the loader allows. Reported as a
    runtime fault rather than a mod fault, because "disable Cobblemon" is the
    one fix that cannot work here.
    """
    m = _JAVA_FEATURE_LOG.search(text)
    if not m:
        return []
    mod, need, have = m.group(1), m.group(2), m.group(3)
    major = need.split(".")[0]
    return [_finding(
        "critical", f"Java {have.split('.')[0]} is installed, but the pack needs "
                    f"Java {major}",
        f"{mod} refused to load because it requires Java {need} or above and "
        f"below the next major, and this instance launched with Java {have}. "
        "Every mod that checks will refuse in turn, so this is one fault and "
        "not a mod problem -- pinning the instance to the right Java fixes it.",
        fix={"action": "set_java", "java_major": int(major)},
        evidence=" ".join(m.group(0).split())[:400],
        category="runtime",
    )]


def scan_text(text: str) -> list[dict]:
    """Run every pattern over a log/crash report."""
    findings = (_scan_missing_deps(text) + _scan_wrong_environment(text)
                + _scan_missing_client_class(text) + _scan_java_feature(text))
    for pattern, builder in _PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                findings.append(builder(m, text))
            except Exception:
                continue
    return findings


# --- log retrieval -----------------------------------------------------


async def _read_first_available(server_id: str, paths: list[str]) -> tuple[str, str]:
    for p in paths:
        try:
            return p, await crafty.read_file(server_id, p)
        except crafty.CraftyError:
            continue
    return "", ""


async def _latest_crash_report(server_id: str) -> tuple[str, str]:
    try:
        entries = await crafty.list_dir(server_id, "crash-reports")
    except crafty.CraftyError:
        return "", ""
    files = [
        (name, meta) for name, meta in entries.items()
        if name != "root_path" and isinstance(meta, dict) and not meta.get("dir")
    ]
    if not files:
        return "", ""
    files.sort(key=lambda kv: kv[1].get("modified", ""), reverse=True)
    name = files[0][0]
    try:
        return f"crash-reports/{name}", await crafty.read_file(
            server_id, f"crash-reports/{name}"
        )
    except crafty.CraftyError:
        return "", ""


async def analyse_logs(server_id: str, tail_lines: int = 400) -> dict:
    """Pattern-match the newest log and crash report."""
    log_path, log_text = await _read_first_available(server_id, LOG_CANDIDATES)
    crash_path, crash_text = await _latest_crash_report(server_id)

    findings = []
    if log_text:
        findings += scan_text(log_text)
    if crash_text:
        findings += scan_text(crash_text)

    # De-duplicate on (title, category) while keeping the worst severity.
    merged: dict[tuple, dict] = {}
    for f in findings:
        key = (f["title"], f["category"])
        if key not in merged or SEVERITY_ORDER[f["severity"]] < SEVERITY_ORDER[
            merged[key]["severity"]
        ]:
            merged[key] = f
    result = sorted(merged.values(), key=lambda f: SEVERITY_ORDER[f["severity"]])

    tail = "\n".join(log_text.splitlines()[-tail_lines:]) if log_text else ""
    return {
        "log_path": log_path,
        "crash_path": crash_path,
        "findings": result,
        "log_tail": tail,
        "crash_tail": "\n".join(crash_text.splitlines()[:120]) if crash_text else "",
        "has_logs": bool(log_text or crash_text),
    }


# --- cheap state checks ------------------------------------------------


async def quick_check(server_id: str) -> dict:
    findings: list[dict] = []
    manifest = await crafty.read_studio_manifest(server_id)

    try:
        root = await crafty.list_dir(server_id, ".")
    except crafty.CraftyError as e:
        return {"findings": [_finding("critical", "Instance unreadable", str(e))]}

    names = {k for k in root if k != "root_path"}

    # EULA
    if "eula.txt" in names:
        try:
            eula = await crafty.read_file(server_id, "eula.txt")
            first = eula.split("\n", 1)[0]
            if not re.search(r"^\s*eula\s*=\s*true", eula, re.I | re.M):
                findings.append(_finding(
                    "critical", "EULA not accepted",
                    "eula.txt does not say eula=true, so the server will exit "
                    "immediately on start.",
                    fix={"action": "accept_eula"}, category="config",
                ))
            elif first.lower() not in (
                "eula=true", "eula = true", "eula= true", "eula =true"
            ):
                # Crafty compares the first line to that exact list, so a
                # leading comment or stray whitespace blocks every start
                # while the file still *looks* correct.
                findings.append(_finding(
                    "critical", "Crafty will not start this server (eula.txt format)",
                    "The EULA is accepted, but Crafty matches the first line of "
                    "eula.txt against an exact list. This file's first line is "
                    f"{first!r}, so Crafty silently refuses to launch and writes "
                    "no log at all. Rewriting the file fixes it.",
                    fix={"action": "accept_eula"}, category="config",
                ))
        except crafty.CraftyError:
            pass
    else:
        findings.append(_finding(
            "warning", "No eula.txt",
            "The server has not been started yet, or the file was removed.",
            fix={"action": "accept_eula"}, category="config",
        ))

    # Mods present?
    mod_files: list[str] = []
    if "mods" in names:
        try:
            entries = await crafty.list_dir(server_id, "mods")
            mod_files = [
                n for n, m in entries.items()
                if n != "root_path" and isinstance(m, dict) and not m.get("dir")
                and n.lower().endswith((".jar", ".jar.disabled"))
            ]
            if not mod_files:
                findings.append(_finding(
                    "warning", "No mods installed",
                    "The mods directory is empty.", category="mods",
                ))
        except crafty.CraftyError:
            pass
    else:
        findings.append(_finding(
            "warning", "No mods directory",
            "This instance has no mods/ folder -- expected for vanilla, "
            "a problem for a modpack.", category="mods",
        ))

    # Client-only jars sitting in mods/
    client_only = [f for f in mod_files
                   if not f.endswith(".disabled") and packs.is_client_only_jar(f)]
    if client_only:
        findings.append(_finding(
            "error", f"{len(client_only)} client-only mod(s) present",
            "These mods are client-side and commonly crash a dedicated server: "
            + ", ".join(client_only[:6]) + ("..." if len(client_only) > 6 else ""),
            fix={"action": "disable_mods", "files": client_only},
            category="mods",
        ))

    # RAM vs what the pack asked for
    try:
        if "user_jvm_args.txt" in names:
            args = await crafty.read_file(server_id, "user_jvm_args.txt")
            m = re.search(r"-Xmx(\d+)([GgMm])", args)
            if m:
                gb = int(m.group(1)) / (1024 if m.group(2).lower() == "m" else 1)
                if gb < 4 and mod_files and len(mod_files) > 50:
                    findings.append(_finding(
                        "warning", f"Low memory for {len(mod_files)} mods",
                        f"-Xmx is {gb:g}G. Packs this size usually need 6-10 GB.",
                        fix={"action": "raise_ram"}, category="resources",
                    ))
    except crafty.CraftyError:
        pass

    # Java version. A loader running on too-new a JVM dies before it can
    # write a single log line, so this check has to be state-based.
    try:
        server = await crafty.get_server(server_id)
        command = server.get("execution_command") or ""
        mc = manifest.get("minecraft")
        if not mc:
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", server.get("executable") or "")
            mc = m.group(1) if m else ""
        if mc and command:
            need = crafty.required_java_major(mc)
            m = re.search(r"java-(?:1\.)?(\d+)(?:\.0)?-", command)
            if m:
                actual = int(m.group(1))
                if actual != need:
                    findings.append(_finding(
                        "critical", f"Java {actual} used, but Minecraft {mc} needs {need}",
                        f"This instance launches with Java {actual}. Forge/NeoForge "
                        f"reject unsupported Java versions and exit before writing "
                        f"any log, which looks like 'nothing happens' on start.",
                        fix={"action": "set_java", "minecraft": mc, "java_major": need},
                        category="runtime",
                    ))
            elif re.match(r'^"?java"?\s', command.strip()):
                findings.append(_finding(
                    "warning", "No explicit Java version selected",
                    f"This instance runs whatever 'java' resolves to on the host. "
                    f"Minecraft {mc} needs Java {need}; if the host default differs, "
                    f"the server exits silently at startup.",
                    fix={"action": "set_java", "minecraft": mc, "java_major": need},
                    category="runtime",
                ))
    except Exception:
        pass

    # Mods built for a different Minecraft version.
    #
    # This used to be a pure filename scan, which meant a health check on a
    # perfectly good instance announced that a dozen mods "may target a
    # different Minecraft version" purely because their own version number
    # happened to look like one -- `create-1.20.1-0.5.1` on a 1.21.1 server,
    # or worse, `sodium-0.5.8` where 0.5.8 was never a game version at all.
    # The filename is now only used to pick candidates; nothing is reported
    # until the claim has been checked against what the mod's own publisher
    # says the file supports.
    mc = manifest.get("minecraft")
    if mc and mod_files:
        findings.extend(await _version_mismatch_findings(mc, mod_files, manifest))

    # Install problems recorded at install time
    for problem in (manifest.get("problems") or [])[:20]:
        findings.append(_finding(
            "error", f"Mod failed to install: {problem.get('name')}",
            problem.get("reason", "unknown"),
            fix={"action": "retry_mod",
                 "project_id": problem.get("project_id"),
                 "file_id": problem.get("file_id")},
            category="install",
        ))

    return {
        "findings": sorted(findings, key=lambda f: SEVERITY_ORDER[f["severity"]]),
        "mod_count": len(mod_files),
        "pack": manifest.get("pack"),
        "minecraft": manifest.get("minecraft"),
        "loader": manifest.get("loader"),
    }



# --- version verification ----------------------------------------------


def _versions_in_name(filename: str) -> set[str]:
    """Version-shaped tokens in a jar name, minus the mod's own version.

    A jar is named `<mod>-<mcversion>-<modversion>.jar` about half the time
    and `<mod>-<modversion>.jar` the rest, and nothing in the name says
    which. So this is a candidate filter, never a verdict.
    """
    return set(re.findall(r"(?<![\d.])(1\.\d{1,2}(?:\.\d{1,2})?)(?![\d.])", filename))


def _accepts(mc: str, declared: set[str]) -> bool:
    """Does a set of declared game versions cover this instance?"""
    if not declared:
        return True                      # nothing declared, nothing to contradict
    if mc in declared:
        return True
    base = ".".join(mc.split(".")[:2])    # 1.21.1 -> 1.21
    return any(v == base or v == mc or mc.startswith(v + ".") for v in declared)


async def _declared_game_versions(manifest: dict, files: list[str]
                                  ) -> dict[str, set[str]]:
    """Ask CurseForge/Modrinth what each installed file actually supports.

    Two bulk calls for the whole instance, not one per mod: both APIs take a
    list. Only mods recorded at install time (or matched by Identify) can be
    checked this way, which is exactly the honest limit of this check.
    """
    records = {
        posixpath.basename(r.get("file", "")): r for r in manifest.get("mods", [])
    }
    wanted = {f: records.get(f.replace(".disabled", "")) for f in files}

    cf_ids: dict[int, list[str]] = defaultdict(list)
    mr_ids: dict[str, list[str]] = defaultdict(list)
    for fname, record in wanted.items():
        if not record or not record.get("file_id"):
            continue
        if record.get("source") == "curseforge":
            try:
                cf_ids[int(record["file_id"])].append(fname)
            except (TypeError, ValueError):
                continue
        elif record.get("source") == "modrinth":
            mr_ids[str(record["file_id"])].append(fname)

    declared: dict[str, set[str]] = {}
    if cf_ids:
        try:
            metas = await curseforge.get_files(list(cf_ids))
            for fid, meta in metas.items():
                for fname in cf_ids.get(int(fid), []):
                    declared[fname] = set(meta.get("game_versions") or [])
        except Exception:
            pass
    if mr_ids:
        async def one(version_id: str, names: list[str]) -> None:
            try:
                version = await modrinth.get_version(version_id)
            except Exception:
                return
            if version:
                for fname in names:
                    declared[fname] = set(version.get("game_versions") or [])

        await asyncio.gather(*(one(v, n) for v, n in mr_ids.items()))
    return declared


async def _version_mismatch_findings(mc: str, mod_files: list[str],
                                     manifest: dict) -> list[dict]:
    """Report only the mods whose publisher says they do not support `mc`."""
    candidates = []
    for f in mod_files:
        if f.endswith(".disabled"):
            continue
        found = _versions_in_name(f)
        if found and not _accepts(mc, found):
            candidates.append(f)
    if not candidates:
        return []

    declared = await _declared_game_versions(manifest, candidates[:60])

    confirmed, unverified = [], []
    for f in candidates:
        versions = declared.get(f)
        if versions is None:
            unverified.append(f)
        elif not _accepts(mc, versions):
            confirmed.append({"file": f, "declared": sorted(versions)})

    out = []
    if confirmed:
        out.append(_finding(
            "error",
            f"{len(confirmed)} mod(s) are published for a different Minecraft version",
            "Their own listing says they do not support "
            f"{mc}: "
            + ", ".join(
                f"{c['file']} (built for {', '.join(c['declared'][:3]) or 'unknown'})"
                for c in confirmed[:6]
            )
            + ("..." if len(confirmed) > 6 else "")
            + ". A loader normally refuses the whole mod set over this.",
            fix={"action": "fix_versions",
                 "files": [c["file"] for c in confirmed[:20]],
                 "minecraft": mc},
            category="mods",
        ))
    if unverified:
        # Deliberately `info`, and deliberately worded as a question rather
        # than a claim: all we know is that a filename mentions another
        # number, which is true of a great many correctly installed mods.
        out.append(_finding(
            "info",
            f"{len(unverified)} mod(s) could not be version-checked",
            "These jars have never been matched to a project, so the only "
            "thing to go on is the filename -- which mentions a version other "
            f"than {mc}, but usually because that is the mod's own version "
            "number. Run Identify Unknown on the Mods tab to check them "
            "properly: "
            + ", ".join(unverified[:6]) + ("..." if len(unverified) > 6 else ""),
            fix={"action": "identify_mods"},
            category="mods",
        ))
    return out


# --- dependency graph --------------------------------------------------


async def deep_scan(job: Job, server_id: str, directory: str = "mods") -> dict:
    """Download every enabled jar and verify the dependency graph."""
    try:
        entries = await crafty.list_dir(server_id, directory)
    except crafty.CraftyError as e:
        raise RuntimeError(f"cannot read {directory}: {e}")

    jars = [
        n for n, m in entries.items()
        if n != "root_path" and isinstance(m, dict) and not m.get("dir")
        and n.lower().endswith(".jar")
    ]
    if not jars:
        return {"findings": [], "mods": [], "note": "no enabled jars to scan"}

    job.set_step(f"Reading {len(jars)} mod jars", 5)
    sem = asyncio.Semaphore(4)
    parsed: dict[str, dict] = {}
    done = 0
    lock = asyncio.Lock()

    async def one(name: str):
        nonlocal done
        async with sem:
            try:
                blob = await crafty.download_file(server_id, f"{directory}/{name}")
                parsed[name] = jarmeta.parse(blob, name)
            except Exception as e:
                parsed[name] = {"parse_error": str(e), "dependencies": [],
                                "mod_id": None, "name": name}
            finally:
                async with lock:
                    done += 1
                    if done % 5 == 0 or done == len(jars):
                        job.set_step(
                            f"Reading jars ({done}/{len(jars)})",
                            5 + 80 * done / len(jars),
                        )

    await asyncio.gather(*(one(n) for n in jars))

    job.set_step("Building dependency graph", 90)
    provided: dict[str, str] = {}
    for fname, info in parsed.items():
        if info.get("mod_id"):
            provided[info["mod_id"].lower()] = fname

    findings: list[dict] = []

    # Duplicates
    by_mod_id = defaultdict(list)
    for fname, info in parsed.items():
        if info.get("mod_id"):
            by_mod_id[info["mod_id"].lower()].append(fname)
    for mod_id, files in by_mod_id.items():
        if len(files) > 1:
            findings.append(_finding(
                "critical", f"Duplicate mod: {mod_id}",
                "Loaded twice, which the loader refuses: " + ", ".join(files),
                fix={"action": "disable_mods", "files": sorted(files)[1:]},
                category="mods",
            ))

    # Missing dependencies
    missing: dict[str, list[str]] = defaultdict(list)
    for fname, info in parsed.items():
        for dep in info.get("dependencies", []):
            if not dep.get("mandatory", True):
                continue
            dep_id = (dep.get("id") or "").lower()
            if dep_id and dep_id not in provided:
                missing[dep_id].append(info.get("name") or fname)

    for dep_id, requesters in sorted(missing.items()):
        findings.append(_finding(
            "critical", f"Missing dependency: {dep_id}",
            "Required by " + ", ".join(sorted(set(requesters))[:5])
            + (" and others" if len(set(requesters)) > 5 else "")
            + " but no installed mod provides it.",
            fix={"action": "install_dependency", "mod_id": dep_id,
                 "required_by": sorted(set(requesters))[:5]},
            category="dependency",
        ))

    # Client-side mods, per their own metadata
    for fname, info in parsed.items():
        if info.get("side") == "client":
            findings.append(_finding(
                "error", f"Client-only mod: {info.get('name') or fname}",
                "The jar declares environment=client and will not work on a "
                "dedicated server.",
                fix={"action": "disable_mods", "files": [fname]},
                category="mods",
            ))

    unreadable = [f for f, i in parsed.items() if i.get("parse_error")]
    if unreadable:
        findings.append(_finding(
            "warning", f"{len(unreadable)} jar(s) had no readable metadata",
            ", ".join(unreadable[:8]) + ("..." if len(unreadable) > 8 else ""),
            category="mods",
        ))

    mods_summary = [
        {
            "file": f,
            "mod_id": i.get("mod_id"),
            "name": i.get("name"),
            "version": i.get("version"),
            "loader": i.get("loader"),
            "side": i.get("side"),
            "dependencies": [d.get("id") for d in i.get("dependencies", [])],
            "error": i.get("parse_error"),
        }
        for f, i in sorted(parsed.items())
    ]
    job.log_line(
        f"Scanned {len(jars)} jars: {len(missing)} missing dependencies, "
        f"{sum(1 for m in by_mod_id.values() if len(m) > 1)} duplicates"
    )
    return {
        "scanned": len(jars),
        "findings": sorted(findings, key=lambda f: SEVERITY_ORDER[f["severity"]]),
        "mods": mods_summary,
    }


async def suggest_compatible_versions(
    server_id: str, files: list[str], directory: str = "mods"
) -> dict:
    """For each named jar, find a version that matches this instance.

    Only identified mods can be resolved automatically -- for the rest we say
    so plainly rather than guessing at a project from a filename.
    """
    manifest = await crafty.read_studio_manifest(server_id)
    mc = manifest.get("minecraft") or ""
    loader = manifest.get("loader") or ""
    records = {
        posixpath.basename(r.get("file", "")): r for r in manifest.get("mods", [])
    }

    suggestions, unresolved = [], []
    sem = asyncio.Semaphore(5)

    async def one(filename: str) -> None:
        base = filename.replace(".disabled", "")
        record = records.get(base)
        if not record or not record.get("project_id"):
            unresolved.append({
                "file": filename,
                "why": "this mod has not been identified yet — run Identify first",
            })
            return
        async with sem:
            try:
                if record["source"] == "curseforge":
                    versions = await curseforge.list_files(
                        int(record["project_id"]), game_version=mc or None,
                        mod_loader=loader or None, page_size=20,
                    )
                else:
                    versions = await modrinth.list_versions(
                        str(record["project_id"]), game_version=mc or None,
                        loader=loader or None,
                    )
            except Exception as e:
                unresolved.append({"file": filename, "why": str(e)})
                return
        if not versions:
            unresolved.append({
                "file": filename,
                "why": f"no build of {record.get('name')} exists for "
                       f"{loader or 'this loader'} {mc}",
            })
            return
        best = versions[0]
        suggestions.append({
            "file": filename,
            "name": record.get("name"),
            "source": record["source"],
            "project_id": record["project_id"],
            "current_version": record.get("version"),
            "suggested_version": best.get("display_name")
            or best.get("version_number"),
            "suggested_file_id": best.get("file_id"),
            "logo": record.get("logo"),
        })

    await asyncio.gather(*(one(f) for f in files[:30]))
    return {
        "minecraft": mc,
        "loader": loader,
        "suggestions": sorted(suggestions, key=lambda s: (s["name"] or "").lower()),
        "unresolved": unresolved,
    }


async def suggest_dependency_sources(mod_id: str, *, game_version: str | None,
                                     loader: str | None) -> dict:
    """Find installable candidates for a missing dependency id."""
    out: dict = {"mod_id": mod_id, "curseforge": [], "modrinth": []}
    query = mod_id.replace("_", " ").replace("-", " ")
    try:
        cf = await curseforge.search(
            query=query, class_id=curseforge.CLASS_MODS,
            game_version=game_version, mod_loader=loader, page_size=5,
        )
        out["curseforge"] = cf["items"]
    except Exception as e:
        out["curseforge_error"] = str(e)
    try:
        # Modrinth project ids often match the mod id exactly.
        exact = await modrinth.get_project(mod_id)
        hits = await modrinth.search(
            query=query, game_version=game_version, loader=loader, page_size=5
        )
        items = hits["items"]
        if exact:
            items = [exact] + [i for i in items if i["id"] != exact["id"]]
        out["modrinth"] = items
    except Exception as e:
        out["modrinth_error"] = str(e)
    return out


# --- crash attribution -------------------------------------------------
#
# "The server crashed, which mod did it?" is the question actually asked
# after a failed start, and it is not the same question as "is this instance
# healthy". Answering it means reading the WHOLE crash report rather than a
# tail of it: the section that names the culprit sits in the middle, between
# the exception at the top and the system details at the bottom.
#
# Every attribution below carries the line it came from, because a mod named
# without evidence is worth nothing to whoever has to decide whether to
# disable it.

# Forge and NeoForge each write a per-mod block when a mod fails to load,
# and they do not agree on the heading or the field names:
#
#   Forge      -- MOD examplemod --
#                Details:
#                    Mod File: /path/to/examplemod-1.2.3.jar
#                    Failure message: Mixin apply failed ...
#
#   NeoForge   -- Mod loading issue for: cobblemon --
#                Details:
#                    Mod file: /path/to/Cobblemon-neoforge-1.7.3+1.21.1.jar
#                    Failure message: Cobblemon (cobblemon) is missing a
#                        feature it requires to run
#                            It requires javaVersion 21 or above ...
#
# Both are matched, and the failure message is read as a block rather than a
# line: NeoForge puts the sentence that actually explains the crash on the
# *continuation* line, indented under the first.
_MOD_BLOCK = re.compile(
    r"^--\s*(?:MOD\s+|Mod loading issue for:\s*)([\w\-.]+)\s*--\s*$"
    r"(.*?)(?=^--\s|\Z)",
    re.M | re.S,
)
_MOD_FILE = re.compile(r"Mod file:\s*(?:.*[/\\])?([^\s/\\]+\.jar)", re.I)
_FAILURE_MSG = re.compile(
    r"(?:Failure message|Exception message):[ \t]*(.+(?:\n[ \t]+\S.*)*)", re.I
)

# A mod refusing to run on the JVM it was given. The mod is named, but it is
# not the culprit -- the Java version is -- and disabling the mod would be
# exactly the wrong fix. Caught here so it can be reported as what it is.
_JAVA_FEATURE = re.compile(
    r"requires\s+javaVersion\s+([\d.]+)\s+or above.*?but\s+([\d.]+)\s+is available",
    re.I | re.S,
)

# The header Forge prints above the stack trace on a mod-attributed crash.
_SUSPECTED = re.compile(r"Suspected Mods?:\s*(.+)", re.I)

# Mixin failures name the owning mod directly.
_MIXIN_FAIL = re.compile(
    r"Mixin apply(?:ing)? (?:for mod )?([\w\-.]+)?\s*failed[:\s]+(\S+)", re.I
)
_MIXIN_CONFIG = re.compile(r"([\w\-.]+)\.mixins\.json", re.I)

# Fabric names the jar in brackets at the end of a frame it attributes.
_TRACE_JAR = re.compile(r"\[([\w.\-+ ]+\.jar)(?::[^\]]*)?\]")

# Frames belonging to the platform rather than to any mod. Counting these
# would rank the loader as the top culprit on every single crash.
_PLATFORM_JARS = re.compile(
    r"^(fabric-loader|fabric-api|fabric[-_]|quilt|forge|neoforge|minecraft|"
    r"server-intermediary|client-intermediary|mixin|asm|guava|gson|log4j|"
    r"netty|authlib|brigadier|datafixerupper|sponge-mixin|bootstraplauncher|"
    r"securejarhandler|modlauncher|eventbus|coremods|accesstransformers)",
    re.I,
)

# A package prefix that identifies the owning project of a stack frame.
_FRAME_PACKAGE = re.compile(r"^\s*at\s+([\w.$]+)\.[\w$]+\(")


def _mod_id_to_file(mod_id: str, mod_files: list[str]) -> str | None:
    """Best match between a loader's mod id and a jar on disk."""
    if not mod_id:
        return None
    norm = re.sub(r"[^a-z0-9]", "", mod_id.lower())
    if not norm:
        return None
    best = None
    for f in mod_files:
        stem = re.sub(r"\.jar(\.disabled)?$", "", f, flags=re.I)
        key = re.sub(r"[^a-z0-9]", "", stem.lower())
        if key.startswith(norm) or norm in key:
            # Prefer the shortest match: "create" should not win "createaddition".
            if best is None or len(f) < len(best):
                best = f
    return best


def _resolve_jar(name: str, mod_files: list[str]) -> str | None:
    """Match a jar name from a log against the jars really installed."""
    if not name:
        return None
    base = posixpath.basename(name.strip())
    for f in mod_files:
        if f == base or f == base + ".disabled" or _base(f) == base:
            return f
    key = re.sub(r"[^a-z0-9]", "", _base(base).lower())
    for f in mod_files:
        if re.sub(r"[^a-z0-9]", "", _base(f).lower()) == key:
            return f
    return None


def _base(name: str) -> str:
    return name[: -len(".disabled")] if name.endswith(".disabled") else name


def attribute_crash(text: str, mod_files: list[str]) -> list[dict]:
    """Name the mods a crash log implicates, strongest evidence first.

    Ranked by how directly the log points at each one:
      100  the loader wrote a "-- MOD x --" block naming the jar
       90  the loader listed it under "Suspected Mods"
       80  a mixin owned by the mod failed to apply
       70  a client-only class was loaded from it on a dedicated server
       40  its classes appear in the stack trace
    """
    hits: dict[str, dict] = {}

    def add(file: str | None, score: int, why: str, evidence: str) -> None:
        if not file:
            return
        current = hits.get(file)
        if current is None:
            hits[file] = {"file": file, "score": score, "why": why,
                          "reasons": [why], "evidence": evidence.strip()[:400],
                          "signals": 1}
            return
        # Every reason is kept, not just the strongest: "listed under
        # Suspected Mods" and "loaded a client-only class on a server" are
        # both true of the same jar and only the second one tells the user
        # what to do about it. Two independent pointers at one jar is also
        # worth saying on its own, so the count is tracked.
        if why not in current["reasons"]:
            current["reasons"].append(why)
            current["signals"] += 1
        if score > current["score"]:
            current.update(score=score, why=why, evidence=evidence.strip()[:400])

    # 1. Per-mod failure blocks -- the loader's own verdict.
    for mod_id, block in _MOD_BLOCK.findall(text):
        jar = _MOD_FILE.search(block)
        failure = _FAILURE_MSG.search(block)
        file = (_resolve_jar(jar.group(1), mod_files) if jar
                else _mod_id_to_file(mod_id, mod_files))
        message = " ".join((failure.group(1) if failure else "").split())
        evidence = ((jar.group(0) if jar else "") + "\n"
                    + (failure.group(0) if failure else ""))

        java = _JAVA_FEATURE.search(block)
        if java:
            # Naming the mod here would send someone to disable a mod that is
            # working perfectly. The instance is running the wrong Java.
            add(file, 60,
                f"it refuses to run on Java {java.group(2)} — it needs Java "
                f"{java.group(1)}. The mod is fine; the instance's Java "
                f"version is not, and disabling this mod will only move the "
                f"failure to the next mod that checks.",
                evidence)
            continue
        add(file, 100,
            "the loader reported this mod as failed: "
            + (message[:220] or "no reason given"),
            evidence)

    # 2. "Suspected Mods: Foo (foo), Bar (bar)"
    for line in _SUSPECTED.findall(text):
        for mod_id in re.findall(r"\(([\w\-.]+)\)", line):
            add(_mod_id_to_file(mod_id, mod_files), 90,
                "the loader listed it under Suspected Mods", line)

    # 3. Mixin failures. The config filename carries the owning mod id even
    #    when the message does not.
    for match in _MIXIN_FAIL.finditer(text):
        line = text[match.start(): text.find("\n", match.start())]
        owner = match.group(1)
        config = _MIXIN_CONFIG.search(match.group(2) or "") or _MIXIN_CONFIG.search(line)
        mod_id = owner or (config.group(1) if config else "")
        add(_mod_id_to_file(mod_id, mod_files), 80,
            "one of its mixins failed to apply, which usually means it is "
            "built against a different version of the mod it patches", line)

    # 4. A client-only class loaded on a dedicated server, attributed to the
    #    jar that appears in the frames underneath.
    for match in _FABRIC_WRONG_ENV.finditer(text):
        tail = text[match.end(): match.end() + 4000]
        for jar in _TRACE_JAR.findall(tail):
            if _PLATFORM_JARS.match(jar):
                continue
            add(_resolve_jar(jar, mod_files), 70,
                f"it loaded the client-only class {match.group(1)} on a "
                "dedicated server", match.group(0))
            break

    # 5. Stack-trace attribution, counted rather than taken at face value:
    #    one frame proves nothing, a jar owning most of the trace does.
    counts: dict[str, int] = defaultdict(int)
    for jar in _TRACE_JAR.findall(text):
        if _PLATFORM_JARS.match(jar):
            continue
        resolved = _resolve_jar(jar, mod_files)
        if resolved:
            counts[resolved] += 1
    for file, count in sorted(counts.items(), key=lambda kv: -kv[1])[:5]:
        if count < 2:
            continue
        add(file, 40,
            f"its classes appear in {count} frames of the stack trace",
            f"{file} x{count}")

    ranked = sorted(hits.values(), key=lambda h: (-h["score"], -h["signals"],
                                                  h["file"]))
    for hit in ranked:
        hit["confidence"] = ("high" if hit["score"] >= 90 else
                             "medium" if hit["score"] >= 70 else "low")
    return ranked


async def read_crash_context(server_id: str) -> dict:
    """Fetch the newest crash report and the server log, untrimmed."""
    log_path, log_text = await _read_first_available(server_id, LOG_CANDIDATES)
    crash_path, crash_text = await _latest_crash_report(server_id)
    return {
        "log_path": log_path, "log_text": log_text,
        "crash_path": crash_path, "crash_text": crash_text,
        "has_logs": bool(log_text or crash_text),
    }


async def crash_review(server_id: str) -> dict:
    """Deterministic half of "which mod crashed this server".

    Runs on its own (the Health Check surfaces it) and is also handed to the
    model as evidence, so the two never disagree about what the log said.
    """
    context = await read_crash_context(server_id)
    try:
        entries = await crafty.list_dir(server_id, "mods")
        mod_files = [
            n for n, m in entries.items()
            if n != "root_path" and isinstance(m, dict) and not m.get("dir")
            and n.lower().endswith((".jar", ".jar.disabled"))
        ]
    except crafty.CraftyError:
        mod_files = []

    combined = "\n".join(t for t in (context["crash_text"], context["log_text"]) if t)
    culprits = attribute_crash(combined, mod_files) if combined else []
    findings = scan_text(combined) if combined else []

    enabled_culprits = [c for c in culprits if not c["file"].endswith(".disabled")]
    return {
        "has_logs": context["has_logs"],
        "crash_path": context["crash_path"],
        "log_path": context["log_path"],
        "crashed": bool(context["crash_text"]) or any(
            f["severity"] == "critical" for f in findings
        ),
        "culprits": enabled_culprits,
        "already_disabled": [c for c in culprits if c["file"].endswith(".disabled")],
        "findings": sorted(findings, key=lambda f: SEVERITY_ORDER[f["severity"]]),
        "mod_count": len(mod_files),
    }
