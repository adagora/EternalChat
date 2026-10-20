"""Draw EternalChat's mark, tree rings, as the page's icons: eternalchat/static/*.

Rings grow outward from an off-center core, as a tree's do: the oldest at the
center, each year around it. One geometry gives the SVG and every PNG; the PNGs
are rasterized here (4x4 supersampling), so no image tool is needed. Run it
again after changing the geometry or colors.
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "eternalchat" / "static"
INK, PAPER, EMBER = (27, 25, 22), (244, 239, 230), (224, 138, 60)
# Rings as (center x, center y, radius, stroke width) and the core as (x, y, r),
# in units of the icon's side.
RINGS = [(0.50, 0.50, 0.335, 0.046), (0.475, 0.525, 0.205, 0.046)]
CORE = (0.452, 0.548, 0.068)


def svg(size=512, radius=0.225, scale=1.0):
    def at(v):
        return round(0.5 + (v - 0.5) * scale, 4) * size

    rings = "".join(f'<circle cx="{at(x):g}" cy="{at(y):g}" r="{r * scale * size:g}" stroke-width="{w * scale * size:g}"/>'
                    for x, y, r, w in RINGS)
    x, y, r = CORE
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
            f'<rect width="{size}" height="{size}" rx="{radius * size:g}" fill="rgb{INK}"/>'
            f'<g fill="none" stroke="rgb{PAPER}">{rings}</g>'
            f'<circle cx="{at(x):g}" cy="{at(y):g}" r="{r * scale * size:g}" fill="rgb{EMBER}"/></svg>\n')


def png(size, *, radius, scale):
    """RGBA bytes of the mark: a rounded square (radius 0: full bleed), rings scaled about the center."""
    n = 4
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(n):
                for sx in range(n):
                    u, v = (px + (sx + 0.5) / n) / size, (py + (sy + 0.5) / n) / size
                    color = sample(u, v, radius, scale)
                    if color:
                        for k in range(3):
                            acc[k] += color[k]
                        acc[3] += 1
            covered = acc[3]
            if covered:
                row += bytes(round(acc[k] / covered) for k in range(3)) + bytes([round(255 * covered / n / n)])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def sample(u, v, radius, scale):
    if radius:
        cx = min(max(u, radius), 1 - radius)
        cy = min(max(v, radius), 1 - radius)
        if (u - cx) ** 2 + (v - cy) ** 2 > radius ** 2:
            return None
    # Back into the unscaled mark's coordinates.
    mu, mv = 0.5 + (u - 0.5) / scale, 0.5 + (v - 0.5) / scale
    x, y, r = CORE
    if (mu - x) ** 2 + (mv - y) ** 2 <= r ** 2:
        return EMBER
    for x, y, r, w in RINGS:
        if abs(((mu - x) ** 2 + (mv - y) ** 2) ** 0.5 - r) <= w / 2:
            return PAPER
    return INK


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "icon.svg").write_text(svg(), encoding="utf-8")
    # "any" icons keep their own rounded corners; the maskable and Apple ones are
    # full bleed (the system cuts the shape) with the mark inside the safe zone.
    for name, size, radius, scale in (("icon-192.png", 192, 0.225, 1.0), ("icon-512.png", 512, 0.225, 1.0),
                                      ("icon-maskable.png", 512, 0, 0.72), ("apple-touch-icon.png", 180, 0, 0.86)):
        (OUT / name).write_bytes(png(size, radius=radius, scale=scale))
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
