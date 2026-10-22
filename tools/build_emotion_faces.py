#!/usr/bin/env python3
"""把 8 张情绪脸 PNG 打成固件资源 emotion_115.bin（E4）。

输入目录里放 8 张 PNG，文件名（不含扩展名）就是 E1 情绪名：

    neutral.png  happy.png  excited.png  shy.png
    sad.png      worried.png  pouty.png  surprised.png

每张图居中裁成正方形 → 缩放到 115×115 → 转 8 位灰度 → 按上面的顺序拼接成
``8 × 115 × 115 = 105800`` 字节的裸灰度数据，固件直接 ``fread`` 进 PSRAM。
缺任何一张就报错退出，不生成半个文件。

用法::

    python3 tools/build_emotion_faces.py <PNG 目录> [-o emotion_115.bin]
    python3 tools/build_emotion_faces.py <PNG 目录> --preview preview.png

不给 -o 时写到 ``firmware/esp32/assets/emotion_115.bin``（相对仓库根，与 cwd 无关），
下次 idf.py build 会自动把它打进 assets 分区。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

# E1 情绪集合——顺序即 bin 里的资源索引，固件 protocol/messages.h 靠它查脸。
EMOTIONS = (
    "neutral",
    "happy",
    "excited",
    "shy",
    "sad",
    "worried",
    "pouty",
    "surprised",
)

FACE_SIZE = 115
FACE_BYTES = FACE_SIZE * FACE_SIZE
BIN_BYTES = len(EMOTIONS) * FACE_BYTES  # 105800

DEFAULT_OUTPUT = ROOT / "firmware" / "esp32" / "assets" / "emotion_115.bin"

try:  # Pillow >= 9.1
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - 老版本 Pillow
    _RESAMPLE = Image.LANCZOS


def center_square(img: Image.Image) -> Image.Image:
    """居中裁成正方形（不拉伸，避免脸被压扁）。"""
    width, height = img.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return img.crop((left, top, left + side, top + side))


def load_emotion_face(path: Path) -> Image.Image:
    """读一张 PNG → 居中正方 → 115×115 8 位灰度。"""
    with Image.open(path) as handle:
        img = handle.convert("L")
    return center_square(img).resize((FACE_SIZE, FACE_SIZE), _RESAMPLE)


def missing_emotions(src_dir: Path) -> list[str]:
    """列出 src_dir 里还缺哪些 E1 情绪的 PNG（按 EMOTIONS 顺序）。"""
    return [name for name in EMOTIONS if not (src_dir / f"{name}.png").is_file()]


def build_emotion_faces_bin(src_dir: Path) -> bytes:
    """把 8 张情绪脸拼成 emotion_115.bin 的内容。

    Raises:
        FileNotFoundError: 少了任何一张 E1 情绪的 PNG。
        ValueError: 图片打不开 / 不是有效位图。
    """
    missing = missing_emotions(src_dir)
    if missing:
        raise FileNotFoundError(
            "缺少情绪脸 PNG: " + ", ".join(missing) + f"（目录 {src_dir}）"
        )

    chunks: list[bytes] = []
    for name in EMOTIONS:
        try:
            face = load_emotion_face(src_dir / f"{name}.png")
        except Exception as exc:  # Pillow 的异常类型随版本变，统一包一层
            raise ValueError(f"{name}.png 读不出来: {exc}") from exc
        if face.size != (FACE_SIZE, FACE_SIZE):
            raise ValueError(f"{name}.png 尺寸异常: {face.size}")
        chunks.append(face.tobytes())

    blob = b"".join(chunks)
    if len(blob) != BIN_BYTES:  # 理论上到不了，守住资源契约
        raise ValueError(f"生成长度 {len(blob)} != {BIN_BYTES}")
    return blob


def render_preview(blob: bytes, out_path: Path) -> Path:
    """把 bin 拼成一行预览图，方便肉眼确认顺序和内容。"""
    if len(blob) != BIN_BYTES:
        raise ValueError(f"预览只接受 {BIN_BYTES} 字节的 bin，收到 {len(blob)}")
    sheet = Image.new("L", (FACE_SIZE * len(EMOTIONS), FACE_SIZE), 255)
    for index in range(len(EMOTIONS)):
        offset = index * FACE_BYTES
        face = Image.frombytes(
            "L", (FACE_SIZE, FACE_SIZE), blob[offset : offset + FACE_BYTES]
        )
        sheet.paste(face, (index * FACE_SIZE, 0))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 8 张 E1 情绪脸 PNG 打成 emotion_115.bin",
    )
    parser.add_argument("src_dir", type=Path, help="放着 8 张 <emotion>.png 的目录")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"输出 bin 路径（默认 {DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        default=None,
        help="额外把 bin 拼成一行灰度预览图写到这个路径",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    src_dir: Path = args.src_dir
    if not src_dir.is_dir():
        print(f"[error] 不是目录: {src_dir}", file=sys.stderr)
        return 2

    try:
        blob = build_emotion_faces_bin(src_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(blob)
    print(f"[ok] {args.output}  ({len(blob)} bytes = {len(EMOTIONS)} x {FACE_SIZE}x{FACE_SIZE})")

    if args.preview is not None:
        try:
            path = render_preview(blob, args.preview)
        except ValueError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
        print(f"[ok] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
