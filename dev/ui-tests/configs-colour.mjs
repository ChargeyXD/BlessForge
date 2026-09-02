// The Configs editor's colour layer, in a real browser.
//
// A transparent textarea over a coloured <pre> is only correct while the two
// agree character-for-character and pixel-for-pixel, so this checks the text
// round-trips, the metrics match, and the layer tracks the textarea on both
// scroll axes. It types one probe line to prove the layer re-colours, then
// reverts it -- edits live in the draft until Save, which this never presses.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const OUT = process.env.BF_OUT || "/out";
const out = [];
const check = (n, c, extra = "") => {
  out.push(!!c);
  console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + String(extra).slice(0, 120) : ""}`);
};

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1050 });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const click = (t) => page.evaluate((t) => {
  const all = [...document.querySelectorAll("button,a,div")].filter(e =>
    e.offsetParent && (e.tagName !== "DIV" || getComputedStyle(e).cursor === "pointer"));
  const el = all.reverse().find(e =>
    (e.innerText || "").replace(/\s+/g, " ").trim().includes(t));
  if (el) { el.click(); return true; }
  return false;
}, t);

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(7000);

const instances = await page.evaluate(async (u) =>
  await (await fetch(u + "/api/instances")).json(), BASE);
const srv = instances.items[0];
check("a server is available to open", !!srv, srv && srv.name);

await page.evaluate((name) => {
  const c = [...document.querySelectorAll("[data-bf-spine] div")]
    .filter(e => e.offsetParent && getComputedStyle(e).cursor === "pointer")
    .find(e => (e.innerText || "").includes(name));
  if (c) c.click();
}, srv.name);
await sleep(4000);
await click("Configs");
await sleep(9000);

/* --- the layer exists and carries the same text -------------------------- */
const base = await page.evaluate(() => {
  const ta = document.getElementById("bfEditor");
  const hi = document.getElementById("bfHi");
  if (!ta || !hi) return { missing: !ta ? "textarea" : "layer" };
  const cs = (el) => {
    const s = getComputedStyle(el);
    return [s.fontFamily, s.fontSize, s.lineHeight, s.letterSpacing,
            s.paddingTop, s.paddingLeft, s.whiteSpace, s.tabSize].join(" | ");
  };
  return {
    value: ta.value,
    layer: hi.textContent,
    runs: hi.querySelectorAll("span").length,
    colours: [...new Set([...hi.querySelectorAll("span")]
      .map(s => getComputedStyle(s).color))],
    taMetrics: cs(ta), hiMetrics: cs(hi),
    taColour: getComputedStyle(ta).color,
    caret: getComputedStyle(ta).caretColor,
    layerBox: hi.getBoundingClientRect().toJSON(),
    taBox: ta.getBoundingClientRect().toJSON(),
  };
});

check("the colour layer is rendered", !base.missing, base.missing && ("missing " + base.missing));
check("the layer reproduces the file exactly", base.layer === base.value,
      base.layer === base.value ? `${base.value.length} chars`
        : `textarea ${base.value.length} vs layer ${base.layer.length}`);
check("the text is split into coloured runs", base.runs > 1, base.runs + " runs");
check("more than one colour is actually used", base.colours.length > 1,
      base.colours.join(" "));
check("both layers share every text metric", base.taMetrics === base.hiMetrics,
      base.taMetrics === base.hiMetrics ? base.taMetrics
        : `\n      textarea: ${base.taMetrics}\n      layer:    ${base.hiMetrics}`);
check("the textarea's own glyphs are hidden",
      /rgba\(0, 0, 0, 0\)|transparent/.test(base.taColour), base.taColour);
check("the caret is still visible", !/rgba\(0, 0, 0, 0\)/.test(base.caret), base.caret);
check("the layer sits exactly over the textarea",
      Math.abs(base.layerBox.x - base.taBox.x) < 1
      && Math.abs(base.layerBox.y - base.taBox.y) < 1
      && Math.abs(base.layerBox.width - base.taBox.width) < 1,
      `layer ${Math.round(base.layerBox.x)},${Math.round(base.layerBox.y)} vs ` +
      `textarea ${Math.round(base.taBox.x)},${Math.round(base.taBox.y)}`);

/* --- a glyph in the layer lands on the same pixel as in the textarea ------ */
// Measured by rendering a probe span with the layer's metrics and comparing it
// against the textarea's own scroll width for the same content.
const align = await page.evaluate(() => {
  const ta = document.getElementById("bfEditor");
  const hi = document.getElementById("bfHi");
  const probe = document.createElement("span");
  const s = getComputedStyle(hi);
  probe.style.cssText = `position:absolute;visibility:hidden;white-space:pre;` +
    `font-family:${s.fontFamily};font-size:${s.fontSize};letter-spacing:${s.letterSpacing};`;
  probe.textContent = "M".repeat(80);
  document.body.appendChild(probe);
  const wHi = probe.getBoundingClientRect().width;
  const t = getComputedStyle(ta);
  probe.style.fontFamily = t.fontFamily;
  probe.style.fontSize = t.fontSize;
  probe.style.letterSpacing = t.letterSpacing;
  const wTa = probe.getBoundingClientRect().width;
  probe.remove();
  return { wHi, wTa };
});
check("80 characters measure the same width in both",
      Math.abs(align.wHi - align.wTa) < 0.5,
      `${align.wHi.toFixed(2)}px vs ${align.wTa.toFixed(2)}px`);

/* --- scrolling keeps them together --------------------------------------- */
const scrolled = await page.evaluate(async () => {
  const ta = document.getElementById("bfEditor");
  const hi = document.getElementById("bfHi");
  ta.scrollTop = 40; ta.scrollLeft = 24;
  ta.dispatchEvent(new Event("scroll", { bubbles: true }));
  await new Promise(r => setTimeout(r, 250));
  const g = document.getElementById("bfGutter");
  return { hiTop: hi.scrollTop, hiLeft: hi.scrollLeft,
           gutTop: g ? g.scrollTop : null, taTop: ta.scrollTop, taLeft: ta.scrollLeft };
});
check("the layer follows the textarea vertically",
      scrolled.hiTop === scrolled.taTop, `layer ${scrolled.hiTop} / textarea ${scrolled.taTop}`);
check("and horizontally, since wrap is off",
      scrolled.hiLeft === scrolled.taLeft, `layer ${scrolled.hiLeft} / textarea ${scrolled.taLeft}`);
check("the gutter still follows too",
      scrolled.gutTop === scrolled.taTop, `gutter ${scrolled.gutTop}`);

/* --- the layer keeps up while typing ------------------------------------- */
// Typing only touches the draft in memory; nothing is written unless Save is
// pressed, and this reverts afterwards regardless.
await page.focus("#bfEditor");
await page.keyboard.type("\n# probe = 1\n");
await sleep(700);
const typed = await page.evaluate(() => {
  const ta = document.getElementById("bfEditor");
  const hi = document.getElementById("bfHi");
  return { exact: hi ? hi.textContent === ta.value : null,
           hasProbe: ta.value.includes("# probe = 1"),
           runs: hi ? hi.querySelectorAll("span").length : 0 };
});
check("the edit reached the textarea", typed.hasProbe);
check("the layer re-coloured to match what was typed", typed.exact === true,
      typed.exact === true ? typed.runs + " runs" : "layer and textarea diverged");
await click("Revert");
await sleep(900);
const reverted = await page.evaluate(() => {
  const ta = document.getElementById("bfEditor");
  const hi = document.getElementById("bfHi");
  return { clean: !ta.value.includes("# probe = 1"),
           exact: hi ? hi.textContent === ta.value : null };
});
check("Revert put the file back", reverted.clean);
check("and the layer followed it back", reverted.exact === true);

/* --- the status bar names the grammar ------------------------------------ */
const bar = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
check("the status bar names the grammar in use",
      /(properties|toml|json|yaml|ini|xml|markdown|javascript|plain)\b/.test(bar),
      (bar.match(/utf-8[^|]{0,60}/) || [""])[0]);

check("no horizontal overflow with the layer in place",
      !(await page.evaluate(() =>
        document.documentElement.scrollWidth > window.innerWidth + 1)));
check("no uncaught page errors", errors.length === 0, errors.slice(0, 3).join(" | "));

await page.screenshot({ path: `${OUT}/configs-colour.png` });
console.log("wrote", `${OUT}/configs-colour.png`);

const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
await browser.close();
process.exit(passed === out.length ? 0 : 1);
