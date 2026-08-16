"""Local AI assistant for troubleshooting, via Ollama.

Deliberate constraints, because a 3B model is a useful pattern-matcher and a
poor decision-maker:

  * It never executes anything. It returns a *proposal*; every action is
    applied only after the user approves it in the UI.
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

OLLAMA_URL = (os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
              ).rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "qwen2.5:3b-instruct-q4_K_M")
AI_ENABLED = (os.environ.get("AI_ENABLED", "true").lower()
              in ("1", "true", "yes", "on"))
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "300"))
# How long Ollama should keep the model resident. Loading a 2 GB model off
# disk costs ~25 s on a spinning-rust host; holding it in RAM makes a second
# question feel instant instead. Set to "0" to free the memory immediately.
AI_KEEP_ALIVE = os.environ.get("AI_KEEP_ALIVE", "30m")
# Keep the prompt small. This is not only about model quality: on a CPU-only
# host, prompt evaluation dominates total time, and it happens *before* the
# first token, so an oversized prompt shows up as a long dead wait with
# nothing on screen. Measured on an i7-4720HQ with qwen2.5:3b, trimming the
# evidence from ~9k to ~2k characters cut time-to-first-token from about
# three minutes to a few seconds.
MAX_LOG_CHARS = int(os.environ.get("AI_MAX_LOG_CHARS", "2000"))
MAX_MODS_LISTED = int(os.environ.get("AI_MAX_MODS_LISTED", "25"))
MAX_FINDINGS = 8

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


def _action_catalogue() -> str:
    lines = []
    for name, spec in ACTION_SPEC.items():
        args = ", ".join(spec["args"]) or "none"
        lines.append(f"- {name}(args: {args}) — {spec['desc']}")
    return "\n".join(lines)


def _client(timeout: float | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=OLLAMA_URL, timeout=timeout or AI_TIMEOUT)


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
                "Set OLLAMA_URL to your Ollama host. From a container on the "
                "default bridge, http://host.docker.internal:11434 works when "
                "extra_hosts maps it to host-gateway."
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
        "hint": f"Run: ollama pull {AI_MODEL}",
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
                # every file must exist in this instance.
                real = [f for f in value if f in known_files]
                missing = [f for f in value if f not in known_files]
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
    # Size the context to the prompt instead of always asking for 8k. A larger
    # window costs allocation and evaluation time on CPU even when most of it
    # goes unused.
    approx_tokens = (len(user_prompt) + len(SYSTEM_PROMPT)) // 3 + 600
    num_ctx = 2048
    for candidate in (2048, 4096, 8192, 16384):
        if approx_tokens <= candidate:
            num_ctx = candidate
            break
    else:
        num_ctx = 16384

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
