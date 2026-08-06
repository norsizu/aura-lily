#!/usr/bin/env python3
"""
Convert a 3x3 outfit pose sheet into Aura ESP32 outfit atlas format.

Atlas format:
  uint32 little-endian width  (600)
  uint32 little-endian height (900)
  raw 8-bit pixels, 3x3 cells of 200x300

Pixel convention:
  0xFF = transparent outside the character
  0xFE = visible white inside the character
  0x00 = black ink

This converter stores grayscale pixels. Dithering is done by firmware only when
compositing the character, so the same asset can be tuned on-device and the
background/UI stay clean.
"""
from __future__ import annotations

import argparse
from collections import deque
import struct
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter


ATLAS_COLS = 3
ATLAS_ROWS = 3
POSE_W = 200
POSE_H = 300
ATLAS_W = ATLAS_COLS * POSE_W
ATLAS_H = ATLAS_ROWS * POSE_H


def remove_sheet_edges(mask: Image.Image) -> None:
    """Generated sheets often have black borders exactly on cell edges."""
    w, h = mask.size
    px = mask.load()
    # Scale the cleared band with the cell size (~2%) so higher-resolution
    # sheets with thicker/dashed separators are still fully cleaned.
    band = max(8, int(min(w, h) * 0.02 + 0.5))

    for y in list(range(min(band, h))) + list(range(max(0, h - band), h)):
        for x in range(w):
            px[x, y] = 0

    for x in list(range(min(band, w))) + list(range(max(0, w - band), w)):
        for y in range(h):
            px[x, y] = 0


def clean_sheet_grid(src: Image.Image, cell_w: int, cell_h: int) -> Image.Image:
    """Erase grid separator lines from the full sheet before cell cropping.

    Generated sheets place (sometimes dashed) separator lines at the exact
    1/3 and 2/3 cell boundaries plus the outer border.  Dashed remnants can
    leak a few pixels into a cell, inflating the shared bbox and shrinking
    the character.  We only touch rows/columns close to those boundaries so
    the character art itself is never affected.
    """
    rgb = src.convert("RGB")
    white = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, white).convert("L")
    ink = diff.point(lambda v: 255 if v > 9 else 0)

    w, h = src.size
    out = src.copy()
    opx = out.load()
    ipx = ink.load()

    def clear_row(y: int) -> None:
        for x in range(w):
            opx[x, y] = (255, 255, 255, 255)

    def clear_col(x: int) -> None:
        for y in range(h):
            opx[x, y] = (255, 255, 255, 255)

    band = max(4, int(min(cell_w, cell_h) * 0.02 + 0.5))
    zone = max(band, int(min(cell_w, cell_h) * 0.06 + 0.5))

    y_bounds = [0, cell_h, 2 * cell_h, h - 1]
    x_bounds = [0, cell_w, 2 * cell_w, w - 1]

    # 1) Unconditionally clear a thin band right on each boundary.
    for by in y_bounds:
        for y in range(max(0, by - band), min(h, by + band + 1)):
            clear_row(y)
    for bx in x_bounds:
        for x in range(max(0, bx - band), min(w, bx + band + 1)):
            clear_col(x)

    # 2) Within a wider zone, clear rows/cols whose ink occupancy looks like a
    #    (dashed) separator line rather than character art.
    for by in y_bounds:
        for y in range(max(0, by - zone), min(h, by + zone + 1)):
            count = sum(1 for x in range(w) if ipx[x, y] != 0)
            if count > w * 2 // 5:
                clear_row(y)
    for bx in x_bounds:
        for x in range(max(0, bx - zone), min(w, bx + zone + 1)):
            count = sum(1 for y in range(h) if ipx[x, y] != 0)
            if count > h * 2 // 5:
                clear_col(x)

    return out


def alpha_mask(cell: Image.Image) -> Image.Image | None:
    """Return alpha-derived visibility mask when the source is truly transparent."""
    if cell.mode != "RGBA":
        return None
    alpha = cell.getchannel("A")
    lo, hi = alpha.getextrema()
    if hi <= 0 or lo >= 255:
        return None
    return alpha.point(lambda v: 255 if v >= 16 else 0)


def keep_major_components(mask: Image.Image, min_ratio: float = 0.02) -> Image.Image:
    """Drop tiny isolated mask blobs (dashed grid-line remnants).

    The character silhouette is one large connected component; separator dash
    remnants are tiny isolated specks.  Keep only components whose area is at
    least ``min_ratio`` of the largest component.
    """
    w, h = mask.size
    px = mask.load()
    labels = [[0] * w for _ in range(h)]
    areas: dict[int, int] = {}
    next_label = 0
    for sy in range(h):
        for sx in range(w):
            if px[sx, sy] == 0 or labels[sy][sx] != 0:
                continue
            next_label += 1
            label = next_label
            area = 0
            q: deque[tuple[int, int]] = deque([(sx, sy)])
            labels[sy][sx] = label
            while q:
                x, y = q.popleft()
                area += 1
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and px[nx, ny] != 0 and labels[ny][nx] == 0:
                        labels[ny][nx] = label
                        q.append((nx, ny))
            areas[label] = area
    if not areas:
        return mask
    max_area = max(areas.values())
    keep = {label for label, area in areas.items() if area >= max_area * min_ratio}
    out = Image.new("L", (w, h), 0)
    opx = out.load()
    for y in range(h):
        row = labels[y]
        for x in range(w):
            if row[x] in keep and row[x] != 0:
                opx[x, y] = 255
    return out


def content_mask(cell: Image.Image) -> Image.Image:
    """Return a solid-ish mask for non-background content in a white pose cell.

    Works for both soft-gradient art and high-contrast manga line art.
    The key challenge is that character interior whites (shirt, skin) are the
    same color as the background white.  We solve this by dilating the ink
    layer before the flood-fill so that even thin outline strokes form a
    closed barrier, preventing the fill from entering the character interior.
    """
    rgb = cell.convert("RGB")
    white = Image.new("RGB", rgb.size, (255, 255, 255))
    diff = ImageChops.difference(rgb, white).convert("L")
    ink = diff.point(lambda v: 255 if v > 9 else 0)

    # Clear only the outermost cell border (grid separator pixels).
    # Do NOT call remove_grid_lines here: for a full-body character that spans
    # most of the cell height, that function would incorrectly delete vertical
    # columns of the character itself.
    remove_sheet_edges(ink)

    # Dilate the ink layer before flood-fill so that thin outline strokes form
    # a fully closed silhouette.  This prevents the background fill from
    # seeping through micro-gaps in the outline into white shirt / skin areas.
    ink_closed = ink.filter(ImageFilter.MaxFilter(5))

    w, h = ink_closed.size
    ink_px = ink_closed.load()
    outside = Image.new("L", (w, h), 0)
    out_px = outside.load()
    q: deque[tuple[int, int]] = deque()

    def push(x: int, y: int) -> None:
        if x < 0 or x >= w or y < 0 or y >= h:
            return
        if out_px[x, y] != 0 or ink_px[x, y] != 0:
            return
        out_px[x, y] = 255
        q.append((x, y))

    for x in range(w):
        push(x, 0)
        push(x, h - 1)
    for y in range(h):
        push(0, y)
        push(w - 1, y)

    while q:
        x, y = q.popleft()
        push(x + 1, y)
        push(x - 1, y)
        push(x, y + 1)
        push(x, y - 1)

    mask = Image.new("L", (w, h), 0)
    mask_px = mask.load()
    for y in range(h):
        for x in range(w):
            if out_px[x, y] == 0:
                mask_px[x, y] = 255

    # Smooth tiny holes/gaps and trim the border again.
    mask = mask.filter(ImageFilter.MaxFilter(3))
    mask = mask.filter(ImageFilter.MinFilter(3))
    remove_sheet_edges(mask)
    # Dashed separator remnants survive as small isolated blobs; drop them so
    # they can't inflate the bounding box.
    mask = keep_major_components(mask)
    return mask


def grayscale_visible(gray: Image.Image, mask: Image.Image) -> bytes:
    """Store grayscale visible pixels while keeping mask=0 as 0xFF transparent."""
    src = gray.tobytes()
    m = mask.tobytes()
    out = bytearray(len(src))
    for i, value in enumerate(src):
        if m[i] == 0:
            out[i] = 0xFF
        else:
            out[i] = value if value < 0xFF else 0xFE
    return bytes(out)


def convert_sheet(src_path: Path, out_path: Path, preview_path: Path | None = None) -> None:
    src = Image.open(src_path).convert("RGBA")
    cell_w = src.width // ATLAS_COLS
    cell_h = src.height // ATLAS_ROWS
    if cell_w <= 0 or cell_h <= 0:
        raise ValueError(f"invalid pose sheet size: {src.size}")

    # Erase grid separators (incl. dashed remnants) on the full sheet first,
    # so stray separator pixels can't inflate the shared bounding box.
    src = clean_sheet_grid(src, cell_w, cell_h)

    atlas = bytearray([0xFF]) * (ATLAS_W * ATLAS_H)
    preview = Image.new("L", (ATLAS_W, ATLAS_H), 255)

    # Use one shared source crop for all nine cells. Per-pose bbox fitting makes
    # the character visibly change size between poses; a shared crop preserves
    # the original sheet's scale relationship.
    bboxes = []
    for row in range(ATLAS_ROWS):
        for col in range(ATLAS_COLS):
            cell = src.crop((
                col * cell_w,
                row * cell_h,
                (col + 1) * cell_w,
                (row + 1) * cell_h,
            ))
            mask = alpha_mask(cell) or content_mask(cell)
            bbox = mask.getbbox()
            if bbox:
                bboxes.append(bbox)
    if not bboxes:
        raise ValueError(f"no visible outfit content found: {src_path}")

    src_crop = (
        max(0, min(b[0] for b in bboxes) - 12),
        max(0, min(b[1] for b in bboxes) - 12),
        min(cell_w, max(b[2] for b in bboxes) + 12),
        min(cell_h, max(b[3] for b in bboxes) + 12),
    )
    crop_w = src_crop[2] - src_crop[0]
    crop_h = src_crop[3] - src_crop[1]
    scale = min((POSE_W - 8) / crop_w, (POSE_H - 4) / crop_h)
    target_w = max(1, int(crop_w * scale + 0.5))
    target_h = max(1, int(crop_h * scale + 0.5))
    print(f"  shared crop={src_crop}, target={target_w}x{target_h}")

    for row in range(ATLAS_ROWS):
        for col in range(ATLAS_COLS):
            cell = src.crop((col * cell_w, row * cell_h,
                             (col + 1) * cell_w, (row + 1) * cell_h)).crop(src_crop)

            mask = alpha_mask(cell) or content_mask(cell)

            # Fit to the firmware's 200x300 pose cell. Bottom-align the pose so
            # shoes stay on the ground like the previous outfit atlases.
            fitted = cell.resize((target_w, target_h), Image.Resampling.LANCZOS)
            fitted_mask = mask.resize((target_w, target_h), Image.Resampling.LANCZOS)
            fitted_mask = fitted_mask.point(lambda v: 255 if v > 12 else 0)

            canvas = Image.new("RGBA", (POSE_W, POSE_H), (0, 0, 0, 0))
            mask_canvas = Image.new("L", (POSE_W, POSE_H), 0)
            px = (POSE_W - fitted.width) // 2
            py = POSE_H - fitted.height - 2
            canvas.alpha_composite(fitted, (px, py))
            mask_canvas.paste(fitted_mask, (px, py))

            gray = canvas.convert("L")
            visible = grayscale_visible(gray, mask_canvas)

            for y in range(POSE_H):
                src_off = y * POSE_W
                dst_y = row * POSE_H + y
                dst_x = col * POSE_W
                dst_off = dst_y * ATLAS_W + dst_x
                atlas[dst_off : dst_off + POSE_W] = visible[src_off : src_off + POSE_W]

    # --- Pack 8-bit atlas into 2-bit per pixel ---
    # Codes: 0=transparent(0xFF), 1=black(0x00), 2=white(0xFE), 3=gray(mid)
    # 4 pixels per byte, MSB first: byte = c[0]<<6 | c[1]<<4 | c[2]<<2 | c[3]
    def _code(v: int) -> int:
        if v == 0xFF: return 0
        if v == 0x00: return 1
        if v >= 0xFE: return 2
        return 1 if v < 128 else 2  # quantize gray to nearest black/white

    n = len(atlas)
    packed = bytearray((n + 3) // 4)
    for i in range(0, n, 4):
        byte = 0
        for j in range(4):
            if i + j < n:
                byte |= _code(atlas[i + j]) << (6 - j * 2)
        packed[i // 4] = byte

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        f.write(struct.pack("<II", ATLAS_W, ATLAS_H))
        f.write(bytes(packed))

    transparent = sum(1 for b in atlas if b == 0xFF)
    black = sum(1 for b in atlas if b == 0x00)
    white = sum(1 for b in atlas if b == 0xFE)
    orig_kb = (8 + n) // 1024
    new_kb = out_path.stat().st_size // 1024
    print(f"{src_path.name} -> {out_path}")
    print(f"  2-bit packed: {new_kb} KB  (was ~{orig_kb} KB, {orig_kb // max(new_kb,1)}x smaller)")
    print(f"  pixel stats: transparent={transparent}, black={black}, white={white}")

    if preview_path:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview.putdata([255 if b == 0xFF else b for b in atlas])
        preview.save(preview_path)
        print(f"  preview={preview_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert 3x3 outfit sheet to Aura outfit .bin")
    parser.add_argument("src", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    convert_sheet(args.src, args.out, args.preview)


if __name__ == "__main__":
    main()
