"""纯 Python 生成 exe 图标（.ico），不依赖 Pillow / cairosvg。

图案与 ``stock_picker/static/favicon.svg`` 保持一致：蓝色圆角方块 + 三根白色 K 线。
同时输出 16/24/32/48/64/128/256 七种尺寸，供 Windows 在不同场景（任务栏、
资源管理器大图标、Alt+Tab）下取用。
"""

from __future__ import annotations

import struct
from pathlib import Path

BG = (0x25, 0x63, 0xEB)      # #2563eb
FG = (0xFF, 0xFF, 0xFF)

# 每根 K 线：(中心 x, 实体上沿, 实体下沿, 影线上沿, 影线下沿)，坐标按 32 格设计稿
CANDLES = [
    (9, 13, 19, 9, 23),
    (16, 9, 16, 6, 20),
    (23, 16, 22, 12, 26),
]
BODY_HALF_WIDTH = 2.5
WICK_HALF_WIDTH = 0.9
CORNER_RADIUS = 7.0


def _inside_rounded(x: float, y: float, size: float) -> bool:
    """点是否落在圆角方块内（坐标原点在左上角）。"""
    r = CORNER_RADIUS * size / 32.0
    if not (0.0 <= x <= size and 0.0 <= y <= size):
        return False
    if (r <= x <= size - r) or (r <= y <= size - r):
        return True
    cx = r if x < r else size - r
    cy = r if y < r else size - r
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def _inside(x: float, y: float, x0: float, x1: float, y0: float, y1: float) -> bool:
    return x0 <= x <= x1 and y0 <= y <= y1


def _render_rgba(size: int, ss: int = 4) -> bytes:
    """渲染 size×size 的 BGRA 像素（自上而下逐行，供 ICO 的 XOR 位图使用）。"""
    scale = size / 32.0
    candles = [
        (
            c[0] * scale - BODY_HALF_WIDTH * scale,
            c[0] * scale + BODY_HALF_WIDTH * scale,
            c[1] * scale,
            c[2] * scale,
            c[0] * scale - WICK_HALF_WIDTH * scale,
            c[0] * scale + WICK_HALF_WIDTH * scale,
            c[3] * scale,
            c[4] * scale,
        )
        for c in CANDLES
    ]

    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            bg_hits = 0
            fg_hits = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = px + (sx + 0.5) / ss
                    y = py + (sy + 0.5) / ss
                    if not _inside_rounded(x, y, size):
                        continue
                    bg_hits += 1
                    for bx0, bx1, by0, by1, wx0, wx1, wy0, wy1 in candles:
                        if _inside(x, y, bx0, bx1, by0, by1) or _inside(x, y, wx0, wx1, wy0, wy1):
                            fg_hits += 1
                            break
            total = ss * ss
            if bg_hits == 0:
                row += b"\x00\x00\x00\x00"
                continue
            alpha = int(round(255 * bg_hits / total))
            # 前景按覆盖比例与底色混合，避免锯齿
            fg_ratio = fg_hits / bg_hits
            b = int(round(BG[2] * (1 - fg_ratio) + FG[2] * fg_ratio))
            g = int(round(BG[1] * (1 - fg_ratio) + FG[1] * fg_ratio))
            r = int(round(BG[0] * (1 - fg_ratio) + FG[0] * fg_ratio))
            row += bytes((b, g, r, alpha))
        rows.append(row)
    # ICO 的 DIB 是自下而上
    return b"".join(reversed(rows))


def _dib(size: int, rgba_bottom_up: bytes) -> bytes:
    header = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        size,          # biWidth
        size * 2,      # biHeight（XOR + AND 两张图）
        1,             # biPlanes
        32,            # biBitCount
        0,             # biCompression = BI_RGB
        len(rgba_bottom_up),
        0, 0, 0, 0,
    )
    # AND 掩码：32 位图靠 alpha 通道透明，这里全 0，但必须按 4 字节对齐存在
    mask_row = ((size + 31) // 32) * 4
    return header + rgba_bottom_up + b"\x00" * (mask_row * size)


def build_ico(sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)) -> bytes:
    images = []
    for s in sizes:
        images.append((s, _dib(s, _render_rgba(s))))

    out = bytearray()
    out += struct.pack("<HHH", 0, 1, len(images))       # ICONDIR
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack(
            "<BBBBHHII",
            s if s < 256 else 0,    # 256 用 0 表示
            s if s < 256 else 0,
            0, 0, 1, 32,
            len(data),
            offset,
        )
        offset += len(data)
    for _, data in images:
        out += data
    return bytes(out)


def write_ico(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_ico())
    return path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "stock_picker.ico"
    write_ico(target)
    print(f"wrote {target} ({target.stat().st_size} bytes)")
