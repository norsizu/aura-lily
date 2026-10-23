#!/usr/bin/env python3
"""Convert Aura 3x3 outfit sheets into the device-approved RLCD layout.

The portrait scale, face anchor, vertical placement, and Bayer phase are kept
compatible with the 2026-08-27 on-device approval.  The atlas cells are wider
than the visible portrait so asymmetric gestures (for example an outstretched
hand) can be retained without changing the face position on screen.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter


OUTFITS = (
    ("睡衣.png", "pajama.bin"),
    ("连衣裙.png", "dress.bin"),
    ("睡裙.png", "nightdress.bin"),
    ("休闲装.png", "casual_a.bin"),
    ("职业装.png", "professional.bin"),
    ("冬装.png", "winter.bin"),
    ("旗袍.png", "qipao.bin"),
    ("马面裙.png", "mamian.bin"),
    ("汉服.png", "hanfu.bin"),
)

SOURCE_SIZE = 1254
SOURCE_CELL = 418
# Keep transparent horizontal headroom in each cell.  The renderer centres the
# complete cell, while each pose below stores its own x placement inside it.
CELL_W = 320
CELL_H = 320
SOURCE_CROP_W = 380
CROP_HALF_W = 175  # approved crop half-width used as the placement baseline
CROP_TOP = 120
CROP_BOTTOM = 315
# Preserve the old source-to-screen scale (220 px from a 350 px source crop).
REFERENCE_CROP_W = CROP_HALF_W * 2
REFERENCE_TARGET_W = 220
SOURCE_TO_SCREEN = REFERENCE_TARGET_W / REFERENCE_CROP_W
TARGET_W = round(SOURCE_CROP_W * SOURCE_TO_SCREEN)
TARGET_H = round((CROP_TOP + CROP_BOTTOM) * SOURCE_TO_SCREEN)
CELL_Y = CELL_H - TARGET_H - 26
SCREEN_X = (400 - CELL_W) // 2 - 25
SCREEN_Y = 300 - CELL_H + 25
REFERENCE_SCREEN_X = (400 - REFERENCE_TARGET_W) // 2 - 25
REFERENCE_CELL_X = 28
REFERENCE_FACE_SCREEN_X = (
    REFERENCE_SCREEN_X + REFERENCE_CELL_X + REFERENCE_TARGET_W / 2
)
CROP_MARGIN = 4

# Preserve the approved per-pose framing relative to the detected face region.
# Order: neutral, proud, thinking, surprised, apologetic, assertive, shy,
# excited, relaxed.
POSE_ANCHOR_DELTA = (
    (28, -72),
    (27, -71),
    (51, -63),
    (28, -62),
    (45, -59),
    (27, -71),
    (44, -65),
    (34, -56),
    (28, -72),
)

BAYER8 = (
    np.array(
        [
            [0, 32, 8, 40, 2, 34, 10, 42],
            [48, 16, 56, 24, 50, 18, 58, 26],
            [12, 44, 4, 36, 14, 46, 6, 38],
            [60, 28, 52, 20, 62, 30, 54, 22],
            [3, 35, 11, 43, 1, 33, 9, 41],
            [51, 19, 59, 27, 49, 17, 57, 25],
            [15, 47, 7, 39, 13, 45, 5, 37],
            [63, 31, 55, 23, 61, 29, 53, 21],
        ],
        dtype=np.float32,
    )
    + 0.5
) / 64.0 * 255.0


def face_centroid(cell: np.ndarray) -> tuple[float, float] | None:
    """Find the enclosed light face region; return None if clothing joins it."""
    height, width = cell.shape
    white = cell >= 246
    seen = np.zeros_like(white, dtype=bool)
    candidates: list[tuple[int, float, float]] = []

    for seed_y in range(40, 225):
        for seed_x in range(70, 340):
            if not white[seed_y, seed_x] or seen[seed_y, seed_x]:
                continue
            queue = deque([(seed_y, seed_x)])
            seen[seed_y, seed_x] = True
            points: list[tuple[int, int]] = []
            touches_edge = False
            while queue:
                y, x = queue.popleft()
                points.append((y, x))
                touches_edge |= x in (0, width - 1) or y in (0, height - 1)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    next_y, next_x = y + dy, x + dx
                    if (
                        0 <= next_y < height
                        and 0 <= next_x < width
                        and white[next_y, next_x]
                        and not seen[next_y, next_x]
                    ):
                        seen[next_y, next_x] = True
                        queue.append((next_y, next_x))

            if touches_edge or not 2500 <= len(points) <= 13000:
                continue
            ys = np.fromiter((point[0] for point in points), dtype=np.int16)
            xs = np.fromiter((point[1] for point in points), dtype=np.int16)
            box_w = int(xs.max() - xs.min() + 1)
            box_h = int(ys.max() - ys.min() + 1)
            center_x = float(xs.mean())
            center_y = float(ys.mean())
            if 70 <= box_w <= 150 and 70 <= box_h <= 170 and center_y < 210:
                candidates.append((len(points), center_x, center_y))

    if not candidates:
        return None
    _area, center_x, center_y = max(candidates)
    return center_x, center_y


def crop_with_white_padding(
    cell: Image.Image, left: int, top: int, width: int, height: int
) -> Image.Image:
    output = Image.new("L", (width, height), 255)
    src_left = max(0, left)
    src_top = max(0, top)
    src_right = min(cell.width, left + width)
    src_bottom = min(cell.height, top + height)
    if src_right > src_left and src_bottom > src_top:
        output.paste(
            cell.crop((src_left, src_top, src_right, src_bottom)),
            (src_left - left, src_top - top),
        )
    return output


def source_content_bbox(cell: np.ndarray) -> tuple[int, int, int, int]:
    """Return the dark-content bounds of one source pose.

    The source sheets use a light paper-like background (typically 252-253),
    so 230 includes antialiased character lines without treating that
    background texture as character content.
    """
    ys, xs = np.where(cell < 230)
    if xs.size == 0:
        raise ValueError("source pose contains no visible character content")
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def pose_crop_left(cell: np.ndarray, anchor_x: int) -> int:
    """Choose the smallest horizontal crop shift that keeps the pose visible."""
    bbox_left, _bbox_top, bbox_right, _bbox_bottom = source_content_bbox(cell)
    if bbox_right - bbox_left + 1 + CROP_MARGIN * 2 > SOURCE_CROP_W:
        raise ValueError(
            "source pose is wider than the supported crop: "
            f"{bbox_right - bbox_left + 1}px"
        )

    # Start at the old face-centred crop and move only as far as needed to
    # include both edges of the gesture. White padding handles source-cell
    # boundaries for poses that touch an edge.
    baseline_left = anchor_x - CROP_HALF_W
    min_left = bbox_right + 1 + CROP_MARGIN - SOURCE_CROP_W
    max_left = bbox_left - CROP_MARGIN
    return min(max(baseline_left, min_left), max_left)


def pose_cell_x(cell: np.ndarray, anchor_x: int) -> tuple[int, int]:
    """Return (source crop left, atlas-cell x placement) for one pose.

    ``REFERENCE_FACE_SCREEN_X`` is the face anchor produced by the previous
    220px cell layout.  Re-solving the placement from that anchor preserves
    the established on-screen coordinates even when the crop shifts.
    """
    crop_left = pose_crop_left(cell, anchor_x)
    face_offset = (anchor_x - crop_left) * SOURCE_TO_SCREEN
    cell_x = round(REFERENCE_FACE_SCREEN_X - SCREEN_X - face_offset)
    if cell_x < 0 or cell_x + TARGET_W > CELL_W:
        raise ValueError(
            f"pose placement does not fit cell: x={cell_x}, "
            f"width={TARGET_W}, cell={CELL_W}"
        )
    return crop_left, cell_x


def pose_geometry(cell: np.ndarray, anchor_x: int) -> dict[str, int]:
    """Return auditable crop and placement values for one source pose."""
    crop_left, cell_x = pose_cell_x(cell, anchor_x)
    bbox_left, bbox_top, bbox_right, bbox_bottom = source_content_bbox(cell)
    return {
        "crop_left": crop_left,
        "cell_x": cell_x,
        "bbox_left": bbox_left,
        "bbox_top": bbox_top,
        "bbox_right": bbox_right,
        "bbox_bottom": bbox_bottom,
    }


def solid_character_mask(image: Image.Image) -> np.ndarray:
    gray = np.asarray(image, dtype=np.uint8)
    ink = (gray < 210).astype(np.uint8) * 255
    closed = np.asarray(Image.fromarray(ink).filter(ImageFilter.MaxFilter(7))).copy()

    bottom_columns = np.where(closed[-30:, :] > 0)[1]
    if bottom_columns.size:
        left = max(0, int(bottom_columns.min()) - 5)
        right = min(closed.shape[1], int(bottom_columns.max()) + 6)
        closed[-10:, left:right] = 255

    height, width = closed.shape
    outside = np.zeros((height, width), dtype=bool)
    queue: deque[tuple[int, int]] = deque()

    def add(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height and not outside[y, x] and closed[y, x] == 0:
            outside[y, x] = True
            queue.append((x, y))

    for x in range(width):
        add(x, 0)
    for y in range(height):
        add(0, y)
        add(width - 1, y)
    for x in range(width):
        add(x, height - 1)
    while queue:
        x, y = queue.popleft()
        add(x + 1, y)
        add(x - 1, y)
        add(x, y + 1)
        add(x, y - 1)

    foreground = (~outside).astype(np.uint8) * 255
    for y in range(int(height * 0.40), height):
        columns = np.where(foreground[y, :] > 0)[0]
        if columns.size >= 2:
            foreground[y, columns[0] : columns[-1] + 1] = 255
    return np.asarray(Image.fromarray(foreground).filter(ImageFilter.MaxFilter(3)))


def pack_2bit(atlas: np.ndarray) -> bytes:
    codes = np.full(atlas.shape, 3, dtype=np.uint8)
    codes[atlas == 0xFF] = 0
    codes[atlas == 0x00] = 1
    codes[atlas == 0xFE] = 2
    if np.any(codes == 3):
        raise ValueError("atlas contains pixels outside transparent/black/white")
    flat = codes.ravel()
    packed = bytearray((flat.size + 3) // 4)
    for offset in range(0, flat.size, 4):
        value = 0
        for index in range(4):
            if offset + index < flat.size:
                value |= int(flat[offset + index]) << (6 - index * 2)
        packed[offset // 4] = value
    return bytes(packed)


def build_outfit(
    source: Image.Image, anchors: list[tuple[int, int]]
) -> tuple[bytes, Image.Image]:
    atlas = np.full((CELL_H * 3, CELL_W * 3), 0xFF, dtype=np.uint8)
    for pose, (anchor_x, anchor_y) in enumerate(anchors):
        row, column = divmod(pose, 3)
        source_cell = source.crop(
            (
                column * SOURCE_CELL,
                row * SOURCE_CELL,
                (column + 1) * SOURCE_CELL,
                (row + 1) * SOURCE_CELL,
            )
        )
        crop_left, cell_x = pose_cell_x(np.asarray(source_cell), anchor_x)
        crop = crop_with_white_padding(
            source_cell,
            crop_left,
            anchor_y - CROP_TOP,
            SOURCE_CROP_W,
            CROP_TOP + CROP_BOTTOM,
        )
        resized = crop.resize((TARGET_W, TARGET_H), Image.Resampling.LANCZOS)
        gray = np.asarray(resized, dtype=np.float32)
        mask = solid_character_mask(resized)

        foreground = np.full((TARGET_H, TARGET_W), 0xFE, dtype=np.uint8)
        for y in range(TARGET_H):
            screen_y = SCREEN_Y + CELL_Y + y
            for x in range(TARGET_W):
                screen_x = SCREEN_X + cell_x + x
                luma = gray[y, x]
                if luma < 110:
                    black = True
                elif luma > 165:
                    black = False
                else:
                    black = luma <= BAYER8[screen_y & 7, screen_x & 7]
                foreground[y, x] = 0x00 if black else 0xFE

        output_cell = np.full((CELL_H, CELL_W), 0xFF, dtype=np.uint8)
        visible_w = min(TARGET_W, CELL_W - cell_x)
        visible_h = min(TARGET_H, CELL_H - CELL_Y)
        output_cell[CELL_Y : CELL_Y + visible_h, cell_x : cell_x + visible_w] = np.where(
            mask[:visible_h, :visible_w] > 0,
            foreground[:visible_h, :visible_w],
            0xFF,
        )
        atlas[
            row * CELL_H : (row + 1) * CELL_H,
            column * CELL_W : (column + 1) * CELL_W,
        ] = output_cell

    data = struct.pack("<II", CELL_W * 3, CELL_H * 3) + pack_2bit(atlas)
    expected_size = 8 + (CELL_W * 3 * CELL_H * 3 + 3) // 4
    if len(data) != expected_size:
        raise ValueError(f"unexpected atlas size: {len(data)}")
    return data, Image.fromarray(atlas)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--preview-dir", type=Path, required=True)
    args = parser.parse_args()

    sources: dict[str, tuple[Path, Image.Image, list[tuple[float, float] | None]]] = {}
    detected_by_pose: list[list[tuple[float, float]]] = [[] for _ in range(9)]
    for source_name, output_name in OUTFITS:
        source_path = args.source_dir / source_name
        image = Image.open(source_path).convert("L")
        if image.size != (SOURCE_SIZE, SOURCE_SIZE):
            raise ValueError(f"{source_path}: expected 1254x1254, got {image.size}")
        pixels = np.asarray(image)
        faces: list[tuple[float, float] | None] = []
        for pose in range(9):
            row, column = divmod(pose, 3)
            face = face_centroid(
                pixels[
                    row * SOURCE_CELL : (row + 1) * SOURCE_CELL,
                    column * SOURCE_CELL : (column + 1) * SOURCE_CELL,
                ]
            )
            faces.append(face)
            if face is not None:
                detected_by_pose[pose].append(face)
        sources[output_name] = (source_path, image, faces)

    fallback_faces: list[tuple[float, float]] = []
    for pose, faces in enumerate(detected_by_pose):
        if not faces:
            raise ValueError(f"could not detect any face for pose {pose}")
        fallback_faces.append(
            (float(np.median([face[0] for face in faces])), float(np.median([face[1] for face in faces])))
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.preview_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "layout": "RLCD 320x320 cells / 239x273 portrait / face-anchored Bayer 8x8 Smooth",
        "cell_size": [CELL_W, CELL_H],
        "portrait_size": [TARGET_W, TARGET_H],
        "source_crop": [SOURCE_CROP_W, CROP_TOP + CROP_BOTTOM],
        "crop_margin": CROP_MARGIN,
        "screen_anchor": [SCREEN_X, SCREEN_Y],
        "reference_face_screen_x": REFERENCE_FACE_SCREEN_X,
        "cell_y": CELL_Y,
        "outfits": {},
    }
    for output_name, (source_path, image, faces) in sources.items():
        anchors: list[tuple[int, int]] = []
        for pose, face in enumerate(faces):
            face_x, face_y = face or fallback_faces[pose]
            delta_x, delta_y = POSE_ANCHOR_DELTA[pose]
            anchors.append((round(face_x + delta_x), round(face_y + delta_y)))
        data, preview = build_outfit(image, anchors)
        output_path = args.output_dir / output_name
        output_path.write_bytes(data)
        preview.save(args.preview_dir / output_name.replace(".bin", ".png"))
        manifest["outfits"][output_name] = {
            "source": str(source_path),
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "anchors": anchors,
            "pose_geometry": [
                pose_geometry(
                    np.asarray(
                        image.crop(
                            (
                                (pose % 3) * SOURCE_CELL,
                                (pose // 3) * SOURCE_CELL,
                                (pose % 3 + 1) * SOURCE_CELL,
                                (pose // 3 + 1) * SOURCE_CELL,
                            )
                        )
                    ),
                    anchor[0],
                )
                for pose, anchor in enumerate(anchors)
            ],
            "face_fallbacks": [face is None for face in faces],
            "output_sha256": hashlib.sha256(data).hexdigest(),
        }
        print(f"{source_path.name} -> {output_name}: {CELL_W * 3}x{CELL_H * 3}, {len(data)} bytes")

    (args.preview_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
