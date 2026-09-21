#!/usr/bin/env python3
"""Mod Roulette: the draw, the vetting, and the export.

This file used to exist to hold down one promise -- *the same seed and the
same constraints deal the same hand* -- and most of it was a port test for a
PRNG. The seed is gone, so the promise is gone with it, and what is left to
protect is the thing that actually made a rolled pack fail:

  * a hand that deals an add-on and not the mod it adds to,
  * a hand that deals two mods which refuse to sit together,
  * a hand that spends one of its slots on a library.

Those are `kinship` and `roulette.vet_hand`, and they are what most of this
file is about now. It runs entirely offline against a synthetic pool; nothing
here touches CurseForge, Modrinth or Crafty, and the two functions that would
(`describe_refs`, `resolve_hand`) are replaced with stubs so the rules can be
exercised exactly.
"""
import asyncio
import io
import json
import re
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app import kinship, roulette  # noqa: E402

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def fake_pool(n=400, offset=0):
    """A synthetic catalogue: nine categories, a spread of sizes and ages."""
    cats = [c["key"] for c in roulette.CATEGORIES]
    mods = []
    for i in range(offset, offset + n):
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
            "relations": {"requires": [], "breaks": [], "embeds": []},
        })
    return mods


def mod(name, **over):
    """One candidate, spelled out, for the rules that read names."""
    base = {
        "source": "curseforge", "project_id": abs(hash(name)) % 90000 + 1000,
        "file_id": 1, "name": name, "summary": "", "logo": None, "url": "",
        "downloads": 1_000_000, "updated": "2026-06-01T00:00:00Z",
        "size": 1024, "file_name": f"{name}.jar", "release_type": "release",
        "cf_categories": [], "mr_categories": [], "category": "utility",
        "server_side": None, "flag": None, "flag_why": {}, "age_days": 30,
        "relations": {"requires": [], "breaks": [], "embeds": []},
    }
    base.update(over)
    return base


def main():  # noqa: C901 -- a checklist, not a branchy function
    # =================================================================
    # 1. the draw
    # =================================================================
    pool = fake_pool()
    c = roulette.merge_constraints({
        "minecraft": "1.21.1", "loader": "NeoForge", "count": 40,
        "intensity": 3, "quality": 0,
        "categories": {"tech": roulette.PREFERRED, "magic": roulette.BANNED},
    })

    a = roulette.deal(pool, c)
    check("the hand is the requested size", len(a) == 40, str(len(a)))
    check("a banned category never appears",
          not any(m["category"] == "magic" for m in a))

    # No seed means no repeat. Two deals sharing every mod would mean the
    # weighting had collapsed into a filter.
    again = roulette.deal(pool, c)
    overlap = len({m["name"] for m in a} & {m["name"] for m in again})
    check("two pulls do not deal the same hand", overlap < len(a),
          f"{overlap}/{len(a)} in common")

    # Preference is a thumb on the scale, not a switch. Averaged over
    # several deals so a single unlucky one cannot fail the suite.
    tech = sum(sum(1 for m in roulette.deal(pool, c) if m["category"] == "tech")
               for _ in range(5)) / 5
    check("a preferred category is weighted up, not made exclusive",
          tech > 40 / len(roulette.CATEGORIES) and tech < 40,
          f"{tech:.1f} of 40 are tech on average")

    holds = [a[0]["name"], a[1]["name"]]
    held = roulette.deal(pool, c, holds)
    check("held mods survive the next pull",
          all(h in [m["name"] for m in held] for h in holds))
    check("a pull with holds still fills the hand", len(held) == 40, str(len(held)))

    swapped = roulette.reroll_one(a, a[5]["name"], pool, c)
    check("rerolling one slot replaces exactly one mod",
          len(swapped) == len(a)
          and len({m["name"] for m in swapped} ^ {m["name"] for m in a}) == 2,
          f"{a[5]['name']} -> {swapped[5]['name']}")

    # =================================================================
    # 2. what the roll is called, now that it is not a seed
    # =================================================================
    ids = {roulette.mint_roll_id() for _ in range(50)}
    check("every roll gets its own id", len(ids) == 50, f"{len(ids)} unique")
    check("a roll id is safe as a filename",
          all(re.fullmatch(r"[A-Za-z0-9-]+", i) for i in ids))
    check("a hostile roll id is reduced to one",
          roulette.safe_roll_id("../../etc/passwd") == "ETCPASSWD",
          roulette.safe_roll_id("../../etc/passwd"))
    check("an empty roll id mints one", bool(roulette.safe_roll_id("")))
    check("the seed machinery is gone",
          not hasattr(roulette, "prng") and not hasattr(roulette, "mint_seed"),
          "leaving it behind would leave two ways to deal a hand")

    # =================================================================
    # 3. Quilt is not on the menu
    # =================================================================
    check("Quilt is not offered", "Quilt" not in roulette.LOADERS,
          ", ".join(roulette.LOADERS))
    check("a stored Quilt preference is corrected, not obeyed",
          roulette.merge_constraints({"loader": "Quilt"})["loader"]
          == roulette.DEFAULT_CONSTRAINTS["loader"])
    check("...and so is a loader nobody has heard of",
          roulette.merge_constraints({"loader": "Rift"})["loader"]
          in roulette.LOADERS)
    check("a Fabric jar no longer passes as a Quilt one",
          not roulette._loader_ok(["fabric"], "quilt", "1.21.1"),
          "Quilt is not installable here, so nothing should resolve for it")
    check("the manifest table has no Quilt entry",
          "quilt" not in roulette.LOADER_MANIFEST_ID)

    # =================================================================
    # 4. kinship: libraries, parents, families
    # =================================================================
    check("a known library is recognised by name alone",
          kinship.looks_like_library("Fabric API"))
    check("...and by the category its author filed it under",
          kinship.looks_like_library("Whatever Thing", ["API and Library"]))
    check("a mod with a library-ish name is NOT dropped on that alone",
          not kinship.looks_like_library("Iron's Spells 'n Spellbooks"),
          "a name is thin evidence and this one has none of the rest")
    check("a plain content mod is left alone",
          not kinship.looks_like_library("Twilight Forest", ["Dimensions"]))
    check("a content mod mis-filed under API and Library survives",
          not kinship.looks_like_library("The One Probe", ["API and Library"]),
          "CurseForge files it there; a player would still notice it missing")

    check("a mod filed under a family it is not, is that family's add-on",
          kinship.category_parent("Steam 'n' Rails", ["Create"]) == "Create")
    check("the family's own mod is not its own add-on",
          kinship.category_parent("Create", ["Create"]) is None)
    check("a generic category names no parent",
          kinship.category_parent("Some Mod", ["Technology"]) is None)

    present = {kinship.norm("Create"): mod("Create")}
    check("a colon-named add-on finds its parent when the parent is around",
          (kinship.name_parent("Create: Steam 'n' Rails", present) or {})
          .get("name") == "Create")
    check("...and finds nothing when it is not",
          kinship.name_parent("Create: Steam 'n' Rails", {}) is None)
    check("a colon in an unrelated title is not a parent",
          kinship.name_parent("Oh The Biomes You'll Go", present) is None)

    check("CurseForge relations are read off the build",
          kinship.relations_from_build(
              {"dependencies": [{"modId": 7, "relationType": 3},
                                {"modId": 9, "relationType": 5},
                                {"modId": 4, "relationType": 2}]},
              "curseforge")
          == {"requires": ["curseforge:7"], "breaks": ["curseforge:9"],
              "embeds": []})
    check("Modrinth relations are read off the version",
          kinship.relations_from_build(
              {"dependencies": [{"project_id": "aa", "dependency_type": "required"},
                                {"project_id": "bb", "dependency_type": "incompatible"}]},
              "modrinth")
          == {"requires": ["modrinth:aa"], "breaks": ["modrinth:bb"],
              "embeds": []})

    viewers = [mod("Just Enough Items", downloads=900),
               mod("Roughly Enough Items", downloads=500),
               mod("EMI", downloads=100)]
    clash = kinship.find_conflicts(viewers)
    check("three recipe viewers is one conflict, not three", len(clash) == 1)
    check("...and the most-used one is the survivor",
          clash[0]["keep"] == "Just Enough Items", clash[0]["keep"])
    check("...leaving the other two to go",
          sorted(clash[0]["drop"]) == ["EMI", "Roughly Enough Items"])
    check("two of a limit-one family is still a conflict",
          len(kinship.find_conflicts(viewers[:2])) == 1)
    check("one of them is not",
          not kinship.find_conflicts(viewers[:1]))
    check("a family with room for two allows two",
          not kinship.find_conflicts([mod("Terralith"), mod("Biomes O' Plenty")]))
    check("...and not three",
          len(kinship.find_conflicts([mod("Terralith"), mod("Biomes O' Plenty"),
                                      mod("Regions Unexplored")])) == 1)
    check("an add-on is not mistaken for its family",
          not kinship.find_conflicts([mod("Sodium"), mod("Sodium Extras")]),
          "whole-name matching, so 'Sodium Extras' is not a second renderer")

    declared = [mod("Alpha", project_id=1, downloads=10,
                    relations={"requires": [], "breaks": ["curseforge:2"],
                               "embeds": []}),
                mod("Beta", project_id=2, downloads=99)]
    d = kinship.find_conflicts(declared)
    check("a declared incompatibility is honoured", len(d) == 1 and d[0]["kind"] == "declared")
    check("...and the bigger mod survives it", d[0]["drop"] == ["Alpha"], str(d))

    # =================================================================
    # 5. vetting a whole hand
    # =================================================================
    real_describe = kinship.describe_refs
    real_resolve = roulette.resolve_hand

    def stub_describe(table):
        async def describe(refs):
            return {r: table[r] for r in refs if r in table}
        return describe

    async def stub_resolve(hand, c, job=None):
        """Pin everything, the way a working catalogue would."""
        out = []
        for m in hand:
            m = dict(m, file_id=m.get("file_id") or 4242, version_match="exact",
                     download_url="https://example.invalid/x")
            out.append(m)
        return out, []

    async def vet(hand, constraints, pool_mods, table=None):
        kinship.describe_refs = stub_describe(table or {})
        roulette.resolve_hand = stub_resolve
        try:
            return await roulette.vet_hand(hand, constraints, pool_mods)
        finally:
            kinship.describe_refs = real_describe
            roulette.resolve_hand = real_resolve

    plain = roulette.merge_constraints({"minecraft": "1.21.1", "loader": "NeoForge"})

    # (a) a library dealt as one of the hand is taken back out
    hand = [mod("Create"), mod("Fabric API"), mod("Twilight Forest")]
    kept, problems, report = asyncio.run(vet(hand, plain, hand))
    check("a bare library is not left in the hand",
          [m["name"] for m in kept] == ["Create", "Twilight Forest"],
          str([m["name"] for m in kept]))
    check("...and says why it went",
          any(p["kind"] == "library" and p["name"] == "Fabric API"
              for p in problems), str(problems))

    # (b) an add-on whose parent is in the pool brings the parent with it
    parent = mod("Create", project_id=328085, downloads=200_000_000)
    addon = mod("Steam 'n' Rails", project_id=754482,
                relations={"requires": ["curseforge:328085"], "breaks": [],
                           "embeds": []})
    table = {"curseforge:328085": {
        "ref": "curseforge:328085",
        "source": "curseforge", "project_id": 328085, "name": "Create",
        "categories": ["Technology"], "downloads": 200_000_000, "library": None}}
    kept, problems, report = asyncio.run(
        vet([addon], plain, [parent, addon], table))
    names = [m["name"] for m in kept]
    check("an add-on's parent is brought into the hand", "Create" in names, str(names))
    check("...as a companion, not as one of your mods",
          any(m.get("adopted_for") == "Steam 'n' Rails"
              for m in kept if m["name"] == "Create"))
    check("...and the add-on stays", "Steam 'n' Rails" in names)
    check("the lineage names the family",
          any(f["parent"] == "Create" and "Steam 'n' Rails" in f["members"]
              for f in report["families"]), str(report["families"]))

    check("the family carries the ref it was resolved from",
          (report["families"][0] or {}).get("ref") == "curseforge:328085",
          "without it, a declared parent outside the pool could never be "
          "fetched, and the strongest signal was the one that gave up first")

    # (b2) a declared parent the pool never sampled is fetched on its own
    fetched = []
    real_fetch = roulette._fetch_parent

    async def stub_fetch(parent_ref):
        fetched.append(parent_ref)
        return dict(parent, name="Create")

    roulette._fetch_parent = stub_fetch
    try:
        kept, problems, _ = asyncio.run(vet([addon], plain, [addon], table))
    finally:
        roulette._fetch_parent = real_fetch
    check("a parent outside the pool is fetched rather than the add-on dropped",
          fetched == ["curseforge:328085"] and "Create" in [m["name"] for m in kept],
          f"{fetched} -> {[m['name'] for m in kept]}")

    # (c) the same add-on with no parent to be had goes instead
    kept, problems, report = asyncio.run(vet([addon], plain, [addon], table))
    check("an add-on with no parent available is dropped",
          not kept, str([m["name"] for m in kept]))
    check("...and the reason names the mod it needed",
          problems and "Create" in problems[0]["reason"], str(problems))

    # (d) a banned parent drops the add-on rather than smuggling it in
    banned = roulette.merge_constraints(
        {"minecraft": "1.21.1", "loader": "NeoForge",
         "categories": {"tech": roulette.BANNED}})
    kept, problems, _ = asyncio.run(
        vet([addon], banned, [dict(parent, category="tech"), addon], table))
    check("a banned category is not smuggled in as somebody's parent",
          not kept and problems and "banned" in problems[0]["reason"],
          str(problems))

    # (e) a family clash is resolved down to the limit
    hand = [mod("Just Enough Items", downloads=900),
            mod("Roughly Enough Items", downloads=500),
            mod("Create", downloads=800)]
    kept, problems, _ = asyncio.run(vet(hand, plain, hand))
    check("only one recipe viewer survives a hand with two",
          sorted(m["name"] for m in kept) == ["Create", "Just Enough Items"],
          str([m["name"] for m in kept]))
    check("...and the drop is reported as a conflict",
          any(p["kind"] == "conflict" for p in problems), str(problems))
    check("a vetted hand has nothing left to resolve",
          not kinship.find_conflicts(kept))

    # (f) a hand that is already fine is left completely alone
    fine = [mod("Create"), mod("Twilight Forest"), mod("Mekanism")]
    kept, problems, _ = asyncio.run(vet(fine, plain, fine))
    check("a hand with nothing wrong with it is untouched",
          [m["name"] for m in kept] == [m["name"] for m in fine] and not problems)

    # =================================================================
    # 6. constraints still do what their labels say
    # =================================================================
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

    # =================================================================
    # 7. the odds panel
    # =================================================================
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
    # The screen reads these four keys by name. They disagreed once, and the
    # panel showed "0 B" and "? GB" for a fortnight with nothing failing.
    check("the summary carries the keys the screen reads",
          {"download_mb", "estimated_jars", "heap_gb", "heap_ceiling_gb"}
          <= set(clean), sorted(clean))

    # =================================================================
    # 8. the export
    # =================================================================
    hand = a[:12]
    blob = roulette.build_export(hand, dict(c, roll_id="20260916-1200-ABCD"),
                                 "Roulette Test", None, "neoforge-21.1.249")
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = z.namelist()
    check("the export is a CurseForge modpack",
          {"manifest.json", "modlist.html"} <= set(names), ", ".join(names))
    manifest = json.loads(z.read("manifest.json"))
    check("the manifest declares the pack type",
          manifest["manifestType"] == "minecraftModpack")
    check("the manifest names the runtime",
          manifest["minecraft"]["version"] == "1.21.1"
          and manifest["minecraft"]["modLoaders"][0]["id"] == "neoforge-21.1.249",
          json.dumps(manifest["minecraft"]))
    # The CurseForge app rejects a manifest whose loader id has no build
    # number -- MinecraftUnsupportedModLoader, "check manifest!" -- and every
    # export written before this carried a bare 'neoforge'.
    loader_id = manifest["minecraft"]["modLoaders"][0]["id"]
    check("the loader id carries a build, not just a family",
          re.match(r"^(forge|neoforge|fabric)-[0-9]", loader_id) is not None,
          loader_id)
    cf = [m for m in hand if m["source"] == "curseforge"]
    check("every CurseForge mod is listed as a project/file pair",
          len(manifest["files"]) == len(cf)
          and all({"projectID", "fileID", "required"} <= set(f) for f in manifest["files"]),
          f"{len(manifest['files'])} files")
    check("Modrinth mods are bundled rather than faked into the manifest",
          len(manifest["files"]) < len(hand),
          "they have no CurseForge project id to reference")
    bundled = roulette.build_export(
        hand, dict(c, roll_id="X"), "T", {"extra.jar": b"PK" + bytes([3, 4]) + b"stub"})
    check("bundled jars land where a launcher expects them",
          "overrides/mods/extra.jar" in zipfile.ZipFile(
              io.BytesIO(bundled)).namelist())
    readme = z.read("README.txt").decode()
    check("the roll id is recorded in the archive", "20260916-1200-ABCD" in readme)
    check("the archive does not promise a repeat", "seed" not in readme.lower()
          or "no seed" in readme.lower(), readme.splitlines()[-4:])
    companion = roulette.build_export(
        [dict(hand[0]), dict(hand[1], adopted_for=hand[0]["name"])],
        dict(c, roll_id="Y"), "T", None, "neoforge-21.1.249")
    check("the archive says what came in with an add-on",
          "brought in" in zipfile.ZipFile(
              io.BytesIO(companion)).read("README.txt").decode())

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
