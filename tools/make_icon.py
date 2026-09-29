"""Render a 512x512 transparent PNG brand icon: a red heart with a white ECG line.

Pure stdlib (zlib + struct): the machine has no Pillow and pip is locked by PEP 668.
"""

import math
import struct
import zlib

SIZE = 512
SS = 3  # supersample factor for anti-aliased edges
RED = (0xC7, 0x00, 0x0B)
WHITE = (0xFF, 0xFF, 0xFF)

# ECG polyline in heart coordinates (y points up)
TRACE = [(-0.86, 0.05), (-0.5, 0.05), (-0.36, 0.2), (-0.18, -0.3), (-0.02, 0.58),
         (0.14, -0.18), (0.28, 0.1), (0.42, 0.05), (0.86, 0.05)]
STROKE = 0.075


def heart_field(x, y):
    """<= 0 inside the implicit heart (x^2 + y^2 - 1)^3 - x^2 y^3."""
    a = x * x + y * y - 1.0
    return a * a * a - x * x * y * y * y


def dist_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def trace_distance(x, y):
    return min(dist_to_segment(x, y, ax, ay, bx, by)
               for (ax, ay), (bx, by) in zip(TRACE, TRACE[1:]))


def sample(u, v):
    """Map image coords (u, v in 0..1, v down) to heart coords and pick a colour."""
    x = (u - 0.5) * 2.6
    y = -(v - 0.5) * 2.6
    if heart_field(x, y) > 0:
        return None
    if trace_distance(x, y) <= STROKE:
        return WHITE
    return RED


def render():
    rows = []
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            red = white = hit = 0
            for sy in range(SS):
                for sx in range(SS):
                    u = (px + (sx + 0.5) / SS) / SIZE
                    v = (py + (sy + 0.5) / SS) / SIZE
                    colour = sample(u, v)
                    if colour is None:
                        continue
                    hit += 1
                    if colour is WHITE:
                        white += 1
            if not hit:
                row += b"\x00\x00\x00\x00"
                continue
            coverage = hit / (SS * SS)
            share = white / hit
            r = round(RED[0] + (WHITE[0] - RED[0]) * share)
            g = round(RED[1] + (WHITE[1] - RED[1]) * share)
            b = round(RED[2] + (WHITE[2] - RED[2]) * share)
            row += bytes((r, g, b, round(255 * coverage)))
        rows.append(bytes(row))
    return rows


def png(path, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        fh.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    import sys
    png(sys.argv[1] if len(sys.argv) > 1 else "icon.png", render())
    print("wrote", sys.argv[1] if len(sys.argv) > 1 else "icon.png")
