"""Generate app/static/icon.png -- the CasaOS app-store tile.

Pure stdlib: no Pillow on the build host, and the icon has to live in the repo
so CasaOS can fetch it over raw.githubusercontent. Shapes mirror the inline
favicon in index.html so the tab icon and the store tile are the same mark.
"""
import struct, zlib

S = 256          # output size
SS = 4           # supersampling factor
W = S * SS

BG      = (0x19, 0x1c, 0x14, 255)   # forge console background
ORANGE  = (0xd9, 0x77, 0x42, 255)   # identity
GREEN   = (0x7f, 0xa2, 0x57, 255)   # action colour
DARK    = (0x11, 0x13, 0x0e, 255)   # cut-outs
FACE    = (0xe8, 0xe9, 0xe0, 255)   # visor highlight

buf = [[(0, 0, 0, 0)] * W for _ in range(W)]


def blend(dst, src):
    a = src[3] / 255.0
    if a >= 1:
        return src
    return tuple(int(src[i] * a + dst[i] * (1 - a)) for i in range(3)) + (255,)


def fill_poly(points, colour):
    pts = [(x * SS, y * SS) for x, y in points]
    ys = [p[1] for p in pts]
    for y in range(max(0, int(min(ys))), min(W, int(max(ys)) + 1)):
        xs = []
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            if (y1 <= y < y2) or (y2 <= y < y1):
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            for x in range(max(0, int(xs[i])), min(W, int(xs[i + 1]) + 1)):
                buf[y][x] = blend(buf[y][x], colour)


def fill_rect(x, y, w, h, colour, radius=0):
    for yy in range(int(y * SS), int((y + h) * SS)):
        for xx in range(int(x * SS), int((x + w) * SS)):
            if 0 <= xx < W and 0 <= yy < W:
                if radius:
                    r = radius * SS
                    cx = min(max(xx, x * SS + r), (x + w) * SS - r)
                    cy = min(max(yy, y * SS + r), (y + h) * SS - r)
                    if (xx - cx) ** 2 + (yy - cy) ** 2 > r * r:
                        continue
                buf[yy][xx] = blend(buf[yy][xx], colour)


# background tile
fill_rect(0, 0, S, S, BG, radius=56)

# the forge helmet, scaled from the 24x24 favicon path
def p(x, y):
    return (28 + x * 8.3, 26 + y * 8.3)

fill_poly([p(3, 3), p(7, 6), p(17, 6), p(21, 3), p(20, 10), p(22, 13),
           p(18, 15), p(12, 21), p(6, 15), p(2, 13), p(4, 10)], ORANGE)
fill_poly([p(12, 21), p(6, 15), p(9, 14), p(15, 14), p(18, 15)], FACE)
fill_rect(*p(8, 10), 21, 21, DARK)
fill_rect(*p(13.5, 10), 21, 21, DARK)
fill_rect(*p(11, 15), 17, 17, GREEN)

# downsample
out = bytearray()
for y in range(S):
    out.append(0)
    for x in range(S):
        r = g = b = a = 0
        for dy in range(SS):
            for dx in range(SS):
                px = buf[y * SS + dy][x * SS + dx]
                r += px[0]; g += px[1]; b += px[2]; a += px[3]
        n = SS * SS
        out += bytes((r // n, g // n, b // n, a // n))


def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(bytes(out), 9))
       + chunk(b"IEND", b""))
open("app/static/icon.png", "wb").write(png)
print("wrote app/static/icon.png", len(png), "bytes")
