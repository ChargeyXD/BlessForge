#!/usr/bin/env python3
"""Mod Roulette: the seed contract, the constraints, and the export.

The whole feature rests on one promise -- *the same seed and the same
constraints deal the same hand* -- so most of this file exists to hold that
promise down. It runs entirely offline against a synthetic pool; nothing here
touches CurseForge, Modrinth or Crafty.

The PRNG check matters more than it looks: the generator is a port of the one
in the design prototype, and the reference values below were produced by
running that original JavaScript. If someone "tidies up" the Python and the
numbers move, every seed anyone has ever shared stops reproducing, and
nothing else in the app would notice.
"""
import json
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app import roulette  # noqa: E402

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


# Captured from the original JavaScript implementation.
JS_REFERENCE = {
    "QRT-8KM-4Z": [0.852753914427, 0.175368672470, 0.799116301583],
    "": [0.416962620104, 0.928768132580, 0.707893693121],
    "a": [0.883859177353, 0.452489330899, 0.214312949916],
    "ZZZ-999-XX|slot|Create": [0.119184753392, 0.837607769063, 0.249317196431],
}


def fake_pool(n=400):
    """A synthetic catalogue: nine categories, a spread of sizes and ages."""
    cats = [c["key"] for c in roulette.CATEGORIES]
    mods = []
    for i in range(n):
        cat = cats[i % len(cats)]
        flag = None
        if i % 23 == 0:
            flag = "CLIENT"
        elif i % 17 == 0:
            flag = "CHAOS"
        elif i % 11 == 0:
            flag = "HEAVY"
        elif i % 13 == 0:
            flag = "CLIENT?"
        mods.append({
            "source": "curseforge" if i % 4 else "modrinth",
            "project_id": 1000 + i,
            "file_id": 50000 + i,
            "name": f"Test Mod {i:03d}",
            "slug": f"test-mod-{i}",
            "summary": "",
            "logo": None,
            "url": f"https://example.invalid/{i}",
            "downloads": (i % 40) * 1_000_000 + 50_000,
            "updated": "2026-06-01T00:00:00Z" if flag != "CHAOS" else "2023-01-01T00:00:00Z",
            "size": (35 if flag == "HEAVY" else 2) * 1024 * 1024,
            "file_name": f"testmod{i}.jar",
            "release_type": "release",
            "cf_categories": [], "mr_categories": [],
            "category": cat,
            "server_side": None,
            "flag": flag,
            "flag_why": {},
            "age_days": 30 if flag != "CHAOS" else 900,
        })
    return mods


def main():
    # --- the generator is a faithful port -------------------------------
    ok = True
    for seed, expected in JS_REFERENCE.items():
        r = roulette.prng(seed)
        got = [round(r(), 12) for _ in expected]
        if got != expected:
            ok = False
            print(f"    {seed!r}: expected {expected}, got {got}")
    check("the PRNG reproduces the original JavaScript exactly", ok,
          "a shared seed is worthless if these diverge")

    # --- seeds -----------------------------------------------------------
    minted = {roulette.mint_seed() for _ in range(200)}
    check("minted seeds match the documented shape",
          all(roulette.SEED_RE.match(s) for s in minted))
    check("minted seeds avoid the ambiguous glyphs",
          not any(ch in "ILO01" for s in minted for ch in s),
          "so they survive being read off a screen and typed back in")
    check("a typed seed is normalised, not rejected",
          roulette.normalise_seed("qrt8km4z") == "QRT-8KM-4Z",
          roulette.normalise_seed("qrt8km4z"))
    check("look-alike characters map onto the alphabet",
          roulette.normalise_seed("IOL-01O-AB") == roulette.normalise_seed("JQJ-Q2Q-AB"))
    check("an empty seed mints a valid one",
          roulette.SEED_RE.match(roulette.normalise_seed("")))

    pool = fake_pool()
    c = roulette.merge_constraints({
        "minecraft": "1.21.1", "loader": "NeoForge", "count": 40,
        "intensity": 3, "quality": 0,
        "categories": {"tech": roulette.PREFERRED, "magic": roulette.BANNED},
    })

    # --- the promise ------------------------------------------------------
    a = roulette.deal("QRT-8KM-4Z", pool, c)
    b = roulette.deal("QRT-8KM-4Z", pool, c)
    check("the same seed and constraints deal the same hand",
          [m["name"] for m in a] == [m["name"] for m in b], f"{len(a)} mods")
    other = roulette.deal("ZZZ-111-AA", pool, c)
    check("a different seed deals a different hand",
          [m["name"] for m in a] != [m["name"] for m in other])
    loose = roulette.deal("QRT-8KM-4Z", pool, dict(c, intensity=5))
    check("the same seed under different constraints deals differently",
          [m["name"] for m in a] != [m["name"] for m in loose],
          "constraints are part of the identity of a roll")

    # --- stability across a pool that has moved on ------------------------
    # This is the property the ticket-array approach did not have.
    grown = pool + fake_pool(60)[:0] + [dict(m, project_id=9000 + i,
                                             name=f"Newcomer {i}")
                                        for i, m in enumerate(fake_pool(30))]
    after = roulette.deal("QRT-8KM-4Z", grown, c)
    kept = len({m["name"] for m in a} & {m["name"] for m in after})
    check("adding mods to the pool disturbs a hand only marginally",
          kept >= len(a) * 0.6, f"{kept}/{len(a)} survived a 30-mod catalogue update")

    # --- constraints do what their labels say -----------------------------
    check("a banned category never appears",
          not any(m["category"] == "magic" for m in a))
    tech = sum(1 for m in a if m["category"] == "tech")
    check("a preferred category is weighted up, not made exclusive",
          tech > len(a) / len(roulette.CATEGORIES) and tech < len(a),
          f"{tech} of {len(a)} are tech")
    check("the hand is the requested size", len(a) == 40, str(len(a)))

    allowed = roulette.eligible(pool, c)
    check("client-only mods are excluded by default",
          not any(m["flag"] == "CLIENT" for m in allowed))
    check("unmaintained mods are excluded below intensity 4",
          not any(m["flag"] == "CHAOS" for m in allowed))
    wild = roulette.eligible(pool, roulette.merge_constraints(
        {"intensity": 5, "quality": 0, "toggles": {"conflict": True}}))
    check("...and allowed once you ask for them",
          any(m["flag"] == "CHAOS" for m in wild))

    floored = roulette.eligible(pool, roulette.merge_constraints({"quality": 20}))
    check("the quality floor drops everything below it",
          floored and all(m["downloads"] >= 20_000_000 for m in floored),
          f"{len(floored)} mods survive a 20M floor")
    check("a floor of 5 actually filters",
          len(roulette.eligible(pool, roulette.merge_constraints({"quality": 5})))
          < len(roulette.eligible(pool, roulette.merge_constraints({"quality": 0}))),
          "the design gated this on > 5, so 1-5 silently did nothing")

    # --- holds and single-slot rerolls ------------------------------------
    holds = [a[0]["name"], a[1]["name"]]
    held = roulette.deal("DIF-FER-NT", pool, c, holds)
    check("held mods survive the next pull",
          all(h in [m["name"] for m in held] for h in holds))
    check("a pull with holds still fills the hand", len(held) == 40, str(len(held)))

    swapped = roulette.reroll_one("QRT-8KM-4Z", a, a[5]["name"], pool, c)
    check("rerolling one slot replaces exactly one mod",
          len(swapped) == len(a)
          and len({m["name"] for m in swapped} ^ {m["name"] for m in a}) == 2,
          f"{a[5]['name']} -> {swapped[5]['name']}")
    check("a reroll is itself deterministic",
          [m["name"] for m in roulette.reroll_one("QRT-8KM-4Z", a, a[5]["name"], pool, c)]
          == [m["name"] for m in swapped])

    # --- the odds panel ---------------------------------------------------
    host = {"total_ram_gb": 11.6, "available_ram_gb": 6.8}
    clean = roulette.summarise([m for m in pool if not m["flag"]][:20], c, host)
    check("a clean hand reads as clean", clean["odds"]["tone"] == "good",
          clean["odds"]["title"])
    risky = roulette.summarise([m for m in pool if m["flag"] == "CHAOS"][:6], c, host)
    check("a hand full of unmaintained mods says so",
          risky["odds"]["tone"] == "bad", risky["odds"]["title"])
    fat = roulette.summarise([m for m in pool if m["flag"] == "HEAVY"][:40], c, host)
    check("a heavy hand warns about memory before it warns about anything else",
          fat["odds"]["tone"] in ("warn", "bad"), fat["odds"]["title"])
    check("the estimate is bounded by the host, not by the pack",
          clean["heap_ceiling_gb"] <= host["total_ram_gb"])

    # --- the export -------------------------------------------------------
    hand = a[:12]
    blob = roulette.build_export(hand, dict(c, seed="QRT-8KM-4Z"), "Roulette Test")
    z = zipfile.ZipFile(__import__("io").BytesIO(blob))
    names = z.namelist()
    check("the export is a CurseForge modpack",
          {"manifest.json", "modlist.html"} <= set(names), ", ".join(names))
    manifest = json.loads(z.read("manifest.json"))
    check("the manifest declares the pack type",
          manifest["manifestType"] == "minecraftModpack")
    check("the manifest names the runtime",
          manifest["minecraft"]["version"] == "1.21.1"
          and manifest["minecraft"]["modLoaders"][0]["id"] == "neoforge",
          json.dumps(manifest["minecraft"]))
    cf = [m for m in hand if m["source"] == "curseforge"]
    check("every CurseForge mod is listed as a project/file pair",
          len(manifest["files"]) == len(cf)
          and all({"projectID", "fileID", "required"} <= set(f) for f in manifest["files"]),
          f"{len(manifest['files'])} files")
    check("Modrinth mods are bundled rather than faked into the manifest",
          len(manifest["files"]) < len(hand),
          "they have no CurseForge project id to reference")
    bundled = roulette.build_export(
        hand, dict(c, seed="X"), "T", {"extra.jar": b"PK\x03\x04stub"})
    check("bundled jars land where a launcher expects them",
          "overrides/mods/extra.jar" in zipfile.ZipFile(
              __import__("io").BytesIO(bundled)).namelist())
    check("the seed is recorded in the archive",
          "QRT-8KM-4Z" in z.read("README.txt").decode())

    # --- the export round-trips through this app's own importer ----------
    from app import packs
    plan = packs.analyse_archive(z)
    check("BlessForge can re-import the pack it just wrote",
          plan.source == "manifest" and len(plan.manifest_files) == len(cf),
          f"{plan.loader} {plan.mc_version}, {len(plan.manifest_files)} mods")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


sys.exit(main())
