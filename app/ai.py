"""AI assistant for troubleshooting, via an Ollama-compatible HTTP API.

The model runs on a shared Ollama server (`OLLAMA_URL`), not on this host.
That is worth knowing when reading the tuning below: prompt evaluation is no
longer the dominant cost, so the evidence budget is generous rather than
miserly, and the context window is sized from the prompt instead of being
kept deliberately tiny.

Deliberate constraints, because a small model is a useful pattern-matcher and
a poor decision-maker:

  * It never executes anything on its own. It returns a *proposal*; actions
    are applied only after the user approves them -- either one by one in the
    UI, or in a batch by explicitly asking for an automatic fix, which is
    still confined to the reversible half of the vocabulary.
  * Its output is validated against a fixed action vocabulary. Anything it
    invents is dropped rather than guessed at.
  * Every action is classified `safe` or `major`. Major actions -- deleting
    mods, replacing versions, wiping config -- are flagged so the UI can
    demand a second, explicit confirmation.
  * The deterministic checks in `diagnostics` run first and are passed in as
    evidence. The model explains and prioritises; it is not the detector.

If Ollama is unreachable the rest of the app is unaffected -- the AI panel
simply reports that it is unavailable.
"""
from __future__ import annotations

import json
import os
import re

import httpx

OLLAMA_URL = (os.environ.get("OLLAMA_URL", "https://ai.shadowco.xyz")).rstrip("/")
# Qwen3-4B-Instruct: small (2.5 GB, Q4_K_M) but a reliable JSON emitter with
# a 256k context, which is what a full crash report needs.
AI_MODEL = os.environ.get("AI_MODEL", "qwen3:4b-instruct")
# Optional bearer token, for an endpoint behind an authenticating proxy.
OLLAMA_API_KEY = (os.environ.get("OLLAMA_API_KEY", "") or "").strip()
AI_ENABLED = (os.environ.get("AI_ENABLED", "true").lower()
              in ("1", "true", "yes", "on"))
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "300"))
# How long Ollama should keep the model resident. Loading a 2 GB model off
# disk costs ~25 s on a spinning-rust host; holding it in RAM makes a second
# question feel instant instead. Set to "0" to free the memory immediately.
AI_KEEP_ALIVE = os.environ.get("AI_KEEP_ALIVE", "30m")
# Evidence budget, and the reason it is what it is.
#
# The endpoint evaluates prompts at roughly 90 tokens/second (measured), and
# it sits behind a proxy that gives the origin 120 seconds to produce the
# first byte. Streaming does not help: nothing is emitted until the whole
# prompt has been read, so time-to-first-token IS prompt evaluation. That
# puts a hard ceiling near 8,000 tokens on anything we send -- past it the
# request dies with a proxy timeout no retry can fix.
#
# So the evidence is selected rather than truncated. Sending a 369-mod
# inventory and a 67k crash report costs two minutes and buys nothing; the
# regex pass in `diagnostics` has already found the candidates, and what the
# model is for is explaining them and catching what the patterns missed.
CRASH_PROMPT_BUDGET = int(os.environ.get("AI_CRASH_BUDGET_CHARS", "13000"))
MAX_LOG_CHARS = int(os.environ.get("AI_MAX_LOG_CHARS", "4500"))
MAX_CRASH_CHARS = int(os.environ.get("AI_MAX_CRASH_CHARS", "7000"))
MAX_MODS_LISTED = int(os.environ.get("AI_MAX_MODS_LISTED", "40"))
MAX_FINDINGS = 10
# Actions safe to apply without a second opinion: every one is reversible.
# Deleting a jar is not, so it never lands here.
AUTO_APPLY_ACTIONS = {"accept_eula", "set_java", "set_ram", "disable_mods"}

# The only actions the model may propose. Anything else is discarded.
# "major" actions change or remove content and need explicit confirmation.
ACTION_SPEC: dict[str, dict] = {
    "accept_eula": {"major": False, "args": [],
                    "desc": "Rewrite eula.txt in the exact form Crafty requires"},
    "set_java": {"major": False, "args": ["minecraft"],
                 "desc": "Point the instance at the correct Java version"},
    "set_ram": {"major": False, "args": ["max_gb"],
                "desc": "Change the maximum heap size"},
    "disable_mods": {"major": True, "args": ["files"],
                     "desc": "Disable one or more mods (reversible)"},
    "delete_mods": {"major": True, "args": ["files"],
                    "desc": "Delete mod files permanently"},
    "install_mod": {"major": True, "args": ["query"],
                    "desc": "Search for and install a missing mod or dependency"},
    "switch_mod_version": {"major": True, "args": ["file"],
                           "desc": "Replace a mod with a different version"},
    "edit_property": {"major": True, "args": ["key", "value"],
                      "desc": "Change a server.properties value"},
    "inspect_config": {"major": False, "args": ["path"],
                       "desc": "Open a config file for the user to review"},
}

SYSTEM_PROMPT = """\
You are a Minecraft server troubleshooting assistant. You analyse evidence \
from a modded server that will not start (or misbehaves) and produce a short, \
concrete plan.

Rules:
- Base every conclusion on the supplied evidence. Do not invent mod names, \
versions or errors.
- Prefer the least destructive fix that resolves the problem.
- If the evidence is insufficient, say so and propose what to inspect. That is \
a valid answer.
- Reply with JSON only, no prose outside it, no markdown fences.

JSON shape:
{
  "summary": "one or two sentences naming the most likely root cause",
  "confidence": "high" | "medium" | "low",
  "root_cause": "short label",
  "actions": [
    {
      "action": "<one of the allowed action names>",
      "why": "why this specific action helps, referencing the evidence",
      "args": { ... }
    }
  ],
  "notes": "anything the user should check manually, or empty string"
}

Allowed action names and their args:
%s

Use exact file names from the evidence in `files`. Keep `actions` to at most \
four entries, ordered most important first. If nothing is wrong, return an \
empty actions list and say so in summary.
"""


CRASH_SYSTEM_PROMPT = """\
You are a Minecraft server crash analyst. You are given the COMPLETE crash \
report and server log from a modded server, plus the exact list of jar files \
installed on it. Your job is to name the mods responsible.

Rules:
- Name mods only by the exact jar filenames from the "Installed jars" list. \
Copy each filename character for character. Never abbreviate, split or \
reconstruct a filename.
- A mod appearing in a stack trace is not automatically the cause: loaders, \
mixin and the game itself appear in almost every trace. Prefer the mod named \
in the crash report's own "Mod File"/"Failure message" section, the mod whose \
mixin failed to apply, or the mod whose class sits deepest in the trace.
- If several mods are implicated, order them by how confident you are.
- If the log does not identify a culprit, say so and return an empty culprits \
list. That is a valid, useful answer.
- Reply with JSON only, no prose outside it, no markdown fences.

JSON shape:
{
  "summary": "one or two sentences: what killed the server",
  "confidence": "high" | "medium" | "low",
  "root_cause": "short label",
  "crash_type": "short label, e.g. mixin conflict / missing dependency / \
client-only mod / out of memory / corrupt jar",
  "culprits": [
    {
      "file": "<exact jar filename from the installed list>",
      "why": "the line or section of the log that implicates it",
      "confidence": "high" | "medium" | "low"
    }
  ],
  "actions": [ {"action": "<allowed action>", "why": "...", "args": {...}} ],
  "notes": "anything to check by hand, or empty string"
}

Allowed action names and their args:
%s

Keep `culprits` to at most six entries and `actions` to at most four.
"""


def _action_catalogue() -> str:
    lines = []
    for name, spec in ACTION_SPEC.items():
        args = ", ".join(spec["args"]) or "none"
        lines.append(f"- {name}(args: {args}) — {spec['desc']}")
    return "\n".join(lines)


def _client(timeout: float | None = None) -> httpx.AsyncClient:
    headers = {"Accept": "application/json"}
    if OLLAMA_API_KEY:
        headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
    return httpx.AsyncClient(
        base_url=OLLAMA_URL, timeout=timeout or AI_TIMEOUT, headers=headers,
        follow_redirects=True,
    )


async def pull_model(model: str, on_progress=None) -> dict:
    """Ask the Ollama server to fetch a model it does not have yet.

    The endpoint is remote and shared, so this is deliberately an explicit
    action rather than something an install triggers on its own.
    """
    try:
        async with _client(timeout=3600) as c:
            async with c.stream("POST", "/api/pull",
                                json={"model": model, "stream": True}) as r:
                if r.status_code >= 400:
                    detail = (await r.aread())[:300].decode("utf-8", "replace")
                    return {"ok": False,
                            "error": f"Ollama returned {r.status_code}: {detail}"}
                last = {}
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        last = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if last.get("error"):
                        return {"ok": False, "error": last["error"]}
                    if on_progress:
                        try:
                            on_progress(last)
                        except Exception:
                            pass
        return {"ok": True, "model": model, "status": last.get("status", "done")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def status() -> dict:
    """Is the assistant usable, and with which model?"""
    if not AI_ENABLED:
        return {"available": False, "reason": "AI_ENABLED is false"}
    try:
        async with _client(timeout=8) as c:
            r = await c.get("/api/tags")
            if r.status_code >= 400:
                return {"available": False,
                        "reason": f"Ollama returned {r.status_code}",
                        "url": OLLAMA_URL}
            models = [m["name"] for m in r.json().get("models", [])]
    except Exception as e:
        return {
            "available": False,
            "reason": f"cannot reach Ollama at {OLLAMA_URL} ({type(e).__name__})",
            "url": OLLAMA_URL,
            "hint": (
                f"BlessForge talks to Ollama over HTTP at {OLLAMA_URL}. Check "
                "that the host is reachable from this container and that "
                "OLLAMA_URL is right; set OLLAMA_API_KEY as well if the "
                "endpoint sits behind an authenticating proxy."
            ),
        }

    if AI_MODEL in models:
        return {"available": True, "model": AI_MODEL, "models": models,
                "url": OLLAMA_URL}
    # Tolerate a tag mismatch (":latest" and friends).
    base = AI_MODEL.split(":")[0]
    fallback = next((m for m in models if m.split(":")[0] == base), None)
    if fallback:
        return {"available": True, "model": fallback, "models": models,
                "url": OLLAMA_URL,
                "note": f"{AI_MODEL} not found; using {fallback}"}
    return {
        "available": False,
        "reason": f"model '{AI_MODEL}' is not installed",
        "models": models,
        "url": OLLAMA_URL,
        "hint": f"Install it on the Ollama host with: ollama pull {AI_MODEL} "
                f"(or use the Pull button, which asks {OLLAMA_URL} to fetch it).",
    }


def _trim_log(text: str) -> str:
    """Keep the parts of a log that carry the failure.

    The tail holds the crash; the head holds the loader/version banner. The
    middle is thousands of lines of mod discovery that tell us nothing.
    """
    if not text:
        return ""
    if len(text) <= MAX_LOG_CHARS:
        return text
    head = text[: MAX_LOG_CHARS // 4]
    tail = text[-(MAX_LOG_CHARS * 3 // 4):]
    return f"{head}\n...[trimmed]...\n{tail}"


def build_evidence(
    *, instance: dict, findings: list[dict], log_tail: str, crash_tail: str,
    mods: list[dict] | None = None, deep_scan: dict | None = None,
) -> str:
    parts = [
        "## Instance",
        f"Minecraft: {instance.get('minecraft') or 'unknown'}",
        f"Loader: {instance.get('loader') or 'unknown'}",
        f"Modpack: {(instance.get('pack') or {}).get('name') or 'none'}",
        f"Mods installed: {instance.get('mod_count', 'unknown')}",
    ]

    if findings:
        parts.append("\n## Automated checks already found")
        for f in findings[:MAX_FINDINGS]:
            detail = (f.get("detail") or "")[:220]
            parts.append(f"- [{f.get('severity')}] {f.get('title')}: {detail}")

    if deep_scan and deep_scan.get("findings"):
        parts.append("\n## Dependency scan")
        for f in deep_scan["findings"][:10]:
            parts.append(f"- [{f.get('severity')}] {f.get('title')}: {f.get('detail')}")

    if mods:
        # Listing every jar of a 200-mod pack is what makes the prompt huge,
        # and the model almost never needs the boring ones. Send the mods a
        # diagnosis actually turns on: the ones already disabled, the ones
        # flagged client-only, and any named in the findings.
        named = " ".join(
            (f.get("title", "") + " " + f.get("detail", "")) for f in findings
        ).lower()
        disabled = [m for m in mods if not m.get("enabled")]
        suspicious = [m for m in mods
                      if m.get("client_only_guess") and m.get("enabled")]
        mentioned = [m for m in mods if m["file"].lower() in named]
        interesting, seen_files = [], set()
        for group in (mentioned, suspicious, disabled):
            for m in group:
                if m["file"] not in seen_files:
                    seen_files.add(m["file"])
                    interesting.append(m)

        parts.append(f"\n## Mods ({len(mods)} installed, "
                     f"{len(disabled)} already disabled)")
        if interesting:
            parts.append("Relevant ones:")
            for m in interesting[:MAX_MODS_LISTED]:
                flags = []
                if not m.get("enabled"):
                    flags.append("disabled")
                if m.get("client_only_guess"):
                    flags.append("looks client-only")
                suffix = f"  [{', '.join(flags)}]" if flags else ""
                parts.append(f"- {m['file']}{suffix}")
        else:
            parts.append("Nothing unusual; the rest are ordinary server mods.")

    if crash_tail:
        parts.append("\n## Crash report\n" + _trim_log(crash_tail))
    if log_tail:
        parts.append("\n## Server log (tail)\n" + _trim_log(log_tail))

    if not crash_tail and not log_tail:
        parts.append(
            "\n## Logs\nNo log file exists. The server produced no output at "
            "all, which usually means it was rejected before the JVM started "
            "(EULA gate, wrong Java version, or not enough memory)."
        )
    return "\n".join(parts)


def _crash_sections(text: str) -> str:
    """Pull the parts of a crash report that carry the verdict.

    A crash report is mostly inventory: the full mod list, the system
    details, hundreds of repeated stack frames. The parts that say what went
    wrong are the description, the exception, and the per-mod failure blocks
    -- a few hundred lines out of two thousand.
    """
    if not text:
        return ""
    lines = text.splitlines()
    keep: list[str] = []

    # The header, up to and including the exception.
    for line in lines[:40]:
        keep.append(line)
        if "A detailed walkthrough" in line:
            break

    # Every per-mod failure block, whichever loader wrote it.
    blocks = re.findall(
        r"^--\s*(?:MOD\s+|Mod loading issue for:\s*)[\w\-.]+\s*--\s*$"
        r"(?:\n(?!--\s).*)*",
        text, re.M,
    )
    if blocks:
        keep.append("")
        keep.extend(b.strip() for b in blocks[:12])

    # Lines that attribute blame directly.
    for line in lines:
        if re.search(r"Suspected Mods|Failure message|Mixin apply|"
                     r"environment type SERVER|Caused by:", line, re.I):
            if line not in keep:
                keep.append(line.rstrip())

    # A slice of the stack, for the frames the above did not cover.
    stack = [l for l in lines if l.lstrip().startswith("at ")][:40]
    if stack:
        keep.append("")
        keep.append("Stack (first 40 frames):")
        keep.extend(stack)

    out = "\n".join(keep)
    return out[:MAX_CRASH_CHARS] if len(out) > MAX_CRASH_CHARS else out


def _log_problem_lines(text: str) -> str:
    """The ERROR/WARN/FATAL lines from a log, newest last.

    A modded server log is 600k characters of mod-discovery chatter. The
    handful of lines at a severity above INFO is the whole of what a
    diagnosis needs from it.
    """
    if not text:
        return ""
    interesting = [
        l.rstrip() for l in text.splitlines()
        if re.search(r"/(ERROR|FATAL)\]|\bERROR\b|\bFATAL\b|Exception|"
                     r"Caused by:|failed", l)
    ]
    if not interesting:
        interesting = text.splitlines()[-60:]
    # Collapse the runs of an identical line a crash loop produces.
    deduped: list[str] = []
    for line in interesting:
        if deduped and deduped[-1] == line:
            continue
        deduped.append(line)
    tail = "\n".join(deduped[-120:])
    return tail[-MAX_LOG_CHARS:]


def _relevant_mods(mods: list[dict], text: str, findings: list[dict] | None,
                   attributed: list[dict] | None = None,
                   ) -> tuple[list[dict], int]:
    """The jars worth naming in the prompt, and how many were left out.

    Listing every jar of a 369-mod pack costs about 12,000 characters -- two
    minutes of prompt evaluation on this endpoint -- to tell the model 360
    things it has no use for. What it needs is the shortlist: whatever the
    log mentions, whatever the deterministic checks flagged, and whatever
    looks client-only. Anything outside that cannot be the answer to "which
    of these does the log blame", because the log never mentions it.
    """
    # The haystack is what the model will actually be shown, NOT the raw
    # report. A crash report ends with a table of every installed mod, so
    # searching the whole file matches all 369 of them and the shortlist
    # degrades into "the first forty alphabetically" -- which is how this
    # went wrong the first time.
    haystack = (text or "").lower()
    named = " ".join(
        (f.get("title", "") + " " + f.get("detail", "")) for f in (findings or [])
    ).lower()

    picked, seen = [], set()

    def take(mod: dict) -> None:
        if mod["file"] not in seen:
            seen.add(mod["file"])
            picked.append(mod)

    # Order is relevance order: the jars the log already blames must survive
    # the cap even on a pack with hundreds of mods.
    blamed = {a["file"] for a in (attributed or [])}
    by_file = {m["file"]: m for m in mods}
    for file in blamed:
        if file in by_file:
            take(by_file[file])
    for mod in mods:
        stem = re.sub(r"\.jar(\.disabled)?$", "", mod["file"]).lower()
        if stem and (stem in haystack or mod["file"].lower() in named):
            take(mod)
    for mod in mods:
        if mod.get("client_only") or mod.get("client_only_guess"):
            take(mod)
    for mod in mods:
        if not mod.get("enabled"):
            take(mod)

    return picked[:MAX_MODS_LISTED], max(0, len(mods) - len(picked[:MAX_MODS_LISTED]))


def build_crash_evidence(
    *, instance: dict, mods: list[dict], crash_text: str, log_text: str,
    findings: list[dict] | None = None, crash_path: str = "",
    log_path: str = "", attributed: list[dict] | None = None,
) -> str:
    """Assemble the evidence for a crash review.

    Selected, not truncated -- see CRASH_PROMPT_BUDGET for why. The order is
    deliberate: what the log already proved comes first, so a model with a
    small attention budget spends it on the right lines.
    """
    section = _crash_sections(crash_text)
    problems = _log_problem_lines(log_text)
    listed, omitted = _relevant_mods(
        mods, section + "\n" + problems, findings, attributed
    )

    parts = [
        "## Instance",
        f"Minecraft: {instance.get('minecraft') or 'unknown'}",
        f"Loader: {instance.get('loader') or 'unknown'}",
        f"Modpack: {(instance.get('pack') or {}).get('name') or 'none'}",
        f"Mods installed: {len(mods)}",
    ]

    if attributed:
        parts.append("\n## What the log already proves")
        parts.append("These attributions come from parsing the report, not "
                     "from guessing. Treat them as facts and build on them.")
        for hit in attributed[:6]:
            parts.append(f"- {hit['file']} ({hit['confidence']} confidence): "
                         f"{hit['why']}")

    if findings:
        parts.append("\n## Automated checks")
        for f in findings[:MAX_FINDINGS]:
            parts.append(f"- [{f.get('severity')}] {f.get('title')}: "
                         f"{(f.get('detail') or '')[:180]}")

    parts.append(f"\n## Jars that could be involved ({len(listed)} of "
                 f"{len(mods)} shown)")
    parts.append("Quote filenames from THIS list, exactly, and no others.")
    for mod in listed:
        flags = []
        if not mod.get("enabled"):
            flags.append("already disabled")
        if mod.get("client_only") or mod.get("client_only_guess"):
            flags.append("client-side")
        parts.append(f"- {mod['file']}" + (f"  [{', '.join(flags)}]" if flags else ""))
    if omitted:
        parts.append(f"({omitted} further jars are installed but are not "
                     "mentioned anywhere in the log.)")

    if section:
        parts.append(f"\n## Crash report{' ' + crash_path if crash_path else ''}")
        parts.append(section)
    if problems:
        parts.append(f"\n## Errors and warnings from the log"
                     f"{' ' + log_path if log_path else ''}")
        parts.append(problems)

    if not crash_text and not log_text:
        parts.append(
            "\n## Logs\nNo crash report and no log file exist. The server "
            "produced no output at all, which means it was rejected before "
            "the JVM started -- the EULA gate, the wrong Java version, or "
            "not enough memory."
        )

    evidence = "\n".join(parts)
    if len(evidence) > CRASH_PROMPT_BUDGET:
        # Last resort. Everything above is already selective, so if it is
        # still oversized the crash report is pathological; cut from the
        # middle of the report rather than dropping the mod list, which is
        # what makes the answer quotable.
        evidence = _trim_middle(evidence, CRASH_PROMPT_BUDGET)
    return evidence


def _trim_middle(text: str, budget: int) -> str:
    """Keep the head and the tail of a long report, drop the middle.

    A Forge crash report opens with the description and the exception, and
    closes with the mod list and system details. What sits between them is
    usually hundreds of stack frames from the same trace, which adds length
    without adding evidence.
    """
    if not text or len(text) <= budget:
        return text
    head = text[: budget * 2 // 3]
    tail = text[-(budget // 3):]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n...[{dropped} characters of stack frames trimmed]...\n{tail}"


def _timing(body: dict) -> dict:
    """Turn Ollama's nanosecond counters into something worth showing.

    Surfacing these is not vanity: on CPU the wait is dominated by prompt
    evaluation, and knowing that is what tells someone whether to trim the
    evidence, warm the model, or pick a smaller one.
    """
    def secs(key: str) -> float:
        value = body.get(key)
        return round(value / 1e9, 1) if isinstance(value, (int, float)) else 0.0

    prompt_tokens = body.get("prompt_eval_count") or 0
    out_tokens = body.get("eval_count") or 0
    prompt_secs = secs("prompt_eval_duration")
    eval_secs = secs("eval_duration")
    return {
        "load_seconds": secs("load_duration"),
        "prompt_seconds": prompt_secs,
        "generate_seconds": eval_secs,
        "total_seconds": secs("total_duration"),
        "prompt_tokens": prompt_tokens,
        "output_tokens": out_tokens,
        "prompt_tokens_per_second": round(prompt_tokens / prompt_secs, 1)
        if prompt_secs else None,
        "output_tokens_per_second": round(out_tokens / eval_secs, 1)
        if eval_secs else None,
    }


async def warm() -> dict:
    """Load the model into memory ahead of time.

    Called when the Troubleshoot tab opens so the first real question does not
    also pay the ~25 s cost of reading the model off disk.
    """
    state = await status()
    if not state.get("available"):
        return {"warmed": False, **state}
    try:
        async with _client(timeout=120) as c:
            r = await c.post("/api/chat", json={
                "model": state["model"],
                "stream": False,
                "keep_alive": AI_KEEP_ALIVE,
                "messages": [{"role": "user", "content": "ok"}],
                "options": {"num_predict": 1, "num_ctx": 512},
            })
        body = r.json() if r.status_code < 400 else {}
        return {"warmed": r.status_code < 400, "model": state["model"],
                **_timing(body)}
    except Exception as e:
        return {"warmed": False, "error": f"{type(e).__name__}: {e}"}


def _extract_json(text: str) -> dict | None:
    """Pull a JSON object out of a model response.

    Even with format=json some models wrap output in fences or emit a
    reasoning preamble, so fall back to locating the outermost braces.
    """
    text = (text or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    # Thinking models may emit a <think> block first.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _norm_file(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _match_file(candidate: str, known_files: set[str]) -> str | None:
    """Map a filename the model wrote onto one that really exists.

    A small model reproduces a long jar name imperfectly -- it drops the
    `.disabled` suffix, changes case, or (seen in testing) splits
    `figura-0.1.5-1.20.1.jar` at a dot and emits two fragments. Rejecting
    those outright threw away correct diagnoses over a typo, so near misses
    are reconciled against the real file list and anything still ambiguous is
    rejected as before.
    """
    if not candidate:
        return None
    if candidate in known_files:
        return candidate
    norm = _norm_file(candidate)
    if not norm:
        return None
    by_norm = {_norm_file(f): f for f in known_files}
    if norm in by_norm:
        return by_norm[norm]
    # `.disabled` written or omitted on either side.
    for suffix in (".jar", ".jar.disabled"):
        alt = _norm_file(re.sub(r"(\.jar)?(\.disabled)?$", "", candidate) + suffix)
        if alt in by_norm:
            return by_norm[alt]
    # A fragment of a real name, but only when exactly one file contains it
    # and the fragment is long enough to be meaningful.
    if len(norm) >= 6:
        hits = [full for key, full in by_norm.items() if norm in key or key in norm]
        if len(hits) == 1:
            return hits[0]
    return None


def validate_plan(raw: dict, known_files: set[str]) -> dict:
    """Keep only actions we can actually execute, with sane arguments."""
    out = {
        "summary": str(raw.get("summary") or "").strip()[:600],
        "confidence": raw.get("confidence") if raw.get("confidence") in
        ("high", "medium", "low") else "low",
        "root_cause": str(raw.get("root_cause") or "").strip()[:120],
        "notes": str(raw.get("notes") or "").strip()[:800],
        "actions": [],
        "rejected": [],
    }

    for entry in (raw.get("actions") or [])[:6]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("action")
        spec = ACTION_SPEC.get(name)
        if not spec:
            out["rejected"].append({"action": name, "why": "unknown action"})
            continue
        args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
        clean: dict = {}
        ok = True

        for key in spec["args"]:
            value = args.get(key)
            if value is None:
                ok = False
                out["rejected"].append(
                    {"action": name, "why": f"missing argument '{key}'"})
                break
            if key == "files":
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list):
                    ok = False
                    out["rejected"].append(
                        {"action": name, "why": "'files' must be a list"})
                    break
                # A hallucinated filename is the most likely failure mode, so
                # every file must resolve to one that exists in this instance.
                real, missing = [], []
                for f in value:
                    hit = _match_file(str(f), known_files)
                    if hit and hit not in real:
                        real.append(hit)
                    elif not hit:
                        missing.append(f)
                if missing:
                    out["rejected"].append({
                        "action": name,
                        "why": f"these files are not in this instance: "
                               f"{', '.join(str(m) for m in missing[:5])}",
                    })
                if not real:
                    ok = False
                    break
                clean["files"] = real
            elif key == "max_gb":
                try:
                    gb = float(value)
                except (TypeError, ValueError):
                    ok = False
                    out["rejected"].append(
                        {"action": name, "why": "max_gb is not a number"})
                    break
                if not 1 <= gb <= 64:
                    ok = False
                    out["rejected"].append(
                        {"action": name, "why": "max_gb outside 1-64"})
                    break
                clean["max_gb"] = gb
            else:
                clean[key] = str(value)[:200]

        if not ok:
            continue
        out["actions"].append({
            "action": name,
            "args": clean,
            "why": str(entry.get("why") or "")[:400],
            "major": spec["major"],
            "description": spec["desc"],
        })

    out["requires_confirmation"] = any(a["major"] for a in out["actions"])
    return out


async def analyse(
    *, instance: dict, findings: list[dict], log_tail: str = "",
    crash_tail: str = "", mods: list[dict] | None = None,
    deep_scan: dict | None = None, question: str = "",
    on_token=None, on_progress=None,
) -> dict:
    """Run the analysis. `on_token(text)` receives output as it is generated.

    Streaming matters here for a practical reason: a 3B model on CPU can take
    a minute or more, and a spinner that never moves is indistinguishable from
    a hang. Callers pass `on_token` to surface progress as it happens.
    """
    state = await status()
    if not state.get("available"):
        return {"available": False, **state}

    model = state["model"]
    evidence = build_evidence(
        instance=instance, findings=findings, log_tail=log_tail,
        crash_tail=crash_tail, mods=mods, deep_scan=deep_scan,
    )
    user_prompt = evidence
    if question:
        user_prompt += f"\n\n## The user asks\n{question.strip()[:500]}"

    streaming = on_token is not None
    # Size the context to the prompt rather than always asking for the
    # model's maximum: the KV cache is allocated up front, and this endpoint
    # is shared with whatever else is using it.
    num_ctx = _size_context(len(user_prompt) + len(SYSTEM_PROMPT))

    payload = {
        "model": model,
        "stream": streaming,
        "format": "json",
        "keep_alive": AI_KEEP_ALIVE,
        "options": {
            # Low temperature: this is diagnosis, not brainstorming.
            "temperature": 0.2,
            "top_p": 0.9,
            "num_ctx": num_ctx,
            "num_predict": 700,
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT % _action_catalogue()},
            {"role": "user", "content": user_prompt},
        ],
    }

    content = ""
    stats: dict = {}
    try:
        async with _client() as c:
            if not streaming:
                r = await c.post("/api/chat", json=payload)
                if r.status_code >= 400:
                    return {"available": True, "ok": False,
                            "error": f"Ollama returned {r.status_code}: "
                                     f"{r.text[:300]}"}
                body = r.json()
                content = (body.get("message") or {}).get("content", "")
                stats = _timing(body)
            else:
                async with c.stream("POST", "/api/chat", json=payload) as r:
                    if r.status_code >= 400:
                        detail = (await r.aread())[:300].decode("utf-8", "replace")
                        return {"available": True, "ok": False,
                                "error": f"Ollama returned {r.status_code}: {detail}"}
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        piece = (chunk.get("message") or {}).get("content", "")
                        if piece:
                            content += piece
                            try:
                                on_token(piece)
                            except Exception:
                                pass
                        if chunk.get("done"):
                            stats = _timing(chunk)
                            if on_progress:
                                try:
                                    on_progress(chunk)
                                except Exception:
                                    pass
    except httpx.TimeoutException:
        return {"available": True, "ok": False,
                "error": f"the model took longer than {AI_TIMEOUT}s. A smaller "
                         f"model, or a longer AI_TIMEOUT, will help.",
                "partial": content[-1500:] or None}
    except Exception as e:
        return {"available": True, "ok": False, "error": f"{type(e).__name__}: {e}",
                "partial": content[-1500:] or None}
    parsed = _extract_json(content)
    if not parsed:
        return {"available": True, "ok": False,
                "error": "the model did not return usable JSON",
                "raw": content[:600]}

    known = {m["file"] for m in (mods or [])}
    plan = validate_plan(parsed, known)
    plan.update({
        "available": True,
        "ok": True,
        "model": model,
        "evidence_chars": len(evidence),
        "num_ctx": num_ctx,
        "stats": stats,
    })
    return plan


# --- crash review ------------------------------------------------------


# Ollama refuses a request whose prompt does not fit the context asked for,
# and says exactly how many tokens it needed. That answer is worth more than
# any estimate, so it is parsed and used rather than guessed at twice.
_CTX_LADDER = (4096, 8192, 16384, 32768, 65536, 131072)
_CTX_OVERFLOW = re.compile(r"request \((\d+) tokens\) exceeds the available "
                           r"context size \((\d+) tokens\)")


def _size_context(prompt_chars: int) -> int:
    """Pick the smallest context window that fits the prompt.

    Qwen3-4B advertises 256k, but asking for all of it allocates a KV cache
    far larger than the job needs and slows every request on a shared server.

    Divided by 2.6, not the usual 4: a crash log is jar filenames, package
    paths and hex, which tokenise far worse than prose. Under-guessing costs
    a whole round trip, so the estimate is deliberately pessimistic.
    """
    approx = int(prompt_chars / 2.6) + 1200
    for candidate in _CTX_LADDER:
        if approx <= candidate:
            return candidate
    return _CTX_LADDER[-1]


def _grow_context(error_text: str, current: int) -> int | None:
    """The context Ollama says this prompt actually needs, or None."""
    m = _CTX_OVERFLOW.search(error_text or "")
    if not m:
        return None
    needed = int(m.group(1)) + 512      # headroom for the reply
    for candidate in _CTX_LADDER:
        if candidate > current and candidate >= needed:
            return candidate
    return None


async def _chat_json(
    *, model: str, system: str, user: str, num_predict: int = 900,
    on_token=None,
) -> dict:
    """One JSON-mode chat turn, streamed when a token callback is given."""
    streaming = on_token is not None
    payload = {
        "model": model,
        "stream": streaming,
        "format": "json",
        "keep_alive": AI_KEEP_ALIVE,
        "options": {
            "temperature": 0.15,
            "top_p": 0.9,
            "num_ctx": _size_context(len(user) + len(system)),
            "num_predict": num_predict,
        },
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    for attempt in range(2):
        try:
            return await _chat_once(payload, on_token if streaming else None)
        except _ContextTooSmall as e:
            bigger = _grow_context(e.detail, payload["options"]["num_ctx"])
            if bigger is None or attempt:
                raise RuntimeError(
                    "the evidence is larger than the model's context window "
                    f"({e.detail[:200]})"
                )
            payload["options"]["num_ctx"] = bigger
    raise RuntimeError("unreachable")


class _ContextTooSmall(RuntimeError):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


async def _chat_once(payload: dict, on_token=None) -> dict:
    streaming = on_token is not None
    content, stats = "", {}
    async with _client() as c:
        if not streaming:
            r = await c.post("/api/chat", json=payload)
            if r.status_code >= 400:
                if "exceed_context_size" in r.text:
                    raise _ContextTooSmall(r.text)
                raise RuntimeError(
                    f"Ollama returned {r.status_code}: {r.text[:300]}")
            body = r.json()
            content = (body.get("message") or {}).get("content", "")
            stats = _timing(body)
        else:
            async with c.stream("POST", "/api/chat", json=payload) as r:
                if r.status_code >= 400:
                    detail = (await r.aread())[:600].decode("utf-8", "replace")
                    if "exceed_context_size" in detail:
                        raise _ContextTooSmall(detail)
                    raise RuntimeError(f"Ollama returned {r.status_code}: {detail}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    piece = (chunk.get("message") or {}).get("content", "")
                    if piece:
                        content += piece
                        try:
                            on_token(piece)
                        except Exception:
                            pass
                    if chunk.get("done"):
                        stats = _timing(chunk)
    return {"content": content, "stats": stats,
            "num_ctx": payload["options"]["num_ctx"]}


def validate_culprits(raw: dict, known_files: set[str]) -> list[dict]:
    """Keep only culprits that name a jar this instance actually has."""
    out, seen = [], set()
    for entry in (raw.get("culprits") or [])[:8]:
        if not isinstance(entry, dict):
            continue
        hit = _match_file(str(entry.get("file") or ""), known_files)
        if not hit or hit in seen:
            continue
        seen.add(hit)
        out.append({
            "file": hit,
            "why": str(entry.get("why") or "")[:400],
            "confidence": entry.get("confidence")
            if entry.get("confidence") in ("high", "medium", "low") else "medium",
        })
    return out


async def review_crash(
    *, instance: dict, mods: list[dict], crash_text: str = "",
    log_text: str = "", findings: list[dict] | None = None,
    crash_path: str = "", log_path: str = "", attributed: list[dict] | None = None,
    on_token=None,
) -> dict:
    """Read a whole crash log and name the mods that caused it.

    Distinct from `analyse`: that summarises an instance's overall health,
    this answers the one question asked after a server dies -- which jar did
    it. The deterministic parse in `diagnostics.attribute_crash` runs first
    and is passed in as evidence; the model's job is to rank and explain, and
    to catch the attributions a regex cannot.
    """
    state = await status()
    if not state.get("available"):
        return {"available": False, **state}

    model = state["model"]
    evidence = build_crash_evidence(
        instance=instance, mods=mods, crash_text=crash_text, log_text=log_text,
        findings=findings, crash_path=crash_path, log_path=log_path,
        attributed=attributed,
    )
    try:
        turn = await _chat_json(
            model=model, system=CRASH_SYSTEM_PROMPT % _action_catalogue(),
            user=evidence, num_predict=1100, on_token=on_token,
        )
    except httpx.TimeoutException:
        return {"available": True, "ok": False,
                "error": f"the model took longer than {AI_TIMEOUT}s"}
    except Exception as e:
        return {"available": True, "ok": False, "error": f"{type(e).__name__}: {e}"}

    parsed = _extract_json(turn["content"])
    if not parsed:
        return {"available": True, "ok": False,
                "error": "the model did not return usable JSON",
                "raw": turn["content"][:600]}

    known = {m["file"] for m in mods}
    plan = validate_plan(parsed, known)
    plan.update({
        "available": True,
        "ok": True,
        "model": model,
        "kind": "crash_review",
        "crash_type": str(parsed.get("crash_type") or "")[:120],
        "culprits": validate_culprits(parsed, known),
        "evidence_chars": len(evidence),
        "num_ctx": turn["num_ctx"],
        "stats": turn["stats"],
        "crash_path": crash_path,
        "log_path": log_path,
    })
    return plan


def split_auto_actions(plan: dict, *, allow_destructive: bool = False
                       ) -> tuple[list[dict], list[dict]]:
    """Divide a plan into what may be applied now and what must be asked.

    "Fix it automatically" is a real request, but it is not a licence to
    delete anything: everything in the automatic half can be undone from the
    Mods tab in one click, and everything that cannot is handed back.
    """
    auto, held = [], []
    for action in plan.get("actions") or []:
        allowed = action.get("action") in AUTO_APPLY_ACTIONS
        if allowed or (allow_destructive and action.get("action") == "delete_mods"):
            auto.append(action)
        else:
            held.append(action)
    return auto, held
