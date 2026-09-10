"""Static checks for the front end.

Replaces `check_bindings.py` and `audit_placeholders.py`, which audited the
design-canvas runtime the UI no longer uses. These check the four things that
can break silently in a no-build-step ES-module front end:

  1. **Every module parses as a module.** `node --check <file>` does *not*
     do this -- it parses as a script and quietly accepts a file with a
     broken string literal in it. Only `--input-type=module` is a real check,
     and that mistake cost a screen once already.
  2. **Every import resolves.** A typo in a relative path is a 404 at
     runtime, on the one route that needs it, and nowhere else.
  3. **Every asset referenced exists.** `/assets/NAME` is a mount over
     `static/img/`, so a renamed file leaves a broken image with no error.
  4. **Every API path the front end names is a route the server serves.**
     This is the contract that used to be enforced by a binding audit, and
     it is the one that matters: a screen calling an endpoint that was
     renamed fails at the moment someone opens it.

Plus one design invariant worth machine-checking, because breaking it is
invisible until you hover: the wiggling-polygon highlight is a `z-index:-1`
child, which only paints behind its parent's background while the parent is
not a stacking context. A `transform` on `.card` or `.btn` creates one, and
the blob then paints between the card fill and the card text.

    python dev/tools/check_frontend.py
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("BF_REPO", pathlib.Path(__file__).resolve().parents[2]))
STATIC = ROOT / "app" / "static"
JS = STATIC / "js"

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"PASS  {label}" + (f"  — {detail}" if detail else ""))
    else:
        failures.append(label)
        print(f"FAIL  {label}" + (f"  — {detail}" if detail else ""))


def js_files() -> list[pathlib.Path]:
    return sorted(JS.rglob("*.js"))


# --- 1. every module parses AS A MODULE --------------------------------

def check_parses() -> None:
    node = "node"
    try:
        subprocess.run([node, "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        print("SKIP  module parse (node is not on PATH)")
        return
    for path in js_files():
        result = subprocess.run(
            [node, "--input-type=module", "--check"],
            input=path.read_bytes(), capture_output=True,
        )
        err = result.stderr.decode("utf-8", "replace").strip()
        check(f"parses as a module: {path.relative_to(STATIC)}",
              result.returncode == 0 and not err,
              err.splitlines()[-1] if err else "")


# --- 2. every import resolves ------------------------------------------

_IMPORT = re.compile(r"""(?:^|\s)(?:import|export)\b[^;\n]*?from\s+['"]([^'"]+)['"]""",
                     re.M)
_DYNAMIC = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")


def check_imports() -> None:
    for path in js_files():
        text = path.read_text(encoding="utf-8")
        for spec in set(_IMPORT.findall(text)) | set(_DYNAMIC.findall(text)):
            if not spec.startswith("."):
                check(f"bare import in {path.name}", False,
                      f"{spec} — there is no bundler, so only relative "
                      "paths resolve")
                continue
            target = (path.parent / spec).resolve()
            check(f"import resolves: {path.name} -> {spec}", target.is_file(),
                  "" if target.is_file() else f"missing {target}")


# --- 3. every referenced asset exists ----------------------------------

_ASSET = re.compile(r"""['"`]/assets/([A-Za-z0-9._-]+)['"`]""")
_ASSET_TPL = re.compile(r"""`/assets/\$\{[^}]+\}`""")
_STATIC_REF = re.compile(r"""['"]/static/([A-Za-z0-9._/-]+)['"]""")


def check_assets() -> None:
    img = STATIC / "img"
    named: set[str] = set()
    for path in list(js_files()) + [STATIC / "index.html"]:
        text = path.read_text(encoding="utf-8")
        named |= set(_ASSET.findall(text))
        for ref in set(_STATIC_REF.findall(text)):
            target = STATIC / ref
            check(f"/static/{ref} exists", target.is_file(),
                  "" if target.is_file() else "not in app/static/")
    # Art passed through a variable (empty(), loader logos) is named as a
    # bare string at its call site, so those are caught too.
    named |= set(re.findall(r"""empty\(\s*'([A-Za-z0-9._-]+)'""",
                            "\n".join(p.read_text(encoding="utf-8")
                                      for p in js_files())))
    for name in sorted(named):
        check(f"asset exists: {name}", (img / name).is_file(),
              "" if (img / name).is_file() else "not in app/static/img/")


# --- 4. every API path the front end calls is a real route -------------

_CALL = re.compile(
    r"""api\.(?:get|post|put|del|upload|raw)\(\s*[`'"]([^`'"$]*)""")
_RUN = re.compile(r"""(?:run|runAwait)\(\s*[`'"]([^`'"$]*)""")
_ES = re.compile(r"""new EventSource\(\s*[`'"]([^`'"$]*)""")


def route_patterns() -> set[str]:
    sys.path.insert(0, str(ROOT))
    from app.main import app  # noqa: E402  -- needs the path above
    return {r.path for r in app.routes if hasattr(r, "path")}


def normalise(path: str) -> str:
    """Turn a call-site path into something comparable with a route.

    Call sites interpolate ids, so the literal prefix is all there is:
    `/api/instances/${id}/files` arrives here as `/api/instances/`. Compared
    by prefix for that reason -- a check that demands an exact match would
    have to reimplement the template.
    """
    path = path.split("?")[0].rstrip("/")
    return path


def check_api() -> None:
    try:
        routes = route_patterns()
    except Exception as e:  # noqa: BLE001 -- reported, not raised
        check("the API surface could be imported", False, str(e))
        return
    # Every route pattern with its parameter placeholders stripped back to
    # the literal text before the first one.
    prefixes = {r.split("{")[0].rstrip("/") for r in routes} | {
        r.rstrip("/") for r in routes}

    text = "\n".join(p.read_text(encoding="utf-8") for p in js_files())
    called = {normalise(m) for m in
              _CALL.findall(text) + _RUN.findall(text) + _ES.findall(text)}
    called = {c for c in called if c.startswith("/api")}

    for path in sorted(called):
        ok = any(path == p or path.startswith(p) or p.startswith(path)
                 for p in prefixes)
        check(f"API path is served: {path}", ok,
              "" if ok else "no route on app.main starts with this")


# --- 5. the wiggling-polygon invariant ---------------------------------

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)


def check_stacking_context() -> None:
    """No transform / opacity / filter on anything that carries a highlight.

    Each of those makes the element a stacking context, at which point its
    z-index:-1 child stops painting behind the element's own background and
    starts painting on top of it -- behind the text, in front of the fill.
    Invisible until hover, and it looks like a colour bug rather than a
    layering one.
    """
    css = (STATIC / "css" / "theme.css").read_text(encoding="utf-8")
    offenders: list[str] = []
    for selector, body in _RULE.findall(css):
        sel = selector.strip()
        if sel.startswith("@") or "keyframes" in sel:
            continue
        # Only the selectors that actually host a highlight.
        if not re.search(r"(^|[,\s])\.(card|btn|p5)\b", sel):
            continue
        if ".p5-hl" in sel or "::before" in sel:
            continue
        for prop in ("transform", "opacity", "filter", "isolation",
                     "will-change", "backdrop-filter"):
            m = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", body)
            if not m:
                continue
            value = m.group(1).strip().rstrip("!important").strip()
            # `transform:none` and `opacity:1` create no stacking context.
            if value in ("none", "1", "auto", "initial"):
                continue
            offenders.append(f"{sel} {{ {prop}: {value} }}")
    check("nothing that carries the wiggling polygon is a stacking context",
          not offenders, "; ".join(offenders[:3]))


# --- 6. the icon set is complete ---------------------------------------

def check_icons() -> None:
    core = (JS / "core.js").read_text(encoding="utf-8")
    block = core.split("const PATHS = {", 1)[1].split("\n};", 1)[0]
    known = set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9]*):", block, re.M))
    text = "\n".join(p.read_text(encoding="utf-8") for p in js_files())
    used = set(re.findall(r"""icon\(\s*'([A-Za-z][A-Za-z0-9]*)'""", text))
    missing = sorted(used - known)
    check("every icon() name exists in the set", not missing,
          ", ".join(missing) if missing else f"{len(known)} icons, "
          f"{len(used)} used")


def main() -> int:
    print("--- front end ---")
    check_parses()
    check_imports()
    check_assets()
    check_icons()
    check_stacking_context()
    print("--- API contract ---")
    check_api()
    print()
    if failures:
        print(f"{checks - len(failures)}/{checks} checks passed; "
              f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"{checks}/{checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
