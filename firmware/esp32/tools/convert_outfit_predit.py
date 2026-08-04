#!/usr/bin/env python3
"""
Aura outfit offline pre-dithering tool  — v2
==================================================
Generates 6 candidate 1-bit outfit atlases from a transparent PNG.

WHY THIS EXISTS
---------------
The current converter stores grayscale pixels; the ESP32 firmware applies
Floyd-Steinberg dithering every render frame.  On a reflective 1-bit LCD the
dense half-tone noise looks muddy — especially pale anime watercolour fills
and thin antialiased outlines.

This tool pre-dithers offline so the stored .bin already contains only
0x00 (black) / 0xFE (white) / 0xFF (transparent).  When the firmware's
existing dither_character_to_graybuf() reads those values the quantisation
error is always 0, making it a no-op.  Zero firmware changes required.

DITHER MODES
------------
1. thresh128        Plain threshold at 128
2. thresh_boost     Gamma-darken (γ=1.6) then threshold — pulls thin lines to black
3. bayer4           4×4 Bayer ordered dither
4. bayer8           8×8 Bayer ordered dither
5. hybrid_soft      Dark ≤80→black, bright ≥200→white, midtones via Bayer-8
6. hybrid_ink       Dark ≤100→black, bright ≥220→white, midtones via Bayer-8
                    (most aggressive line preservation — recommended for anime art)

OUTPUT (per mode)
-----------------
  outfits/<name>_<mode>.bin        firmware-compatible atlas (.bin)
  outfits/preview_<mode>.png       9-pose preview at 1:1 screen pixels
  outfits/preview_<mode>_scene.png  single pose 4 composited on mock scene

Additionally a side-by-side comparison sheet is written:
  outfits/comparison_pose4.png     all 6 modes of pose 4 in one image

BINARY FORMAT (unchanged from current firmware)
------------------------------------------------
  uint32_le width   (= ATLAS_COLS * POSE_W)
  uint32_le height  (= ATLAS_ROWS * POSE_H)
  raw 8-bit pixels  — 0xFF transparent, 0xFE white, 0x00 black

USAGE
-----
  python3 convert_outfit_predit.py <source.png> [--out-dir <dir>] [--pose-w 200] [--pose-h 300]

Default output dir is firmware/esp32/assets/outfits/candidates/.
"""
from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Atlas / pose constants (match firmware)
# ---------------------------------------------------------------------------
ATLAS_COLS = 3
ATLAS_ROWS = 3
DEFAULT_POSE_W = 200
DEFAULT_POSE_H = 300

# ---------------------------------------------------------------------------
# Bayer matrices (normalised to 0–255 threshold values)
# ---------------------------------------------------------------------------
_BAYER4_RAW = np.array([
    [ 0,  8,  2, 10],
    [12,  4, 14,  6],
    [ 3, 11,  1,  9],
    [15,  7, 13,  5],
], dtype=np.float32)
BAYER4 = (_BAYER4_RAW + 0.5) / 16.0 * 255.0  # 0–255 thresholds

_BAYER8_RAW = np.array([
    [ 0, 32,  8, 40,  2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44,  4, 36, 14, 46,  6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [ 3, 35, 11, 43,  1, 33,  9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47,  7, 39, 13, 45,  5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=np.float32)
BAYER8 = (_BAYER8_RAW + 0.5) / 64.0 * 255.0  # 0–255 thresholds


# ---------------------------------------------------------------------------
# Dithering functions  (input: np.float32 array H×W in [0,255], alpha H×W bool)
# returns:  np.uint8 H×W where 0=black, 254=white, 255=transparent
# ---------------------------------------------------------------------------

def _to_firmware_pixel(bits: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """
    bits:  bool array (True=black, False=white)
    alpha: bool array (True=visible)
    returns uint8: 0=black, 0xFE=white, 0xFF=transparent
    """
    result = np.full(bits.shape, 0xFF, dtype=np.uint8)
    result[alpha & bits]  = 0x00
    result[alpha & ~bits] = 0xFE
    return result


def dither_thresh128(luma: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Plain threshold at 128."""
    return _to_firmware_pixel(luma < 128, alpha)


def dither_thresh_boost(luma: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Gamma-darken (γ=1.6) to pull soft outlines toward black, then threshold."""
    boosted = np.power(luma / 255.0, 1.6) * 255.0
    return _to_firmware_pixel(boosted < 128, alpha)


def _bayer_dither(luma: np.ndarray, alpha: np.ndarray, bayer: np.ndarray) -> np.ndarray:
    h, w = luma.shape
    bh, bw = bayer.shape
    # Tile the Bayer matrix over the full image
    tiled = np.tile(bayer, ((h + bh - 1) // bh, (w + bw - 1) // bw))[:h, :w]
    # Pixel is black when luma <= Bayer threshold
    bits = luma <= tiled
    return _to_firmware_pixel(bits, alpha)


def dither_bayer4(luma: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    return _bayer_dither(luma, alpha, BAYER4)


def dither_bayer8(luma: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    return _bayer_dither(luma, alpha, BAYER8)


def _hybrid_dither(luma: np.ndarray, alpha: np.ndarray,
                   black_lock: float, white_lock: float,
                   bayer: np.ndarray) -> np.ndarray:
    """
    Force dark pixels to black, bright pixels to white, dither midtones with Bayer.
    This preserves anime line art cleanly while giving clothing halftone texture.
    """
    h, w = luma.shape
    bh, bw = bayer.shape
    tiled = np.tile(bayer, ((h + bh - 1) // bh, (w + bw - 1) // bw))[:h, :w]

    bits = np.zeros((h, w), dtype=bool)
    bits[luma < black_lock] = True                          # force black
    # force white: bits[luma > white_lock] stays False
    # midtones: bayer
    mid = (luma >= black_lock) & (luma <= white_lock)
    bits[mid] = luma[mid] <= tiled[mid]

    return _to_firmware_pixel(bits, alpha)


def dither_hybrid_soft(luma: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """black≤80, white≥200, midtones Bayer-8."""
    return _hybrid_dither(luma, alpha, 80.0, 200.0, BAYER8)


def dither_hybrid_ink(luma: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """black≤100, white≥220, midtones Bayer-8.  Most ink-like for anime art."""
    return _hybrid_dither(luma, alpha, 100.0, 220.0, BAYER8)


MODES: dict[str, callable] = {
    "thresh128":    dither_thresh128,
    "thresh_boost": dither_thresh_boost,
    "bayer4":       dither_bayer4,
    "bayer8":       dither_bayer8,
    "hybrid_soft":  dither_hybrid_soft,
    "hybrid_ink":   dither_hybrid_ink,
}


# ---------------------------------------------------------------------------
# Source art loading helpers
# ---------------------------------------------------------------------------

def load_and_crop(src_path: Path, pose_w: int, pose_h: int):
    """
    Load a 3×3 pose sheet PNG, compute shared crop + scale (same logic as
    convert_outfit_sheet.py so character size stays consistent), return a list
    of 9 (luma_arr, alpha_arr) tuples at (pose_h, pose_w) shape.
    """
    src = Image.open(src_path).convert("RGBA")
    cw = src.width  // ATLAS_COLS
    ch = src.height // ATLAS_ROWS

    # --- shared crop bbox from alpha channel ---
    bboxes = []
    for row in range(ATLAS_ROWS):
        for col in range(ATLAS_COLS):
            cell = src.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))
            alpha = cell.getchannel("A")
            lo, hi = alpha.getextrema()
            if hi <= 10:
                continue
            mask = alpha.point(lambda v: 255 if v >= 16 else 0)
            bbox = mask.getbbox()
            if bbox:
                bboxes.append(bbox)

    if not bboxes:
        raise ValueError("No visible alpha content found in source PNG")

    # expand 12px each side, clamp to cell bounds
    src_crop = (
        max(0, min(b[0] for b in bboxes) - 12),
        max(0, min(b[1] for b in bboxes) - 12),
        min(cw, max(b[2] for b in bboxes) + 12),
        min(ch, max(b[3] for b in bboxes) + 12),
    )
    crop_w = src_crop[2] - src_crop[0]
    crop_h = src_crop[3] - src_crop[1]
    scale = min((pose_w - 8) / crop_w, (pose_h - 4) / crop_h)
    target_w = max(1, int(crop_w * scale + 0.5))
    target_h = max(1, int(crop_h * scale + 0.5))
    print(f"  Shared crop: {src_crop}  →  target cell: {target_w}×{target_h}")

    poses = []
    for row in range(ATLAS_ROWS):
        for col in range(ATLAS_COLS):
            cell = src.crop(
                (col * cw, row * ch, (col + 1) * cw, (row + 1) * ch)
            ).crop(src_crop)

            # scale with LANCZOS for best downsampling quality
            fitted = cell.resize((target_w, target_h), Image.Resampling.LANCZOS)

            # Place bottom-centred in the pose canvas
            canvas_rgba = Image.new("RGBA", (pose_w, pose_h), (255, 255, 255, 0))
            px = (pose_w - target_w) // 2
            py = pose_h - target_h - 2
            canvas_rgba.alpha_composite(fitted, (px, py))

            arr = np.array(canvas_rgba, dtype=np.float32)   # H W 4
            alpha_bool = arr[:, :, 3] >= 16                 # visible pixels
            r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
            luma = 0.299 * r + 0.587 * g + 0.114 * b       # float32 0-255
            poses.append((luma, alpha_bool))

    return poses


# ---------------------------------------------------------------------------
# Atlas packing + binary output
# ---------------------------------------------------------------------------

def poses_to_bin(cells: list[np.ndarray], pose_w: int, pose_h: int) -> bytes:
    """
    cells: list of 9 uint8 arrays (pose_h, pose_w) with 0/0xFE/0xFF convention.
    returns bytes in firmware atlas format.
    """
    atlas_w = ATLAS_COLS * pose_w
    atlas_h = ATLAS_ROWS * pose_h
    atlas = np.full((atlas_h, atlas_w), 0xFF, dtype=np.uint8)
    for idx, cell in enumerate(cells):
        row, col = divmod(idx, ATLAS_COLS)
        y0 = row * pose_h
        x0 = col * pose_w
        atlas[y0:y0 + pose_h, x0:x0 + pose_w] = cell
    header = struct.pack("<II", atlas_w, atlas_h)
    return header + atlas.tobytes()


# ---------------------------------------------------------------------------
# Preview generation helpers
# ---------------------------------------------------------------------------

def cell_to_preview_pil(cell: np.ndarray) -> Image.Image:
    """Convert firmware-convention uint8 cell to grayscale PIL for preview."""
    img = np.zeros(cell.shape, dtype=np.uint8)
    img[cell == 0x00] = 0
    img[cell == 0xFE] = 255
    img[cell == 0xFF] = 200   # show transparent as mid-gray in preview
    return Image.fromarray(img, "L")


def make_9pose_preview(cells: list[np.ndarray], pose_w: int, pose_h: int) -> Image.Image:
    """3×3 grid of all poses, transparent shown as light gray."""
    atlas_w = ATLAS_COLS * pose_w
    atlas_h = ATLAS_ROWS * pose_h
    out = Image.new("L", (atlas_w, atlas_h), 200)
    for idx, cell in enumerate(cells):
        row, col = divmod(idx, ATLAS_COLS)
        tile = cell_to_preview_pil(cell)
        out.paste(tile, (col * pose_w, row * pose_h))
    return out


def make_scene_mockup(cell: np.ndarray, pose_w: int, pose_h: int) -> Image.Image:
    """
    Simulate a 400×300 screen scene:
      - background (dark gray 80, simulating an outdoor scene)
      - character composited at firmware x-offset
      - right UI strip (light 230, simulating a dialogue panel)
    Final image is threshold-converted to 1-bit for accurate screen preview.
    """
    SCREEN_W, SCREEN_H = 400, 300
    scene = np.full((SCREEN_H, SCREEN_W), 80, dtype=np.uint8)   # dark background

    # right panel (x 270..400 approx)
    scene[:, 270:] = 230

    # character placement (mirrors firmware: x_off = (400-pose_w)//2 - 25, y_off = 300-pose_h)
    x_off = (SCREEN_W - pose_w) // 2 - 25
    y_off = SCREEN_H - pose_h
    x_off = max(0, x_off)
    y_off = max(0, y_off)

    # composite character pixels over scene (skip transparent)
    for dy in range(min(pose_h, SCREEN_H - y_off)):
        for dx in range(min(pose_w, SCREEN_W - x_off)):
            px = cell[dy, dx]
            if px != 0xFF:
                scene[y_off + dy, x_off + dx] = (0 if px == 0x00 else 255)

    # convert to 1-bit via threshold (this is what the LCD displays)
    onebpp = ((scene < 128).astype(np.uint8)) * 0
    preview_1bit = np.where(scene < 128, 0, 255).astype(np.uint8)
    return Image.fromarray(preview_1bit, "L")


def make_comparison(per_mode_pose4: dict[str, np.ndarray],
                    pose_w: int, pose_h: int) -> Image.Image:
    """
    Horizontal strip: 6 scene mockups of pose 4 (one per mode) + labels.
    Final size: 6 × 400 wide × (300 + 20) tall.
    """
    SCREEN_W, SCREEN_H = 400, 300
    LABEL_H = 18
    n = len(per_mode_pose4)
    out = Image.new("L", (SCREEN_W * n, SCREEN_H + LABEL_H), 240)

    from PIL import ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
    except Exception:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(out)
    for i, (mode, cell) in enumerate(per_mode_pose4.items()):
        x_base = i * SCREEN_W
        mockup = make_scene_mockup(cell, pose_w, pose_h)
        out.paste(mockup, (x_base, LABEL_H))
        draw.text((x_base + 4, 2), mode, font=font, fill=0)
        # vertical separator
        if i > 0:
            draw.line([(x_base, 0), (x_base, SCREEN_H + LABEL_H)], fill=128, width=1)

    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aura outfit pre-dithering tool — generates 1-bit asset candidates"
    )
    parser.add_argument("src", type=Path, help="Source transparent 3×3 pose sheet PNG")
    parser.add_argument(
        "--out-dir", type=Path,
        default=Path(__file__).parent.parent / "assets" / "outfits" / "candidates",
        help="Output directory (default: assets/outfits/candidates/)"
    )
    parser.add_argument("--pose-w", type=int, default=DEFAULT_POSE_W)
    parser.add_argument("--pose-h", type=int, default=DEFAULT_POSE_H)
    parser.add_argument("--stem", default=None,
                        help="Output file stem (default: source filename without extension)")
    args = parser.parse_args()

    stem = args.stem or args.src.stem
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading {args.src} …")
    poses_raw = load_and_crop(args.src, args.pose_w, args.pose_h)

    comparison_cells: dict[str, np.ndarray] = {}

    for mode_name, dither_fn in MODES.items():
        print(f"\n[{mode_name}]")
        cells = []
        for luma, alpha in poses_raw:
            cells.append(dither_fn(luma, alpha))

        # .bin
        bin_path = out_dir / f"{stem}_{mode_name}.bin"
        bin_data = poses_to_bin(cells, args.pose_w, args.pose_h)
        bin_path.write_bytes(bin_data)
        transparent = sum(1 for b in bin_data[8:] if b == 0xFF)
        black       = sum(1 for b in bin_data[8:] if b == 0x00)
        white       = sum(1 for b in bin_data[8:] if b == 0xFE)
        print(f"  {bin_path}  ({bin_path.stat().st_size // 1024} KB)")
        print(f"  transparent={transparent:,}  black={black:,}  white={white:,}")

        # 9-pose preview
        preview_path = out_dir / f"preview_{mode_name}.png"
        make_9pose_preview(cells, args.pose_w, args.pose_h).save(preview_path)
        print(f"  9-pose preview → {preview_path}")

        # scene mockup for pose 4 (neutral)
        scene_path = out_dir / f"scene_{mode_name}.png"
        make_scene_mockup(cells[4], args.pose_w, args.pose_h).save(scene_path)
        print(f"  scene mockup   → {scene_path}")

        comparison_cells[mode_name] = cells[4]

    # comparison strip
    comp_path = out_dir / "comparison_pose4.png"
    make_comparison(comparison_cells, args.pose_w, args.pose_h).save(comp_path)
    print(f"\nComparison strip → {comp_path}")
    print("\nDone.  Candidates ready for evaluation.")
    print("Recommended first-try: hybrid_ink")


if __name__ == "__main__":
    main()
