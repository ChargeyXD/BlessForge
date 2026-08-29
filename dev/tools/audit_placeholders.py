#!/usr/bin/env python3
"""Find text in the canvas that should have come from the API.

The front end is the design canvas with its markup kept intact, which is the
point -- but the canvas was drawn against mock data, so any text node that is
neither a binding nor genuine UI chrome is a number or a name someone will
eventually believe. This lists them so they can be judged one at a time.

Chrome (labels, headings, help text) is fine and expected; what matters is
anything that looks like a *value*: a count, a size, a version, a name, a
timestamp, a path.

    python3 dev/tools/audit_placeholders.py            # suspicious only
    python3 dev/tools/audit_placeholders.py --all      # every literal
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX = ROOT / "app" / "static" / "index.html"

# Text that reads like data rather than chrome.
SUSPICIOUS = [
    (re.compile(r"\b\d[\d,]*\s*(MB|GB|KB|ms|GHz|threads?)\b", re.I), "a measurement"),
    (re.compile(r"\b\d+\s*(of|/)\s*\d+\b"), "a count of a count"),
    (re.compile(r"\b\d+\.\d+\.\d+\b"), "a version"),
    (re.compile(r"\b\d+\s*(mods?|jars?|files?|players?|servers?|warnings?|packs?)\b", re.I),
     "a quantity"),
    (re.compile(r"\b\d+m \d+s\b|\b\d+ (min|hours?|days?|weeks?) ago\b"), "a duration"),
    (re.compile(r"\b(?:25\d{3})\b"), "a port"),
    (re.compile(r"/(?:srv|crafty|home)/\S+"), "a path"),
    (re.compile(r"\bv?\d+\.\d+(\.\d+)?[-+]\w+"), "a build string"),
    (re.compile(r"\b(qwen|ollama|localhost|127\.0\.0\.1)\b", re.I), "an endpoint"),
    (re.compile(r"\.(jar|zip|toml|json|properties|log|txt)\b"), "a filename"),
]

# Words that are chrome even though they contain digits or look name-ish.
CHROME = re.compile(
    r"^(?:[←-⇿─-➿⬀-⯿×·…\s]|"
    r"&#\d+;|&\w+;)*$"
)


def literals(tpl: str):
    """Every text node between tags that is not purely a binding."""
    for m in re.finditer(r">([^<>]{2,200})<", tpl):
        raw = m.group(1)
        line = tpl[: m.start()].count("\n") + 1
        # strip bindings; what is left is literal text
        rest = re.sub(r"\{\{[^}]*\}\}", "", raw).strip()
        if not rest or CHROME.match(rest):
            continue
        yield line, rest


def main() -> int:
    show_all = "--all" in sys.argv
    html = INDEX.read_text(encoding="utf-8")
    tpl = html[html.index("<x-dc>"): html.index("</x-dc>")]
    base = html[: html.index("<x-dc>")].count("\n")

    hits, total = [], 0
    for line, text in literals(tpl):
        total += 1
        why = [w for rx, w in SUSPICIOUS if rx.search(text)]
        if why:
            hits.append((base + line, text, why[0]))
        elif show_all:
            hits.append((base + line, text, "literal"))

    print(f"{total} literal text nodes in the canvas")
    print(f"{len([h for h in hits if h[2] != 'literal'])} look like data\n")
    for line, text, why in hits:
        print(f"  index.html:{line}  [{why}]")
        print(f"    {text[:120]}")
    return 1 if any(h[2] != "literal" for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
