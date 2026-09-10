"""Regenerate the app marks from the shrine fox.

    python dev/tools/make_icon.py

Produces three files, all derived from `app/static/img/fox-mascot.png` so the
tab icon, the rail brand and the CasaOS store tile are unmistakably the same
animal:

    app/static/icon.png          256px  CasaOS tile + favicon, on a plum tile
    app/static/img/appmark-fox.png  128px  rail brand, transparent
    app/static/img/fox-head.png     512px  head crop, for empty states

The mascot is a wide image with a lot of transparent margin and the fox's
head in the upper middle, so every crop here is computed from the alpha
channel rather than from hard-coded pixel coordinates -- replace the mascot
and this still finds the face.

Needs Pillow. It is a dev-time tool and is not in requirements.txt: the image
it produces is committed, so the container never runs this.
"""
from __future__ import annotations

import pathlib
import sys

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    sys.exit("This needs Pillow: pip install Pillow")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "app" / "static" / "img" / "fox-mascot.png"

# The plum the rest of the app uses for its dark slab, so the tile reads as
# part of the same product rather than as a sticker on top of it.
TILE_BG = (36, 27, 40, 255)
TILE_RING = (233, 160, 180, 255)


def trim(img: Image.Image, threshold: int = 8) -> Image.Image:
    """Crop to the non-transparent pixels."""
    alpha = img.getchannel("A")
    box = alpha.point(lambda v: 255 if v > threshold else 0).getbbox()
    return img.crop(box) if box else img


def head_crop(img: Image.Image) -> Image.Image:
    """The head, letterboxed into a square.

    The mascot is a fox peering over a ledge: head and ears in the upper
    portion of the trimmed art, paws at the bottom. The ears are the widest
    part of it, so a square cut out of the middle loses them -- take the full
    width and the top of the height, then pad to square. Padding rather than
    cropping is the point: the mark keeps the silhouette that makes it
    recognisable at 32 pixels.
    """
    art = trim(img)
    w, h = art.size
    head = art.crop((0, 0, w, int(h * 0.74)))
    side = max(head.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(head, ((side - head.width) // 2,
                                  (side - head.height) // 2))
    return square


def rounded_tile(face: Image.Image, size: int, radius_ratio: float = 0.22
                 ) -> Image.Image:
    """The face on a plum rounded square, with a soft sakura ring."""
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    r = int(size * radius_ratio)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=r, fill=TILE_BG)

    # A ring of the sakura pink, blurred, so the tile has some depth at 32px
    # instead of reading as a flat square with a sticker on it.
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (size * 0.12, size * 0.12, size * 0.88, size * 0.88),
        fill=TILE_RING[:3] + (70,))
    tile.alpha_composite(glow.filter(ImageFilter.GaussianBlur(size * 0.07)))

    inset = int(size * 0.045)
    box = size - inset * 2
    scaled = face.resize((box, box), Image.LANCZOS)
    tile.alpha_composite(scaled, (inset, inset))

    # Clip anything the fox spilled past the rounded corners.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=r, fill=255)
    tile.putalpha(Image.composite(tile.getchannel("A"), mask,
                                  mask.point(lambda v: 255 if v == 0 else 0)))
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)
    return out


def main() -> int:
    if not SRC.is_file():
        sys.exit(f"missing {SRC}")
    src = Image.open(SRC).convert("RGBA")
    face = head_crop(src)

    targets = [
        (ROOT / "app" / "static" / "icon.png", rounded_tile(face, 256)),
        (ROOT / "app" / "static" / "img" / "appmark-fox.png",
         face.resize((128, 128), Image.LANCZOS)),
        (ROOT / "app" / "static" / "img" / "fox-head.png",
         face.resize((512, 512), Image.LANCZOS)),
    ]
    for path, img in targets:
        img.save(path, "PNG", optimize=True)
        print(f"wrote {path.relative_to(ROOT)}  {img.size[0]}px  "
              f"{path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
