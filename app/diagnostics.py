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


def scan_text(text: str) -> list[dict]:
    """Run every pattern over a log/crash report."""
    findings = (_scan_missing_deps(text) + _scan_wrong_environment(text)
                + _scan_missing_client_class(text))
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

    # Mods built for a different Minecraft version. The filename is not
    # authoritative, but a jar named "...-1.20.1-..." sitting in a 1.21.1
    # instance is worth surfacing -- that mismatch is a very common cause of
    # a loader refusing the whole mod set.
    mc = manifest.get("minecraft")
    if mc and mod_files:
        mismatched = []
        for f in mod_files:
            if f.endswith(".disabled"):
                continue
            found = set(re.findall(r"1\.\d{1,2}(?:\.\d{1,2})?", f))
            # Ignore mod version numbers that merely look like MC versions by
            # requiring every detected version to disagree with the instance.
            if found and mc not in found:
                # A 1.21 tag on a 1.21.1 server is fine.
                base = ".".join(mc.split(".")[:2])
                if not any(v == base or mc.startswith(v) for v in found):
                    mismatched.append({"file": f, "detected": sorted(found)})
        if mismatched:
            findings.append(_finding(
                "warning",
                f"{len(mismatched)} mod(s) may target a different Minecraft version",
                "These filenames mention a version other than "
                f"{mc}: " + ", ".join(m["file"] for m in mismatched[:6])
                + ("..." if len(mismatched) > 6 else "")
                + ". Filenames can be misleading, so check before acting.",
                fix={"action": "fix_versions",
                     "files": [m["file"] for m in mismatched[:20]],
                     "minecraft": mc},
                category="mods",
            ))

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
