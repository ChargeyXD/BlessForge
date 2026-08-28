// jsdom applies no CSS, so it cannot see a layout break. This drives real
// Chrome and reports the things only a rendering engine knows: whether the
// page overflows sideways, whether anything collapsed to zero, and what the
// tokens actually resolved to.
//
// Crafty has no servers on this machine, so the instance screens are shot
// against an intercepted fixture -- the same shape /api/instances/{id}
// returns. Everything else is the live app.
import puppeteer from "puppeteer";
const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const SID = "11112222-3333-4444-5555-666677778888";
const utcAgo = (s) => new Date(Date.now() - s * 1000).toISOString().replace("T", " ").slice(0, 19);

const INSTANCE = {
  server: { server_id: SID, server_name: "Hollow Depths", server_port: 25571,
    path: `/crafty/servers/${SID}`, executable: "neoforge-21.1.95-server.jar",
    execution_command: "/usr/lib/jvm/java-17-openjdk-amd64/bin/java -Xms2G nogui",
    auto_start: true, created: "2026-08-12 09:14:02" },
  manifest: { schema: 1, complete: true, installed_at: Date.now() / 1000 - 86400 * 17,
    pack: { source: "curseforge", name: "Enhanced Terrain", version: "1.4.2" },
    minecraft: "1.21.1", loader: "neoforge", loader_version: "21.1.95",
    mods: Array.from({ length: 91 }, (_, i) => ({ file: `m${i}.jar` })) },
  stats: { running: true, crashed: false, cpu: 37.4, mem: "3.1GB", mem_percent: 41,
    online: 2, max: 20, world_size: "1.4GB", world_name: "world", version: "1.21.1",
    desc: "A BlessForge server", started: utcAgo(15120) },
  state: "running", uptime_s: 15120,
  java: { path: "/usr/lib/jvm/java-17-openjdk-amd64/bin/java", major: 17, pinned: true,
    minecraft: "1.21.1", required: 21, ok: false },
};
const FLEET = { items: [
  { server_id: SID, name: "Hollow Depths", port: 25571, state: "running",
    pack: { name: "Enhanced Terrain", version: "1.4.2" }, minecraft: "1.21.1",
    loader: "neoforge", managed: true, cpu: 37.4, mem: 41, players: 2, max_players: 20 },
  { server_id: "b".repeat(32), name: "Cottage Witch", port: 25572, state: "crashed",
    pack: { name: "Cottage Witch" }, minecraft: "1.20.1", loader: "forge", managed: true },
  { server_id: "c".repeat(32), name: "Relic Test", port: 25573, state: "orphan",
    minecraft: "1.21.1", loader: "fabric", managed: false },
] };
const DIAG = { mod_count: 91, findings: [
  { severity: "critical", category: "runtime",
    title: "Cobblemon needs Java 21; this server launches with 17",
    detail: "The loader refuses to load it.", fix: { action: "set_java" }, evidence: "" },
  { severity: "warning", category: "mods", title: "3 client-only jars are enabled",
    detail: "Wasted memory, possible crash.", fix: { action: "find_client_only" }, evidence: "" },
  { severity: "info", category: "general", title: "No crash report on disk",
    detail: "Nothing to attribute.", fix: null, evidence: "" },
] };
const PORT = { crafty_port: 25571, properties_port: 25565, query_port: 25565,
  in_use_by_others: [], published_range: [25500, 25600], mismatch: true,
  note: "Crafty's record and server.properties disagree." };

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
const msgs = [];
page.on("console", (m) => { if (m.type() === "error") msgs.push(`[console] ${m.text()}`); });
page.on("pageerror", (e) => msgs.push(`[pageerror] ${e.message}`));
page.on("requestfailed", (r) => msgs.push(`[reqfail] ${r.url()} ${r.failure()?.errorText}`));

await page.setRequestInterception(true);
page.on("request", (req) => {
  const p = new URL(req.url()).pathname;
  const send = (o) => req.respond({ status: 200, contentType: "application/json",
                                    body: JSON.stringify(o) });
  if (p === "/api/instances") return send(FLEET);
  if (p === `/api/instances/${SID}`) return send(INSTANCE);
  if (p === `/api/instances/${SID}/stats`) return send({ ...INSTANCE.stats, uptime_s: INSTANCE.uptime_s });
  if (p === `/api/instances/${SID}/diagnose`) return send(DIAG);
  if (p === `/api/instances/${SID}/port`) return send(PORT);
  req.continue();
});

const probe = () => page.evaluate(() => {
  const r = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().height) : null; };
  return {
    overflowsSideways: document.documentElement.scrollWidth > window.innerWidth + 1,
    scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    ember: getComputedStyle(document.documentElement).getPropertyValue("--ember").trim(),
    spineW: Math.round(document.querySelector(".spine")?.getBoundingClientRect().width || 0),
    powH: r(".pow"), needsH: r(".needs"), trioH: r(".sit-trio"),
    tiles: document.querySelectorAll(".pow .stat").length,
    needRows: document.querySelectorAll("#needsBody .need").length,
    zeroHeight: [...document.querySelectorAll(".card, .stat, .need, .wipe")]
      .filter((e) => e.getBoundingClientRect().height < 4)
      .map((e) => e.className).slice(0, 5),
  };
});

const shots = [];
async function shoot(name, w, h, before) {
  await page.setViewport({ width: w, height: h });
  if (before) await before();
  await new Promise((r) => setTimeout(r, 1400));
  await page.screenshot({ path: name + ".png", fullPage: false });
  shots.push([name, await probe()]);
}

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await new Promise((r) => setTimeout(r, 5000));
await shoot("shot-1-roulette", 1440, 900);
await shoot("shot-2-situation", 1440, 900,
  () => page.evaluate((id) => window.__bf.openInstance(id), SID));
await shoot("shot-3-situation-1000", 1000, 900);
await shoot("shot-4-situation-560", 560, 900);
await shoot("shot-5-situation-360", 360, 780);

for (const [name, info] of shots) console.log(name, JSON.stringify(info));
console.log("CONSOLE:", msgs.length ? msgs.slice(0, 12).join("\n  ") : "clean");
await browser.close();
