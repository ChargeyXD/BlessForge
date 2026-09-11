"""Generate the two cherry-blossom backdrops, one per theme.

    python dev/tools/make_backdrop.py

Writes `app/static/img/sakura-light.svg` and `sakura-dark.svg`.

Why SVG and not the source photographs
--------------------------------------
The backdrop is blurred to the point where nothing but mass, colour and
silhouette survives, and it has to cover any viewport from a phone to an
ultrawide. Those two facts together make a raster the wrong format: a JPEG
big enough not to smear on a 3440px monitor is megabytes, has to be
resampled at every size, and is thrown away by the blur anyway. These are a
few KB each, scale without resampling, and carry their own
`feGaussianBlur` so the browser blurs once at rasterise time instead of on
every repaint of a full-page fixed element.

They are *modelled on* the two reference images, not copies of them:

  light -- sumi-e. One gnarled tree leaning left, canopy sweeping right,
           crimson-to-pink speckled blossom, loose petals drifting off to
           the right, faint mountains behind, still water and a reflection
           along the bottom. Very high key.
  dark  -- a night canopy filling the top-left, warm lantern bokeh inside
           it, a near-black sky with small stars, dark water below.

If the real images are ever wanted instead, drop them in as
`app/static/img/bg-sakura-light.<ext>` / `bg-sakura-dark.<ext>` and the
front end picks them up on its own -- see `backdrop()` in app.js. Nothing
here has to change.

Deterministic: a fixed seed per scene, so regenerating produces the same
file and the diff stays empty unless the code changed.
"""
from __future__ import annotations

import math
import pathlib
import random

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "app" / "static" / "img"


# --- geometry ----------------------------------------------------------

def branch(rng, x, y, angle, length, width, depth, out, tips):
    """Grow one branch, recursively, collecting paths and blossom anchors.

    `out` gets (path-d, stroke-width) for the wood; `tips` gets (x, y,
    weight) for wherever blossom should cluster. Blossom follows the thin
    ends rather than being scattered over the whole tree, which is what
    makes a painted cherry read as a cherry.
    """
    # Prune on WIDTH, not just depth. A 1.5px twig under stdDeviation 7
    # contributes nothing but bytes -- the blur erases it completely --
    # and the twigs are where the exponential lives. Stopping at 2.5px
    # takes the tree from 937 paths to about 120 and looks the same.
    if depth <= 0 or length < 9 or width < 2.5:
        tips.append((x, y, max(width, 1.0)))
        return

    # A cherry branch does not travel straight -- it kinks. Two control
    # points wandering off the straight line give the crooked, knuckly
    # line the reference has.
    ex = x + math.cos(angle) * length
    ey = y + math.sin(angle) * length
    wob = length * 0.34
    c1x = x + math.cos(angle) * length * 0.35 + rng.uniform(-wob, wob)
    c1y = y + math.sin(angle) * length * 0.35 + rng.uniform(-wob, wob)
    c2x = x + math.cos(angle) * length * 0.7 + rng.uniform(-wob, wob)
    c2y = y + math.sin(angle) * length * 0.7 + rng.uniform(-wob, wob)
    out.append((
        f"M{x:.0f} {y:.0f}C{c1x:.0f} {c1y:.0f} {c2x:.0f} {c2y:.0f} "
        f"{ex:.0f} {ey:.0f}",
        max(1, round(width)),
    ))

    if width > 2.2:
        tips.append((ex, ey, width * 0.5))

    # Two or three children, biased to the right so the canopy sweeps that
    # way the way the reference does.
    kids = 2 if depth > 2 and rng.random() < 0.72 else 3
    for i in range(kids):
        spread = rng.uniform(0.20, 0.62)
        turn = spread if i % 2 == 0 else -spread * 0.72
        branch(
            rng, ex, ey,
            angle + turn + 0.06,          # the bias
            length * rng.uniform(0.62, 0.80),
            max(0.9, width * rng.uniform(0.55, 0.70)),
            depth - 1, out, tips,
        )


OPACITIES = (".35", ".55", ".75", ".95")


def emit(dots: list[tuple[int, int, int, str, str]]) -> str:
    """Serialise discs, grouped by everything they have in common.

    This is the whole file-size story. A disc carrying its own fill,
    radius and opacity is ~62 bytes and 3,000 of them is a 200 KB
    backdrop, which is absurd for something nobody is meant to look
    directly at. Quantising opacity to four buckets and hoisting `fill`
    and `opacity` onto a wrapping <g> leaves `<circle cx="470" cy="300"
    r="12"/>` -- 34 bytes -- and the same picture, because the blur
    cannot resolve the difference between opacity .61 and .55 anyway.

    `r` STAYS ON THE CIRCLE. It is a geometry property, not an
    inheritable presentation attribute, so `<g r="12">` is ignored and
    every circle collapses to r=0 -- which renders as a tree with no
    blossom on it at all. (SVG 2 makes `r` settable in CSS, but Firefox
    does not implement geometry properties in CSS, so the stylesheet
    route is not portable either.) Only `fill` and `opacity` hoist.
    """
    buckets: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
    for x, y, r, fill, op in dots:
        buckets.setdefault((fill, op), []).append((x, y, r))
    parts = []
    for (fill, op), pts in sorted(buckets.items()):
        inner = "".join(f'<circle cx="{x}" cy="{y}" r="{r}"/>'
                        for x, y, r in pts)
        parts.append(f'<g fill="{fill}" opacity="{op}">{inner}</g>')
    return "".join(parts)


def blossom(rng, tips, count, palette, spread, size):
    """Petal mass as discs clustered on the branch tips.

    Not one filled shape: a cherry in bloom is thousands of separate
    florets with sky between them, and the speckle is the only thing that
    distinguishes it from a pink cloud. But `size` is deliberately large
    relative to the reference -- at stdDeviation 7 on a 1600px canvas
    anything under about 14px across is erased, so small discs cost bytes
    and buy nothing. Fewer, fatter, still speckled.
    """
    if not tips:
        return []
    total = sum(t[2] for t in tips) or 1.0
    out = []
    for tx, ty, weight in tips:
        n = max(1, int(count * (weight / total)))
        for _ in range(n):
            # Gaussian scatter, so clusters have soft edges rather than
            # a visible circular boundary.
            dx = rng.gauss(0, spread)
            dy = rng.gauss(0, spread * 0.8)
            r = max(3, int(abs(rng.gauss(size, size * 0.4))))
            out.append((
                round(tx + dx), round(ty + dy), quant_r(r),
                rng.choice(palette), rng.choice(OPACITIES),
            ))
    return out


def quant_r(r: int) -> int:
    for step in (5, 8, 12, 17, 23):
        if r <= step:
            return step
    return 23


def loose(rng, count, x0, x1, y0, y1, palette, size):
    """Petals that have left the tree.

    Discs rather than rotated ellipses for the same reason: a rotate()
    transform is 30 bytes per petal and the blur eats the orientation.
    """
    return [(
        round(rng.uniform(x0, x1)), round(rng.uniform(y0, y1)),
        quant_r(max(3, int(abs(rng.gauss(size, size * 0.5))))),
        rng.choice(palette), rng.choice(OPACITIES),
    ) for _ in range(count)]


def wood(paths, colour):
    """Branches, grouped by stroke width.

    Same trick as `emit`: `stroke`, `stroke-width`, `fill` and
    `stroke-linecap` are identical across every branch of a given
    thickness, so they belong on the group, not on 900 copies.
    """
    by_width: dict[int, list[str]] = {}
    for d, w in paths:
        by_width.setdefault(int(w), []).append(d)
    return "".join(
        f'<g stroke="{colour}" stroke-width="{w}" fill="none" '
        f'stroke-linecap="round">'
        + "".join(f'<path d="{d}"/>' for d in ds) + "</g>"
        for w, ds in sorted(by_width.items())
    )


# --- light: sumi-e, one tree, high key ---------------------------------

def light() -> str:
    rng = random.Random(20260910)
    W, H = 1600, 560
    horizon = H * 0.80

    petals = ["#B23F5C", "#D9788F", "#E9A0B4", "#C4335A", "#F3C3CE"]

    paths: list[tuple[str, float]] = []
    tips: list[tuple[float, float, float]] = []
    # The trunk leans left out of the water, then everything above it
    # travels right.
    branch(rng, 470, horizon, math.radians(-74), 132, 30, 7, paths, tips)
    # A second, lower limb going hard right -- the reference's canopy is
    # much wider than it is tall.
    branch(rng, 500, horizon - 96, math.radians(-16), 150, 15, 6, paths, tips)

    mass = blossom(rng, tips, 620, petals, spread=30, size=11)
    drift = loose(rng, 90, 640, W, 40, horizon - 10, petals, 8)
    fallen = loose(rng, 26, 260, 1180, horizon + 6, H - 10, petals, 7)

    mountains = (
        '<path d="M0 372 L86 300 L150 338 L232 272 L320 344 L392 318 '
        'L455 370 L0 372Z" fill="#8E8794" opacity=".16"/>'
        '<path d="M0 392 L120 340 L198 376 L286 322 L372 384 L440 358 '
        'L520 396 L0 396Z" fill="#8E8794" opacity=".10"/>'
        '<path d="M1180 386 L1290 330 L1372 372 L1468 326 L1560 380 '
        'L1600 362 L1600 396 L1180 396Z" fill="#8E8794" opacity=".09"/>'
    )

    stars = ""  # none in daylight
    body = f"""
  <rect width="{W}" height="{H}" fill="#FBF3F0"/>
  {mountains}
  <rect x="0" y="{horizon:.0f}" width="{W}" height="{H - horizon:.0f}"
        fill="#EDE2E4" opacity=".55"/>
  <g opacity=".26" transform="translate(0 {2 * horizon:.0f}) scale(1 -1)">
    {wood(paths, "#2E2432")}
    {emit(mass[::3])}
  </g>
  {wood(paths, "#2E2432")}
  {emit(mass)}
  {emit(drift)}
  {emit(fallen)}
  {stars}
"""
    # Faint on purpose. It is a ground, not a picture: at full strength
    # the trunk competes with the text sitting on top of it.
    return svg(W, H, body, blur=7, extra_opacity=0.62)


# --- dark: night canopy, lantern bokeh ---------------------------------

def dark() -> str:
    rng = random.Random(20260911)
    W, H = 1600, 1000
    # No near-whites here. #F8DCE3 is only a shade off the stars and at
    # this blur the two become the same grey blob, which is what made the
    # first pass read as generic bokeh instead of a cherry tree at night.
    petals = ["#F0A0B6", "#EFAEC0", "#D9788F", "#C86E8B", "#B8547A"]

    paths: list[tuple[str, float]] = []
    tips: list[tuple[float, float, float]] = []
    # The canopy hangs INTO frame from above, so every branch starts above
    # the top edge and grows down. Seven origins across the full width
    # rather than four bunched left: the reference has blossom over the
    # whole top of the frame, thinning towards the bottom right.
    origins = [
        (-80, -40, 34, 210, 30), (150, -90, 62, 190, 26),
        (430, -80, 78, 175, 24), (720, -100, 92, 170, 22),
        (1010, -85, 104, 165, 21), (1290, -95, 116, 158, 20),
        (1560, -60, 140, 150, 18),
    ]
    for x, y, deg, length, width in origins:
        branch(rng, x, y, math.radians(deg), length, width, 7, paths, tips)

    mass = blossom(rng, tips, 1500, petals, spread=36, size=14)
    # Few, and low, so they read as petals that have left the canopy
    # rather than as a second layer of bokeh competing with it.
    drift = loose(rng, 55, 0, W, H * 0.42, H * 0.80, petals, 8)

    star_bits = []
    for _ in range(260):
        x = rng.uniform(0, W)
        # Only where sky actually shows: below the canopy mass, and
        # weighted to the right where it is thinnest.
        y = rng.uniform(H * 0.30, H * 0.83)
        if rng.random() > (0.25 + 0.75 * (x / W) * (y / H)):
            continue
        star_bits.append((round(x), round(y), 2, "#FFFFFF",
                          rng.choice((".35", ".55"))))

    # The lanterns. Warm, inside the canopy, and the only warm light in
    # the scene -- which is what sells it as night.
    lantern_bits = []
    for _ in range(22):
        x = rng.uniform(40, W - 40)
        y = rng.uniform(20, H * 0.40)
        r = rng.uniform(16, 44)
        lantern_bits.append(
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r:.0f}" '
            f'fill="url(#lantern)" opacity="{rng.uniform(.55, 1):.2f}"/>'
        )

    water = H * 0.86
    body = f"""
  <rect width="{W}" height="{H}" fill="url(#night)"/>
  {emit(star_bits)}
  <rect x="0" y="{water:.0f}" width="{W}" height="{H - water:.0f}"
        fill="#0A0713"/>
  <g opacity=".30" transform="translate(0 {2 * water:.0f}) scale(1 -1)">
    {emit(mass[::4])}
  </g>
  {wood(paths, "#1A1220")}
  {emit(mass)}
  {''.join(lantern_bits)}
  {emit(drift)}
"""
    defs = """
  <radialGradient id="lantern">
    <stop offset="0%" stop-color="#FFF0C4" stop-opacity=".95"/>
    <stop offset="45%" stop-color="#F3C97A" stop-opacity=".45"/>
    <stop offset="100%" stop-color="#E8A85A" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="night" x1="0" y1="0" x2=".3" y2="1">
    <stop offset="0%" stop-color="#171029"/>
    <stop offset="55%" stop-color="#0E0A1B"/>
    <stop offset="100%" stop-color="#0A0713"/>
  </linearGradient>
"""
    return svg(W, H, body, blur=9, defs=defs, extra_opacity=0.72)


# --- wrapper -----------------------------------------------------------

def svg(w: int, h: int, body: str, blur: int, defs: str = "",
        extra_opacity: float = 1.0) -> str:
    """Wrap a scene, with the blur baked in.

    Baked rather than applied in CSS: `filter:blur()` on a full-page fixed
    element is repainted by the compositor on scroll and theme change,
    where this is rasterised once. `filterUnits` is left at the default
    objectBoundingBox but the region is widened, because the default -10%
    margin clips a blur this wide and leaves a hard rectangular edge.
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" preserveAspectRatio="xMidYMid slice">'
        f"<defs>{defs}"
        f'<filter id="soft" x="-15%" y="-15%" width="130%" height="130%">'
        f'<feGaussianBlur stdDeviation="{blur}"/></filter>'
        f"</defs>"
        f'<g filter="url(#soft)" opacity="{extra_opacity}">{body}</g>'
        f"</svg>"
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, gen in (("sakura-light.svg", light), ("sakura-dark.svg", dark)):
        text = gen()
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"{name}: {len(text) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
