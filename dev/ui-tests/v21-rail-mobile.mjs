// The mobile rail drawer, at phone width, with touch.
//
// It opened and then could not be touched: the scrim was hung off `body`, so
// its z-index competed with #shell's 2 rather than with the rail's 60, and it
// painted over the open drawer and swallowed every tap. Hit-testing is the
// only way to catch that -- the drawer looks right in a screenshot.
//
// Read-only: it opens and closes a drawer and follows one nav link.
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
await page.setCacheEnabled(false);
await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Drive it through the toggle only. Poking `rail-open` directly used to be
// harmless; it is not any more, because opening also locks the page behind
// the drawer and removing one class of the pair leaves the body fixed with
// no way back.
const open = () => page.evaluate(() => {
  if (document.body.classList.contains("rail-open")) {
    document.querySelector("#railtoggle")?.click();
  }
  document.querySelector("#railtoggle")?.click();
});

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(7000);

check("the drawer is off-screen to begin with",
      await page.evaluate(() => /matrix.*-\d/.test(getComputedStyle(document.querySelector("#rail")).transform)));
check("a toggle is offered at phone width",
      await page.evaluate(() => !!document.querySelector("#railtoggle")?.offsetParent));

await open();
await sleep(900);
check("it opens", await page.evaluate(() => document.body.classList.contains("rail-open")
      && getComputedStyle(document.querySelector("#rail")).transform === "none"));

/* --- the bug: everything inside the drawer must be the topmost hit ------- */
const hits = await page.evaluate(() => {
  const rail = document.querySelector("#rail");
  const rb = rail.getBoundingClientRect();
  const at = (x, y) => {
    const el = document.elementFromPoint(x, y);
    return { inRail: el ? rail.contains(el) : false,
             hit: el ? `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}` : "null" };
  };
  return {
    top: at(rb.left + 40, rb.top + 30),
    nav: at(rb.left + 60, rb.top + 120),
    mid: at(rb.left + 40, rb.top + Math.min(300, rb.height - 40)),
    outside: at(Math.min(rb.right + 40, window.innerWidth - 5), 300),
  };
});
check("the top of the drawer is hit-testable", hits.top.inRail, hits.top.hit);
check("the nav block is hit-testable", hits.nav.inRail, hits.nav.hit);
check("the middle of the drawer is hit-testable", hits.mid.inRail, hits.mid.hit);
check("the scrim is what sits outside the drawer, not over it",
      !hits.outside.inRail, hits.outside.hit);

/* --- and a tap has to do something -------------------------------------- */
const tapped = await page.evaluate(async () => {
  const link = [...document.querySelectorAll("#rail a, #rail .navlink, #rail button")]
    .find((e) => /discover/i.test(e.textContent));
  if (!link) return { err: "no Discover row in the drawer" };
  const before = location.hash;
  link.click();
  await new Promise((r) => setTimeout(r, 1300));
  return { before, after: location.hash,
           closed: !document.body.classList.contains("rail-open") };
});
check("tapping a row navigates", !tapped.err && tapped.after !== tapped.before,
      tapped.err || `${tapped.before || "(none)"} -> ${tapped.after}`);
check("and the drawer closes itself on navigation", tapped.closed);

/* --- it can be dismissed without navigating ----------------------------- */
await open();
await sleep(800);
const scrimClose = await page.evaluate(async () => {
  const rail = document.querySelector("#rail");
  const rb = rail.getBoundingClientRect();
  const x = Math.min(rb.right + 40, window.innerWidth - 5);
  document.elementFromPoint(x, 300)?.click();
  await new Promise((r) => setTimeout(r, 600));
  return !document.body.classList.contains("rail-open");
});
check("tapping the scrim closes it", scrimClose);

await open();
await sleep(800);
await page.keyboard.press("Escape");
await sleep(600);
check("Escape closes it",
      await page.evaluate(() => !document.body.classList.contains("rail-open")));

check("no horizontal scrolling with the drawer open", await page.evaluate(async () => {
  document.querySelector("#railtoggle")?.click();
  await new Promise((r) => setTimeout(r, 700));
  const over = document.documentElement.scrollWidth > window.innerWidth + 1;
  document.querySelector("#railtoggle")?.click();
  return !over;
}));

/* THE DRAG, which a hit test cannot see.
   ------------------------------------------------------------
   Every check above is about a single POINT: is this pixel the
   topmost element, does this tap land. All of them passed while
   the drawer was still unusable on a real phone, because the
   fault was about MOVEMENT. The rail is a fixed overlay and is
   not itself a scroller, so a drag starting anywhere on it
   chained straight through to the document: the page slid away
   under the finger and the drawer sat still. That is what "the
   sidebar is frozen" looks like from the other side of the
   screen. */
const lock = await page.evaluate(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  window.scrollTo(0, 300);
  await sleep(200);
  const before = Math.round(window.scrollY);
  document.querySelector("#railtoggle")?.click();
  await sleep(500);
  const cs = getComputedStyle(document.body);
  const fixed = cs.position === "fixed";
  const offsetHeld = cs.top === `-${before}px`;
  // Try to move the page while the drawer is open.
  window.scrollTo(0, 2000);
  await sleep(200);
  const moved = Math.round(window.scrollY) !== 0;
  const railScrolls = (() => {
    const r = document.querySelector("#rail");
    const s = getComputedStyle(r);
    return s.overflowY === "auto" && s.overscrollBehaviorY === "contain";
  })();
  document.querySelector("#railtoggle")?.click();
  await sleep(500);
  return { before, fixed, offsetHeld, movedWhileOpen: moved, railScrolls,
           restored: Math.round(window.scrollY), released: getComputedStyle(document.body).position !== "fixed" };
});
check("the page behind the drawer is locked", lock.fixed);
check("...at the offset it was already at", lock.offsetHeld, `top should be -${lock.before}px`);
check("the page cannot be scrolled while the drawer is open",
      !lock.movedWhileOpen);
check("the rail scrolls itself, and does not chain out to the page",
      lock.railScrolls);
check("closing releases the lock", lock.released);
check("...and puts the page back where it was",
      lock.restored === lock.before, `${lock.restored} vs ${lock.before}`);

const taps = await page.evaluate(() => {
  document.querySelector("#railtoggle")?.click();
  const small = [...document.querySelectorAll("#rail a, #rail button")]
    .map((e) => ({ t: (e.textContent || e.getAttribute("aria-label") || "?").trim().slice(0, 20),
                   h: Math.round(e.getBoundingClientRect().height) }))
    .filter((x) => x.h > 0 && x.h < 44);
  document.querySelector("#railtoggle")?.click();
  return small;
});
check("every control in the drawer is a 44px touch target", taps.length === 0,
      taps.map((x) => `${x.t} ${x.h}px`).join(", "));

check("no uncaught page errors", errors.length === 0, errors.slice(0, 2).join(" | "));

await page.evaluate(() => document.querySelector("#railtoggle")?.click());
await sleep(700);
await page.screenshot({ path: `${OUT}/rail-mobile.png` });
const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
await browser.close();
process.exit(passed === out.length ? 0 : 1);
