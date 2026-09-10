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

Plus the layering contract behind the wiggling polygon, which is worth
machine-checking because breaking it is invisible until someone hovers. The
highlight is an explicit layer inside each card and button -- blob at 0, fill
at 1, content at 2 -- and every part of that has to hold together or the blob
paints over the card instead of behind it. See `check_layering`.

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


def _decl(body: str, prop: str) -> str | None:
    m = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", body)
    return m.group(1).strip() if m else None


_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) pairs, with comments stripped first.

    Stripping matters: the selector group is `[^{}]+`, so it swallows
    everything since the previous closing brace -- including the comment
    block above the rule. Left in, `.card` is never found by name and every
    layering check reports a false failure.
    """
    css = _COMMENT.sub("", css)
    out = []
    for sel, body in _RULE.findall(css):
        sel = " ".join(sel.split()).strip()
        if not sel or sel.startswith("@") or "keyframes" in sel:
            continue
        out.append((sel, body))
    return out


def check_layering() -> None:
    """The polygon must paint behind the card's FILL, not behind its text.

    This inverts the rule that used to be here. The highlight was a
    `z-index:-1` pseudo-element, which paints behind its parent's background
    only while the parent is NOT a stacking context -- so the old check
    forbade transforms everywhere. That rule was one animated `transform`
    away from being broken, and the entrance animation on `.stagger > *`
    duly broke it: the blob started painting over the card fill and under the
    text, which is exactly what it must never do.

    The layering is now explicit and the contract is the opposite one. Each
    highlight host is a stacking context ON PURPOSE, and inside it:

        .p5-hl    z-index 0   the blob
        ::after   z-index 1   the fill and the border
        content   z-index 2   everything you read

    So what is checked is that the three layers exist and stay in that
    order -- and transforms are free, which is what lets the app move.
    """
    theme = (STATIC / "css" / "theme.css").read_text(encoding="utf-8")
    app = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    rules = _rules(theme) + _rules(app)

    def find(selector: str, prop: str) -> str | None:
        """The last declared value for `prop` on a rule naming `selector`.

        Last, not first: CSS cascades, and a later rule is the one that
        actually applies.
        """
        found = None
        for sel, body in rules:
            if selector not in [s.strip() for s in sel.split(",")]:
                continue
            value = _decl(body, prop)
            if value:
                found = value
        return found

    # Every host that carries a blob must isolate, or the layering is at the
    # mercy of whatever ancestor happens to be a stacking context.
    for host in (".card", ".btn", ".loadercard"):
        check(f"{host} isolates its own stacking context",
              find(host, "isolation") == "isolate",
              "without `isolation:isolate` the layer order is not ours to set")

    blob_z = find(".p5-hl", "z-index")
    check("the blob sits on layer 0", blob_z == "0", f"z-index: {blob_z}")

    for host in (".card::after", ".btn::after", ".loadercard::after"):
        z = find(host, "z-index")
        check(f"{host} (the fill) sits above the blob", z == "1", f"z-index: {z}")

    for host in (".card > *:not(.p5-hl)", ".btn > *:not(.p5-hl)",
                 ".loadercard > *:not(.p5-hl)"):
        z = find(host, "z-index")
        check(f"content in {host.split(' ')[0]} sits above the fill",
              z == "2", f"z-index: {z}")

    # `.card > *` without the :not() would win over `.p5-hl`'s own
    # `position:absolute` on specificity and collapse the blob to a zero-size
    # element in flow -- which looks exactly like the highlight not working,
    # and did, once.
    naked = [sel for sel, _ in rules
             if sel in (".card > *", ".btn > *", ".loadercard > *")]
    check("no blanket `> *` rule can outrank the blob's own position",
          not naked, ", ".join(naked))

    # Nothing may reintroduce the old negative-z-index trick.
    negatives = [sel for sel, body in rules
                 if _decl(body, "z-index") == "-1"]
    check("nothing relies on a negative z-index any more",
          not negatives, ", ".join(negatives[:3]))


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
    check_layering()
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
