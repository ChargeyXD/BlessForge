// The Situation screen.
//
// Crafty has no servers on this machine right now, so the instance payloads
// are fixtures -- but they are shaped from the real contract (Crafty's own
// ServerStats columns, and the `state` / `java` / `uptime_s` blocks
// /api/instances/{id} computes), and everything else in the page is the live
// backend. Point BF_SERVER_ID at a real instance and the reads pass straight
// through instead; the writes stay intercepted either way, so this never
// starts, stops or deletes anything.
import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const LIVE = process.env.BF_SERVER_ID || "";
const SID  = LIVE || "11112222-3333-4444-5555-666677778888";

const utcAgo = (s) => new Date(Date.now() - s * 1000)
  .toISOString().replace("T", " ").slice(0, 19);

const mods = (n) => Array.from({ length: n }, (_, i) => ({ file: `mod-${i}.jar` }));

const RUNNING = {
  server: {
    server_id: SID, server_name: "Hollow Depths", server_port: 25571,
    path: `/crafty/servers/${SID}`, executable: "neoforge-21.1.95-server.jar",
    execution_command: "/usr/lib/jvm/java-17-openjdk-amd64/bin/java -Xms2G -Xmx4G nogui",
    auto_start: true, created: "2026-08-12 09:14:02", type: "minecraft-java",
  },
  manifest: {
    schema: 1, complete: true, installed_at: Date.now() / 1000 - 86400 * 17,
    pack: { source: "curseforge", install_source: "curseforge",
            name: "Enhanced Terrain", version: "1.4.2" },
    minecraft: "1.21.1", loader: "neoforge", loader_version: "21.1.95",
    mods: mods(91),
  },
  stats: {
    running: true, crashed: false, cpu: 37.4, mem: "3.1GB", mem_percent: 41,
    online: 2, max: 20, world_size: "1.4GB", world_name: "world",
    version: "1.21.1", desc: "A BlessForge server", started: utcAgo(15120),
  },
  state: "running", uptime_s: 15120,
  // Java 17 under a 1.21.1 pack: the exact fault §5.5 hit on ATMons.
  java: { path: "/usr/lib/jvm/java-17-openjdk-amd64/bin/java", major: 17,
          pinned: true, minecraft: "1.21.1", required: 21, ok: false },
};

const STOPPED = {
  server: {
    server_id: SID, server_name: "Relic <img src=x onerror=alert(1)>", server_port: 25565,
    path: `/crafty/servers/${SID}`, executable: "server.jar",
    execution_command: "java -Xmx4G -jar server.jar nogui",
    auto_start: false, created: "2026-05-02 20:00:00", type: "minecraft-java",
  },
  manifest: {},
  stats: { running: false, crashed: false, cpu: 0, mem: "0", mem_percent: 0,
           online: 0, max: 0, world_size: "410MB", world_name: "world",
           desc: "Unable to Connect", started: false },
  state: "stopped", uptime_s: null,
  java: { path: "java", major: null, pinned: false, minecraft: "1.21.1",
          required: 21, ok: null },
};

const DIAG = {
  mod_count: 91,
  findings: [
    { severity: "warning", category: "mods", title: "3 client-only jars are enabled",
      detail: "They waste memory and can crash a dedicated server.",
      fix: { action: "find_client_only" }, evidence: "" },
    { severity: "critical", category: "runtime",
      title: "Cobblemon needs Java 21; this server launches with 17",
      detail: "The loader refuses to load it.",
      fix: { action: "set_java", java_major: 21 }, evidence: "line 4412" },
    { severity: "info", category: "general", title: "No crash report on disk",
      detail: "Nothing to attribute.", fix: null, evidence: "" },
  ],
};
const DIAG_CLEAN = { mod_count: 0, findings: [] };

const PORT_BAD = { crafty_port: 25571, properties_port: 25565, query_port: 25565,
                   in_use_by_others: [], published_range: [25500, 25600], mismatch: true,
                   note: "Crafty's record and server.properties disagree." };
const PORT_OK  = { ...PORT_BAD, properties_port: 25571, mismatch: false, note: null };

// What the screen is currently pretending to look at.
let FIX = RUNNING, DIA = DIAG, PRT = PORT_BAD;
const writes = [];
let statsCalls = 0;

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e.stack || e.message)));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const html = fs.readFileSync("/static/index.html", "utf8");
const appjs = fs.readFileSync("/static/app.js", "utf8");
const dom = new JSDOM(html.replace(/<script src="\/static\/app.js"><\/script>/, ""),
  { url: BASE + "/", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: vc });
const { window } = dom;
window.EventSource = class { constructor(u) { this.url = u; } close() {} };
window.CSS = { escape: (s) => String(s).replace(/["\\]/g, "\\$&") };
window.scrollTo = () => {};
window.HTMLElement.prototype.scrollIntoView = () => {};

// jsdom ships no Response constructor; api() only ever touches these.
const json = (o) => ({ ok: true, status: 200, statusText: "OK", json: async () => o });

window.fetch = (url, opts = {}) => {
  const u = String(url);
  const method = (opts.method || "GET").toUpperCase();
  const path = u.startsWith("http") ? new URL(u).pathname + new URL(u).search : u;

  // Nothing this harness does may reach a real server.
  if (method !== "GET" && path.includes(`/api/instances/${SID}`)) {
    writes.push({ method, path, body: opts.body ? JSON.parse(opts.body) : null });
    return Promise.resolve(json({ ok: true, action: path.split("/").pop(),
                                  prepared: { java: "/usr/lib/jvm/java-21-openjdk-amd64/bin/java" } }));
  }
  if (!LIVE) {
    if (path === `/api/instances/${SID}`) return Promise.resolve(json(FIX));
    if (path === `/api/instances/${SID}/stats`) {
      statsCalls++;
      return Promise.resolve(json({ ...FIX.stats, uptime_s: FIX.uptime_s }));
    }
    if (path === `/api/instances/${SID}/diagnose`) return Promise.resolve(json(DIA));
    if (path === `/api/instances/${SID}/port`) return Promise.resolve(json(PRT));
  } else if (path === `/api/instances/${SID}/stats`) {
    statsCalls++;
  }
  return globalThis.fetch(u.startsWith("http") ? u : BASE + path, opts);
};

const s = window.document.createElement("script");
s.textContent = appjs;
window.document.body.appendChild(s);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (q, r = window.document) => r.querySelector(q);
const $$ = (q, r = window.document) => [...r.querySelectorAll(q)];
const txt = (q) => ($(q) ? $(q).textContent.replace(/\s+/g, " ").trim() : "");
const out = [];
const check = (n, c, extra = "") => {
  out.push(!!c); console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + extra : ""}`);
};

await sleep(6000);
check("app booted", !!$(".spine") && !!$(".canvas"));

/* --- a running server ---------------------------------------------------- */
await window.__bf.openInstance(SID);
await sleep(1200);

check("situation renders", !!$(".pow") && !!$(".needs") && !!$(".sit-trio"));
check("state and uptime in the rail", /RUNNING/.test(txt(".pow-state")) && /UP 4h 12m/.test(txt(".pow-state")),
      txt(".pow-state"));
check("headline states the state", txt(".pow-head h2").length > 0, txt(".pow-head h2"));
check("blurb is built from facts, not invented",
      /2 of 20 players connected/.test(txt(".pow-head p")) && !/TPS/.test(txt(".pow-head p")),
      txt(".pow-head p"));

const tiles = $$(".pow .stat");
check("four stat tiles", tiles.length === 4, tiles.map((t) => $(".k", t).textContent).join(", "));
check("no TPS tile — nothing measures it",
      !/TPS/i.test($(".pow .stats").textContent));
check("players tile shows online/max", $(".v", tiles[0]).textContent.trim() === "2/20",
      $(".v", tiles[0]).textContent.trim());
check("cpu tile is a percentage", $(".v", tiles[1]).textContent.trim() === "37%",
      $(".v", tiles[1]).textContent.trim());
const memBar = $(".bar > i", tiles[2]);
check("memory tile shows Crafty's own string", $(".v", tiles[2]).textContent.trim() === "3.1GB");
check("memory bar uses mem_percent, not the string",
      memBar && memBar.getAttribute("style").includes("width:41%"),
      memBar ? memBar.getAttribute("style") : "no bar");
check("memory is not labelled heap", !/heap/i.test($(".k", tiles[2]).textContent));
check("fourth tile is the world on disk",
      $(".v", tiles[3]).textContent.trim() === "1.4GB");

const acts = Object.fromEntries($$(".pow-acts [data-act]").map((b) => [b.dataset.act, b]));
check("start is disabled while running", acts.start_server.disabled);
check("restart is the primary action while running",
      !acts.restart_server.disabled && acts.restart_server.classList.contains("primary"));
check("stop is offered", !acts.stop_server.disabled);
check("force kill is offered only while running", !!acts.kill_server);

/* --- facts --------------------------------------------------------------- */
const facts = Object.fromEntries($$(".facts dt").map((d, i) => [d.textContent.trim(), $$(".facts dd")[i]]));
check("facts name the java in the launch command", /17/.test(facts.java.textContent), facts.java.textContent.trim());
check("a wrong java says what it needs", /needs 21/.test(facts.java.textContent), facts.java.textContent.trim());
check("a wrong java is red", /red-soft/.test(facts.java.getAttribute("style") || ""));
check("data dir is Crafty's path, not /srv/minecraft",
      facts["data dir"].textContent.includes("/crafty/servers/"), facts["data dir"].textContent.trim());
check("jar is the executable's basename", facts.jar.textContent.trim() === "neoforge-21.1.95-server.jar");
check("loader carries its build", facts.loader.textContent.trim() === "neoforge 21.1.95");

/* --- needs you ----------------------------------------------------------- */
const needs = $$("#needsBody .need");
check("needs list populated", needs.length >= 4, needs.length + " rows");
check("critical sorts first", needs[0].classList.contains("crit"),
      needs[0].textContent.replace(/\s+/g, " ").trim().slice(0, 60));
check("a port disagreement is raised here",
      $("#needsBody").textContent.includes("25571") && $("#needsBody").textContent.includes("25565"));
check("severity and category are stated on each row",
      /CRITICAL · RUNTIME/i.test($(".sub", needs[0]).textContent.replace(/\s+/g, " ")),
      $(".sub", needs[0]).textContent.trim());

const modsRow = needs.find((n) => /client-only/i.test(n.textContent));
modsRow.click();
await sleep(500);
check("a mod finding opens the Mods tab", window.__bf.state.tab === "mods", window.__bf.state.tab);
await window.__bf.go("instance", "situation");
await sleep(1200);
const needs2 = $$("#needsBody .need");
const javaRow = needs2.find((n) => /Java 21/.test(n.textContent));
javaRow.click();
await sleep(400);
check("a java finding opens Tune", window.__bf.state.tab === "tune", window.__bf.state.tab);

/* --- installed pack ------------------------------------------------------ */
await window.__bf.go("instance", "situation");
await sleep(1200);
check("pack card names the pack", txt(".packname") === "Enhanced Terrain", txt(".packname"));
check("pack card counts its mods", /91 mods/.test($$(".packmeta").map((e) => e.textContent).join(" ")));
check("pack card warns what a switch replaces",
      /keeps the world and replaces every mod/.test($(".sit-trio").textContent));

/* --- removal ------------------------------------------------------------- */
$('[data-act="forget"]').click();
await sleep(400);
check("forgetting asks first", !!$(".veil"));
check("forgetting does not demand the name be typed", !$("#typeGate"));
$$(".sheet footer .btn").pop().click();
await sleep(600);
check("forgetting sends files=false",
      writes.some((w) => w.method === "DELETE" && /files=false/.test(w.path)),
      JSON.stringify(writes.slice(-1)));

await window.__bf.openInstance(SID);
await sleep(1200);
$('[data-act="destroy"]').click();
await sleep(400);
const gate = $("#typeGate");
const goBtn = $$(".sheet footer .btn").pop();
check("deleting the world demands the name", !!gate);
check("its confirm starts disabled", goBtn.disabled);
gate.value = "Hollow Depth";
gate.dispatchEvent(new window.Event("input", { bubbles: true }));
await sleep(120);
check("a near-miss does not unlock it", goBtn.disabled, "typed 'Hollow Depth'");
gate.value = "Hollow Depths";
gate.dispatchEvent(new window.Event("input", { bubbles: true }));
await sleep(120);
check("the exact name unlocks it", !goBtn.disabled);
const before = writes.length;
goBtn.click();
await sleep(800);
check("deleting sends files=true",
      writes.slice(before).some((w) => w.method === "DELETE" && /files=true/.test(w.path)),
      JSON.stringify(writes.slice(before)));

/* --- power actions ------------------------------------------------------- */
await window.__bf.openInstance(SID);
await sleep(1200);
const n0 = writes.length;
$('[data-act="kill_server"]').click();
await sleep(400);
check("force kill asks first", !!$(".veil"));
$$(".sheet footer .btn")[0].click();          // "Keep it"
await sleep(400);
check("declining a kill sends nothing", writes.length === n0);

$('[data-act="stop_server"]').click();
await sleep(700);
check("stop posts the crafty action",
      writes.some((w) => w.method === "POST" && w.path.endsWith("/action/stop_server")));
check("what _prepare_for_start silently fixed is reported",
      /re-pinned/i.test($("#toasts").textContent), txt("#toasts").slice(0, 90));

/* --- a stopped, unmanaged server ----------------------------------------- */
if (!LIVE) {
  FIX = STOPPED; DIA = DIAG_CLEAN; PRT = PORT_OK;
  await window.__bf.openInstance(SID);
  await sleep(1200);
  check("a stopped server offers Start as the primary action",
        $('[data-act="start_server"]').classList.contains("primary")
        && !$('[data-act="start_server"]').disabled);
  check("stop and restart are disabled while stopped",
        $('[data-act="stop_server"]').disabled && $('[data-act="restart_server"]').disabled);
  check("no force kill on a stopped server", !$('[data-act="kill_server"]'));
  check("live tiles read as unknown, not zero",
        $$(".pow .stat .v").slice(0, 3).every((v) => v.textContent.trim() === "—"),
        $$(".pow .stat .v").map((v) => v.textContent.trim()).join(" | "));
  check("the world tile still has a number", $$(".pow .stat .v")[3].textContent.trim() === "410MB");
  check("an unpinned java is unknown, not wrong",
        /container default/.test($(".facts").textContent)
        && !/red-soft/.test($$(".facts dd")[4].getAttribute("style") || ""),
        $$(".facts dd")[4].textContent.trim());
  check("an unmanaged server says so", /Not installed by BlessForge/.test(txt(".packname")));
  check("an unmanaged server counts the jars it found",
        /0 jars found on disk/.test($(".sit-trio").textContent));
  check("no switch/re-import buttons without a pack", !$("[data-go]"));
  check("a clean instance says nothing needs you",
        /Nothing needs you/.test($("#needsBody").textContent));
  check("a server name containing markup is escaped, not injected",
        !$(".sit-trio img") && !$(".pow img")
        && $(".wipe").ownerDocument.querySelectorAll("img[onerror]").length === 0);
}

/* --- teardown ------------------------------------------------------------ */
FIX = RUNNING; DIA = DIAG; PRT = PORT_BAD;
await window.__bf.openInstance(SID);
await sleep(1200);
const t0 = statsCalls;
await sleep(7000);
check("a running screen ticks its own numbers", statsCalls > t0, `${statsCalls - t0} polls in 7s`);
await window.__bf.go("discover", "roulette");
await sleep(500);
const t1 = statsCalls;
await sleep(7000);
check("leaving the screen stops the poll", statsCalls === t1,
      `${statsCalls - t1} polls after leaving — an interval left running polls Crafty forever`);

check("no uncaught page errors", errors.length === 0, errors.slice(0, 2).join(" | "));

const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
process.exit(passed === out.length ? 0 : 1);
