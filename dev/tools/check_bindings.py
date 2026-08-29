#!/usr/bin/env python3
"""Every binding the design canvas reads must exist in renderVals().

The template renders `{{ someName }}` against whatever renderVals() returned.
A name it does not return renders as nothing at all -- no error, no console
message, just a blank where a number should be. That is the single easiest way
to break this UI, so it gets its own check.

Needs Docker (to run the logic under Node) but no network and no backend.

    python3 dev/tools/check_bindings.py
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX = ROOT / "app" / "static" / "index.html"

STUB = r"""
const fs = require('fs');
global.fetch = async () => ({ ok:true, json:async () => ({}) });
global.EventSource = class { constructor(){} close(){} };
global.window = { addEventListener(){}, removeEventListener(){} };
global.document = { getElementById: () => null };
global.navigator = {};
class DCLogic {
  constructor(p){ this.props = p || {}; }
  setState(){} forceUpdate(){}
  componentDidMount(){} componentDidUpdate(){} componentWillUnmount(){}
  renderVals(){ return {}; }
}
const src = fs.readFileSync(process.argv[2], 'utf8');
const Component = new Function('DCLogic','StreamableLogic','React',
  src + '\n;return Component;')(DCLogic, DCLogic, {});
let vals;
try { vals = new Component({}).renderVals(); }
catch (e) { console.error('RENDERVALS THREW: ' + e.message); process.exit(2); }
console.log(JSON.stringify(Object.keys(vals)));
"""


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    tpl = html[html.index("<x-dc>"):html.index("</x-dc>")]

    roots: set[str] = set()
    for m in re.finditer(r"\{\{([^}]*)\}\}", tpl):
        r = re.match(r"([A-Za-z_$][\w$]*)", m.group(1).strip())
        if r:
            roots.add(r.group(1))
    # `<sc-for as="x">` introduces x; the loop body's x.field is not a binding.
    loop_vars = set(re.findall(r'\bas="([A-Za-z_$][\w$]*)"', tpl))
    needed = roots - loop_vars - {"true", "false", "null", "undefined"}

    i = html.index("data-dc-script")
    j = html.index(">", i) + 1
    k = html.rindex("</script>")

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "logic.js").write_text(html[j:k], encoding="utf-8")
        (d / "stub.js").write_text(STUB, encoding="utf-8")
        try:
            out = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{d}:/w:ro", "node:20-alpine",
                 "node", "/w/stub.js", "/w/logic.js"],
                capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError:
            print("docker is not available; cannot run the logic")
            return 2
    if out.returncode != 0:
        print(out.stderr.strip() or "the logic did not run")
        return 2

    provided = set(json.loads(out.stdout))
    missing = sorted(needed - provided)

    print(f"template reads   {len(needed)} bindings")
    print(f"renderVals gives {len(provided)}")
    if missing:
        print(f"\nMISSING ({len(missing)}) — these render blank, silently:")
        for m in missing:
            print("   ", m)
        return 1
    print("\nevery binding the template reads is provided")
    return 0


if __name__ == "__main__":
    sys.exit(main())
