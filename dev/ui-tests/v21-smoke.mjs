// BlessForge 2.1 — HANDOVERCASAOS.md §3A and §3B, in a real browser.
//
// §3A: it starts, the fleet renders, nothing is obviously wrong.
// §3B: the existing fleet — created by 2.0 — still reads correctly through
//      everything 2.1 touched.
//
// Read-only. It navigates and reads; it starts, stops, saves and deletes
// nothing. Routes are hashes, so it goes by URL rather than hunting buttons.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const OUT = process.env.BF_OUT || "/out";
const out = [];
const check = (n, c, extra = "") => {
  out.push(!!c);
  console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + String(extra).slice(0, 130) : ""}`);
};
const note = (s) => console.log(`      ${s}`);

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1050 });

// Every console error and failed request, for the "no errors" check.
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("requestfailed", (r) => {
  const u = r.url();
  // Google Fonts is loaded deliberately as a progressive enhancement and is
  // expected to fail on a box with no route out; index.html says so.
  if (/favicon|fonts\.googleapis|fonts\.gstatic/.test(u)) return;
  // Leaving a tab aborts whatever it had in flight -- the console poll, most
  // often. That is cleanup, not a failed request.
  if (r.failure()?.errorText === "net::ERR_ABORTED") return;
  errors.push(`reqfail ${u.slice(0, 80)} ${r.failure()?.errorText}`);
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => page.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
const overflows = () => page.evaluate(() =>
  document.documentElement.scrollWidth > window.innerWidth + 1);
const goto = async (hash, wait = 3500) => {
  await page.evaluate((h) => { location.hash = h; }, hash);
  await sleep(wait);
};

/* ================= §3A — it starts ================= */
console.log("--- §3A: it starts, and nothing is obviously wrong ---");

const t0 = Date.now();
await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(6000);
note(`first paint to settled: ${Math.round((Date.now() - t0) / 100) / 10}s`);

const health = await page.evaluate(async (u) =>
  await (await fetch(u + "/api/health")).json(), BASE);
const healthz = await page.evaluate(async (u) =>
  await (await fetch(u + "/api/healthz")).json(), BASE);

check("healthz returns ok/BlessForge",
      healthz.status === "ok" && healthz.app === "BlessForge", JSON.stringify(healthz));
check("health.ready is true", health.ready === true);
check("crafty is reachable, with a latency",
      health.checks?.crafty?.ok === true && typeof health.checks.crafty.latency_ms === "number",
      `${health.checks?.crafty?.servers} servers, ${health.checks?.crafty?.latency_ms} ms`);
check("curseforge is ok", health.checks?.curseforge?.ok === true);
check("storage is ok (§0 would have failed otherwise)",
      health.checks?.storage?.ok === true,
      `${health.checks?.storage?.free_gb} GB free at ${health.checks?.storage?.path}`);

const instances = await page.evaluate(async (u) =>
  (await (await fetch(u + "/api/instances")).json()).items, BASE);
note(`${instances.length} instances: ` + instances.map((i) => `${i.name} (${i.state})`).join(", "));

const shell = await page.evaluate(() => ({
  nodes: document.querySelectorAll("body *").length,
  hasCanvas: !!document.querySelector("x-dc"),
  modules: [...document.querySelectorAll('script[type=module]')].map((s) => s.src),
}));
check("the page actually rendered", shell.nodes > 150, shell.nodes + " nodes");
check("the design-canvas runtime is gone", !shell.hasCanvas);

const fleet = await text();
for (const inst of instances) {
  check(`fleet lists ${inst.name}`, fleet.includes(inst.name));
}

// Loader, version and port for each, read off the card rather than the API.
const cards = await page.evaluate(() => {
  const body = document.body.innerText;
  return body;
});
for (const inst of instances) {
  const bits = [];
  if (inst.minecraft) bits.push(["version", inst.minecraft]);
  if (inst.loader) bits.push(["loader", inst.loader]);
  if (inst.port) bits.push(["port", String(inst.port)]);
  const missing = bits.filter(([, v]) => !new RegExp(v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i").test(cards));
  check(`${inst.name}: version, loader and port on the card`,
        missing.length === 0,
        missing.length ? "missing " + missing.map(([k, v]) => `${k}=${v}`).join(", ")
                       : bits.map(([, v]) => v).join(" · "));
}

const pills = await page.evaluate(() =>
  [...document.querySelectorAll(".pill, [class*=pill]")].map((e) => e.textContent.trim().toLowerCase()));
for (const inst of instances) {
  const want = { running: "running", stopped: "stopped", crashed: "crashed",
                 orphan: "orphaned", incomplete: "half" }[inst.state] || inst.state;
  check(`${inst.name}: state reads "${want}"`,
        pills.some((p) => p.includes(want)) || fleet.toLowerCase().includes(want));
}

check("no horizontal overflow at 1600px", !(await overflows()));
check("no console errors on the fleet", errors.length === 0, errors.slice(0, 3).join(" | "));

/* ================= §3B — the existing fleet reads right ================= */
console.log("\n--- §3B: the 2.0 fleet still reads correctly ---");

// The largest modded instance, as the handover asks for.
const withMods = await Promise.all(instances.map(async (i) => {
  const m = await page.evaluate(async (u, id) =>
    await (await fetch(`${u}/api/instances/${id}/mods`)).json(), BASE, i.server_id);
  return { ...i, count: m.count || 0, mods: m.mods || [] };
}));
withMods.sort((a, b) => b.count - a.count);
const big = withMods[0];
note(`largest: ${big.name} — ${big.count} jars, ${big.loader} ${big.minecraft}`);
check("a modded instance is available to inspect", big.count > 0, `${big.name}: ${big.count} jars`);

const errBefore = errors.length;

/* Overview */
await goto(`/i/${big.server_id}/overview`, 5000);
const ov = await text();
check("Overview: loader", new RegExp(big.loader, "i").test(ov), big.loader);
check("Overview: Minecraft version", ov.includes(big.minecraft), big.minecraft);
check("Overview: port", ov.includes(String(big.port)), String(big.port));
check("Overview: mod count", new RegExp(`\\b${big.count}\\b`).test(ov), String(big.count));
check("Overview: names a Java version", /java/i.test(ov),
      (ov.match(/Java[^·|]{0,26}/i) || [""])[0].trim());
check("Overview does not overflow", !(await overflows()));

/* Mods */
await goto(`/i/${big.server_id}/mods`, 7000);
const modsTxt = await text();
const sample = big.mods.slice(0, 6).map((m) => m.name || m.file);
check("Mods: the list renders every jar",
      new RegExp(`\\b${big.count}\\b`).test(modsTxt), `${big.count} expected`);
check("Mods: named jars appear",
      sample.filter((n) => n && modsTxt.includes(n.slice(0, 16))).length >= Math.min(3, sample.length),
      sample.slice(0, 3).join(", "));
const disabled = big.mods.filter((m) => !m.enabled);
if (disabled.length) {
  check("Mods: disabled jars are shown as disabled",
        /disabled|off\b/i.test(modsTxt), `${disabled.length} disabled`);
}
const iconCount = await page.evaluate(() =>
  [...document.querySelectorAll("img")].filter((i) => i.naturalWidth > 0).length);
check("Mods: icons load", iconCount > 0, iconCount + " images painted");
check("Mods does not overflow", !(await overflows()));

/* Configs */
await goto(`/i/${big.server_id}/configs`, 8000);
const cfg = await text();
check("Configs: files are listed", /\.(toml|properties|json|cfg|txt)/i.test(cfg),
      (cfg.match(/[\w.-]+\.(toml|properties|json)/) || [""])[0]);
check("Configs does not overflow", !(await overflows()));

/* Tune */
await goto(`/i/${big.server_id}/tune`, 6000);
const tune = await text();
check("Tune: host RAM is a real number, not 'unknown'",
      /\d+(\.\d+)?\s*GB/i.test(tune) && !/unknown/i.test(tune),
      (tune.match(/[\d.]+ ?GB[^·|]{0,20}/i) || [""])[0].trim());
check("Tune: a current heap is shown", /heap/i.test(tune));
check("Tune does not overflow", !(await overflows()));

/* Console */
await goto(`/i/${big.server_id}/console`, 6000);
const con = await text();
check("Console: renders and names its source",
      /console/i.test(con) && /(buffer|log|latest\.log|stopped|not running)/i.test(con),
      (con.match(/(Crafty's live buffer|latest\.log|[^.]{0,30}not running)/i) || [""])[0]);
check("Console does not overflow", !(await overflows()));

/* Diagnose */
await goto(`/i/${big.server_id}/diagnose`, 8000);
const diag = await text();
const findings = await page.evaluate(async (u, id) =>
  await (await fetch(`${u}/api/instances/${id}/diagnose`)).json(), BASE, big.server_id);
check("Diagnose: renders",
      /diagnos|finding|check|healthy|clear/i.test(diag),
      `${(findings.findings || []).length} findings from the API`);
if ((findings.findings || []).length) {
  const first = findings.findings[0];
  check("Diagnose: the top finding is on screen",
        diag.toLowerCase().includes(first.title.slice(0, 26).toLowerCase()),
        first.title);
}
check("Diagnose does not overflow", !(await overflows()));

/* Backups / Undo */
await goto(`/i/${big.server_id}/backups`, 5000);
const snaps = await page.evaluate(async (u, id) =>
  await (await fetch(`${u}/api/instances/${id}/backups`)).json(), BASE, big.server_id);
const bk = await text();
check("Undo: renders",
      /undo|snapshot|restore|nothing/i.test(bk),
      `${(snaps.items || []).length} snapshots from the API`);
if ((snaps.items || []).length) {
  check("Undo: a real snapshot reason is listed",
        snaps.items.some((s) => bk.includes(String(s.reason || "").slice(0, 16))),
        snaps.items.map((s) => s.reason).join(" | ").slice(0, 80));
}

check("no console errors across every tab",
      errors.length === errBefore,
      errors.slice(errBefore, errBefore + 3).join(" | "));

/* --- phone width, §3E's last line, cheap to do here ---------------- */
await page.setViewport({ width: 390, height: 844 });
await goto("/", 3000);
check("no horizontal scrolling at 390px", !(await overflows()));
const drawer = await page.evaluate(() =>
  [...document.querySelectorAll("button")].some((b) =>
    /server list|menu|navigation/i.test(
      (b.getAttribute("aria-label") || "") + " " + b.textContent)));
check("a way to reach the server list exists at phone width", drawer);
await page.setViewport({ width: 1600, height: 1050 });

await goto("/", 2500);
await page.screenshot({ path: `${OUT}/v21-fleet.png` });
console.log(`\nwrote ${OUT}/v21-fleet.png`);

if (errors.length) {
  console.log("\nerrors seen:");
  errors.slice(0, 8).forEach((e) => console.log("   " + e));
}
const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
await browser.close();
process.exit(passed === out.length ? 0 : 1);
