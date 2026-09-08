"""图片像素 diff：两张长截图 → 变化区域矩形列表（用于 Flutter Web 等 canvas 页面）。"""
from __future__ import annotations

from PIL import Image, ImageChops

CELL = 8          # 网格粒度（像素）
THRESHOLD = 40    # 像素差阈值
MIN_AREA = 4      # 最少网格数


def _pad(img: Image.Image, w: int, h: int) -> Image.Image:
    if img.size == (w, h):
        return img
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    canvas.paste(img, (0, 0))
    return canvas


def diff_images(base_path: str, new_path: str) -> dict:
    a = Image.open(base_path).convert("RGB")
    b = Image.open(new_path).convert("RGB")
    w, h = max(a.width, b.width), max(a.height, b.height)
    a, b = _pad(a, w, h), _pad(b, w, h)
    d = ImageChops.difference(a, b).convert("L").point(lambda p: 255 if p > THRESHOLD else 0)
    gw, gh = (w + CELL - 1) // CELL, (h + CELL - 1) // CELL
    small = d.resize((gw, gh), Image.BOX)
    px = small.load()
    grid = [[px[x, y] > 8 for x in range(gw)] for y in range(gh)]
    seen = [[False] * gw for _ in range(gh)]
    boxes = []
    changed_cells = 0
    for y in range(gh):
        for x in range(gw):
            if not grid[y][x] or seen[y][x]:
                continue
            stack = [(x, y)]
            seen[y][x] = True
            minx = maxx = x
            miny = maxy = y
            n = 0
            while stack:
                cx, cy = stack.pop()
                n += 1
                minx, maxx, miny, maxy = min(minx, cx), max(maxx, cx), min(miny, cy), max(maxy, cy)
                for nx in range(max(0, cx - 2), min(gw, cx + 3)):
                    for ny in range(max(0, cy - 2), min(gh, cy + 3)):
                        if grid[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((nx, ny))
            changed_cells += n
            if n >= MIN_AREA:
                boxes.append({"x": minx * CELL, "y": miny * CELL, "w": (maxx - minx + 1) * CELL, "h": (maxy - miny + 1) * CELL})
    boxes.sort(key=lambda r: (r["y"], r["x"]))
    for i, r in enumerate(boxes):
        r["type"] = "changed"
        r["label"] = f"区域 {i + 1}"
    return {
        "changes": boxes[:400],
        "width": w,
        "height": h,
        "size_changed": a.size != b.size or Image.open(base_path).size != Image.open(new_path).size,
        "ratio": round(changed_cells / max(1, gw * gh), 3),
        "counts": {"changed": len(boxes)},
        "truncated": len(boxes) > 400,
    }
