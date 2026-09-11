// BlessForge 2.1 — HANDOVERCASAOS.md §3C, the new features, in a real browser.
//
// File manager, Players, the client-only scan and Roulette. This one WRITES:
// it edits a config on the instance named by BF_SID, saves it, and reverts to
// the original bytes afterwards. Point it at a throwaway instance.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const OUT = process.env.BF_OUT || "/out";
const SID = process.env.BF_SID;
const out = [];
const check = (n, c, extra = "") => {
  out.push(!!c);
  console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + String(extra).slice(0, 130) : ""}`);
};
const note = (s) => console.log(`      ${s}`);

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setCacheEnabled(false);
await page.setViewport({ width: 1600, height: 1050 });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => page.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
const goto = async (hash, wait = 4000) => {
  await page.evaluate((h) => { location.hash = h; }, hash);
  await sleep(wait);
};
const api = (p, opts) => page.evaluate(async (u, o) => {
  const r = await fetch(u, o ? { method: o.method || "POST",
    headers: { "Content-Type": "application/json" },
    body: o.body ? JSON.stringify(o.body) : undefined } : undefined);
  const t = await r.text();
  try { return JSON.parse(t); } catch { return { _status: r.status, _text: t.slice(0, 200) }; }
}, BASE + p, opts || null);
const clickText = (t) => page.evaluate((t) => {
  const el = [...document.querySelectorAll("button,a,[role=button]")]
    .filter((e) => e.offsetParent)
    .find((e) => (e.textContent || "").replace(/\s+/g, " ").trim().toLowerCase()
                   .includes(t.toLowerCase()));
  if (el) { el.click(); return true; }
  return false;
}, t);

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(6000);

/* ================= file manager ================= */
console.log("--- §3C: file manager ---");

const usage = await api(`/api/instances/${SID}/files/usage`);
const folders = usage.folders || (Array.isArray(usage) ? usage : []);
note(folders.map((f) => `${f.name} ${f.size}`).join(", "));

await goto(`/i/${SID}/files`, 4000);
check("the listing appears without waiting for folder sizes",
      /server\.properties|NAME/i.test(await text()),
      "listing up at 4s");
await sleep(16000);   // the sidebar sizes the tree in the background
const filesTxt = await text();
check("sidebar shows real folder sizes",
      folders.slice(0, 3).every((f) => filesTxt.includes(f.name)) && /\d+(\.\d+)? ?[KMG]B/.test(filesTxt),
      folders.slice(0, 3).map((f) => `${f.name} ${f.size}`).join(" · "));

// Navigate into config/ and back up.
const intoConfig = await page.evaluate(() => {
  const row = [...document.querySelectorAll("button,a,tr,li,div")]
    .filter((e) => e.offsetParent)
    .find((e) => (e.textContent || "").trim().startsWith("config")
                 && e.getBoundingClientRect().height < 90);
  if (row) { row.click(); return true; }
  return false;
});
await sleep(3500);
check("navigating into config/ works",
      intoConfig && await page.evaluate(() => /config/.test(location.hash)
        || !!document.querySelector("[class*=crumb]")),
      "hash " + await page.evaluate(() => location.hash));
const crumbs = (await api(`/api/instances/${SID}/files?path=config`)).crumbs || [];
check("breadcrumbs describe the path",
      crumbs.length >= 2 && crumbs[crumbs.length - 1].path === "config",
      crumbs.map((c) => c.name).join(" / "));

/* --- the flagged bug: server.properties save must confirm AND land ------- */
const original = await api(`/api/instances/${SID}/files/read?path=server.properties`);
const body0 = original.content ?? original.text ?? "";
check("server.properties reads back", typeof body0 === "string" && body0.length > 0,
      `${body0.length} bytes`);

const marker = `# bf21-probe-${Date.now()}`;
const saved = await api(`/api/instances/${SID}/files/write`, {
  body: { path: "server.properties", content: body0 + "\n" + marker + "\n" },
});
check("the write endpoint accepts server.properties",
      !saved._status || saved._status < 400, JSON.stringify(saved).slice(0, 110));

const reread = await api(`/api/instances/${SID}/files/read?path=server.properties`);
const body1 = reread.content ?? reread.text ?? "";
check("the save actually landed (reopened and checked)",
      body1.includes(marker), body1.includes(marker) ? "marker present" : "marker MISSING");

const snaps = await api(`/api/instances/${SID}/backups`);
check("the save took a snapshot",
      (snaps.items || []).some((s) => /server\.properties|before editing/i.test(s.reason || "")),
      (snaps.items || []).slice(0, 2).map((s) => s.reason).join(" | "));

// Put it back exactly as it was.
await api(`/api/instances/${SID}/files/write`, { body: { path: "server.properties", content: body0 } });
const body2 = (await api(`/api/instances/${SID}/files/read?path=server.properties`)).content ?? "";
check("reverted to the original bytes", body2 === body0,
      body2 === body0 ? `${body0.length} bytes restored` : "DID NOT REVERT");

/* --- the confirmation dialog is real and resolves true ------------------- */
// Driven through the UI, because the bug that was fixed lived in the dialog's
// promise, not in the endpoint.
await goto(`/i/${SID}/files`, 5000);
const dialogWorks = await page.evaluate(async () => {
  const mod = await import("/static/js/core.js");
  // Resolve true: click the confirm button once the dialog is up.
  const p = mod.confirmDialog({ title: "probe", message: "probe", confirmLabel: "Yes" });
  await new Promise((r) => setTimeout(r, 400));
  const btns = [...document.querySelectorAll(".modal button, dialog button, [class*=modal] button")];
  const yes = btns.find((b) => /yes/i.test(b.textContent));
  if (!yes) return { ok: false, why: "no confirm button rendered" };
  yes.click();
  const resolved = await Promise.race([p, new Promise((r) => setTimeout(() => r("TIMEOUT"), 2500))]);
  return { ok: resolved === true, resolved: String(resolved) };
});
check("confirmDialog resolves TRUE when confirmed",
      dialogWorks.ok, dialogWorks.why || `resolved ${dialogWorks.resolved}`);

const dialogCancels = await page.evaluate(async () => {
  const mod = await import("/static/js/core.js");
  const p = mod.confirmDialog({ title: "probe", message: "probe", confirmLabel: "Yes" });
  await new Promise((r) => setTimeout(r, 400));
  const btns = [...document.querySelectorAll(".modal button, dialog button, [class*=modal] button")];
  const no = btns.find((b) => /cancel/i.test(b.textContent));
  if (!no) return { ok: false, why: "no cancel button" };
  no.click();
  const resolved = await Promise.race([p, new Promise((r) => setTimeout(() => r("TIMEOUT"), 2500))]);
  return { ok: resolved === false, resolved: String(resolved) };
});
check("and FALSE when cancelled", dialogCancels.ok,
      dialogCancels.why || `resolved ${dialogCancels.resolved}`);

const guard = await page.evaluate(async () => {
  const mod = await import("/static/js/core.js");
  const p = mod.confirmDialog({ title: "probe", message: "probe", requireText: "DELETE" });
  await new Promise((r) => setTimeout(r, 400));
  const btns = [...document.querySelectorAll(".modal button, dialog button, [class*=modal] button")];
  const ok = btns.find((b) => b.classList.contains("primary") || b.classList.contains("danger"));
  const disabledBefore = ok ? ok.disabled : null;
  const input = document.querySelector(".modal input, dialog input, [class*=modal] input");
  if (input) {
    input.value = "DELETE";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 200));
  }
  const enabledAfter = ok ? !ok.disabled : null;
  const cancel = btns.find((b) => /cancel/i.test(b.textContent));
  if (cancel) cancel.click();
  await p.catch(() => {});
  return { disabledBefore, enabledAfter, hadInput: !!input };
});
check("a type-to-confirm guard starts disabled and unlocks on the exact word",
      guard.hadInput && guard.disabledBefore === true && guard.enabledAfter === true,
      JSON.stringify(guard));

/* --- protected folders ---------------------------------------------------- */
const protectedNames = folders.filter((f) => f.protected).map((f) => f.name);
check("world and libraries are marked protected",
      protectedNames.includes("world") && protectedNames.includes("libraries"),
      protectedNames.join(", "));

/* --- search, download, upload -------------------------------------------- */
const found = await api(`/api/instances/${SID}/files/search?q=server.properties`);
const hits = found.items || found.results || [];
check("search returns hits with paths", hits.length > 0 && hits.some((h) => (h.path || "").includes("server.properties")),
      hits.slice(0, 2).map((h) => h.path).join(", "));

const dl = await page.evaluate(async (u) => {
  const r = await fetch(u);
  const t = await r.text();
  return { status: r.status, bytes: t.length, head: t.slice(0, 40) };
}, `${BASE}/api/instances/${SID}/files/download?path=server.properties`);
check("download returns the file intact", dl.status === 200 && dl.bytes === body0.length,
      `${dl.bytes} bytes (file is ${body0.length})`);

const probeName = `bf21-probe-${Date.now()}.txt`;
const created = await api(`/api/instances/${SID}/files/create`,
  { body: { parent: "config", name: probeName, directory: false } });
const listing = await api(`/api/instances/${SID}/files?path=config`);
const names = (listing.entries || []).map((e) => e.name);
check("a new file appears in config/", names.includes(probeName),
      created._status ? JSON.stringify(created).slice(0, 90) : `${names.length} entries`);
await api(`/api/instances/${SID}/files/delete`, { body: { paths: [`config/${probeName}`] } });

/* ================= client-only scan ================= */
console.log("\n--- §3C: client-only scan ---");
const modsNow = await api(`/api/instances/${SID}/mods`);
const scanJob = await api(`/api/instances/${SID}/client-scan`, { body: {} });
let scan = null;
if (scanJob.job_id) {
  for (let i = 0; i < 60; i++) {
    await sleep(5000);
    const j = await api(`/api/jobs/${scanJob.job_id}`);
    if (j.status && j.status !== "running") { scan = j.result; break; }
  }
} else {
  scan = scanJob;
}
if (scan) {
  const sum = scan.summary || scan;
  const buckets = ["client", "review", "protected", "server"].map((k) => [k, sum[k] ?? 0]);
  const total = buckets.reduce((a, [, v]) => a + v, 0);
  note(`${buckets.map(([k, v]) => `${k} ${v}`).join(", ")}  (total ${sum.total}) vs ${modsNow.count} jars`);
  check("the scan finished and reported every jar",
        sum.total === modsNow.count, `${sum.total} scanned vs ${modsNow.count} installed`);
  check("the four buckets add up to the total",
        total === sum.total, `${total} vs ${sum.total}`);
} else {
  check("the scan finished", false, "no result — " + JSON.stringify(scanJob).slice(0, 110));
}

/* ================= roulette ================= */
console.log("\n--- §3C: roulette ---");
await goto("/roulette", 6000);
const rou = await text();
check("the roulette screen renders", /roulette|lever|pull|seed/i.test(rou));
check("no console errors on the new screens", errors.length === 0, errors.slice(0, 3).join(" | "));

await page.screenshot({ path: `${OUT}/v21-features.png` });
const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
await browser.close();
process.exit(passed === out.length ? 0 : 1);
