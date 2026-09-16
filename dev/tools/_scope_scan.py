"""Find assignments to names that were never declared in their module.

An ES module is always strict, so `page = 0` where `page` was never
declared is a ReferenceError at RUNTIME -- not a parse error. `node
--input-type=module --check` accepts the file happily, and the failure
only appears when that exact line executes, inside whatever handler it
happens to be in.

That is how the add-mod search broke: a `page` variable was removed when
the browser became an endless feed, and two assignments to it survived in
the search input's `oninput` and in the CurseForge/Modrinth switch. Both
threw before the search could run, so typing did nothing and the source
toggle did nothing, with only a console error to show for it.

Heuristic, deliberately: a full scope analysis needs a real parser and
this needs to run with nothing installed. It blanks comments and string
bodies first (an SVG attribute inside a template literal looks exactly
like an assignment), collects every binding form it can see, and reports
what is left. False positives are possible; a silent ReferenceError in a
click handler is worse.
"""
from __future__ import annotations

import pathlib
import re

GLOBALS = set("""
window document location console localStorage sessionStorage navigator history
performance fetch setTimeout clearTimeout setInterval clearInterval
requestAnimationFrame cancelAnimationFrame Math JSON Object Array String Number
Boolean Date Promise Set Map WeakMap WeakSet RegExp Error TypeError URL
URLSearchParams Intl IntersectionObserver MutationObserver ResizeObserver
EventSource FormData Blob File FileReader AbortController CustomEvent Event
KeyboardEvent MouseEvent PointerEvent DragEvent Image matchMedia
getComputedStyle structuredClone queueMicrotask crypto atob btoa isNaN parseInt
parseFloat encodeURIComponent decodeURIComponent alert confirm prompt
globalThis undefined NaN Infinity Node Element HTMLElement DOMParser
""".split())

_ASSIGN = re.compile(
    r"(?<![\w$.?])([a-z_$][\w$]*)\s*(?:=(?!=|>)|\+\+|--|\+=|-=|\*=|/=|\|\|=|\?\?=)")

_BINDERS = (
    # A destructuring pattern FIRST, and greedily to its closing brace: the
    # plain form below stops at the first `=`, which silently loses every
    # name after a default value -- `const { a, b = 1, c } = opts` would
    # bind `a` and `b` and miss `c`, and `c` would then be reported as an
    # undeclared assignment it never was.
    r"\b(?:let|const|var)\s+(\{[\s\S]*?\}|\[[\s\S]*?\])\s*=",
    r"\b(?:let|const|var)\s+([^=;{\[\n]+)",
    r"\bfunction\s*\*?\s*([\w$]+)",
    r"\bclass\s+([\w$]+)",
    r"import\s+([^;]+?)\s+from",
    r"\bcatch\s*\(\s*([\w$]+)",
    r"\bfor\s*\(\s*(?:const|let|var)\s+([^);]+)",
)

BACKSLASH = chr(92)


def blank(src: str) -> str:
    """Comments and string bodies replaced by spaces, newlines preserved.

    Line numbers have to survive so a hit can be reported at the line it
    is actually on.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
        elif c in "\"'`":
            quote, j = c, i + 1
            while j < n:
                if src[j] == BACKSLASH:
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                j += 1
            body = src[i + 1:max(i + 1, j - 1)]
            out.append(quote + "".join(ch if ch == "\n" else " " for ch in body) + quote)
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def declared_in(src: str) -> set[str]:
    names = set(GLOBALS)
    for pattern in _BINDERS:
        for m in re.finditer(pattern, src):
            names |= set(re.findall(r"[\w$]+", m.group(1)))
    # Parameter lists, arrow and classic, plus bare single-arg arrows.
    for m in re.finditer(r"\(([^()]*)\)\s*=>|function\s*\*?\s*[\w$]*\s*\(([^()]*)\)",
                         src):
        names |= set(re.findall(r"[\w$]+",
                                (m.group(1) or "") + " " + (m.group(2) or "")))
    names |= set(re.findall(r"(?:^|[(,\s])([\w$]+)\s*=>", src))
    # Object-literal and class method shorthand: `foo(a, b) {`
    for m in re.finditer(r"[\w$]+\s*\(([^()]*)\)\s*\{", src):
        names |= set(re.findall(r"[\w$]+", m.group(1)))
    return names


def scan(root: pathlib.Path) -> list[tuple[str, int, str]]:
    hits = []
    for path in sorted(root.rglob("*.js")):
        src = blank(path.read_text(encoding="utf-8"))
        known = declared_in(src)
        for m in _ASSIGN.finditer(src):
            name = m.group(1)
            if name in known:
                continue
            hits.append((str(path).replace("\\", "/"),
                         src[:m.start()].count("\n") + 1, name))
    return hits


if __name__ == "__main__":
    found = scan(pathlib.Path("app/static/js"))
    print(f"{len(found)} assignment(s) to an undeclared name:")
    for f, line, name in found:
        print(f"  {f}:{line}   {name} = ...")
