#!/usr/bin/env python3
"""Convert the square dessert PNGs into small grayscale SPIFFS assets."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter


ITEMS = (
    "pudding",
    "dorayaki",
    "ice_cream",
    "matcha_parfait",
    "strawberry_cake",
    "strawberry_tart",
    "chocolate_sundae",
    "celebration_mille_crepe",
)
FILE_NAMES = {"celebration_mille_crepe": "celebration_mille"}
SIZE = 120


def convert(source: Path, target: Path) -> None:
    image = Image.open(source).convert("L")
    image = image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    image = ImageEnhance.Contrast(image).enhance(1.08).filter(ImageFilter.SHARPEN)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        stream.write(struct.pack("<II", SIZE, SIZE))
        stream.write(image.tobytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    missing = [name for name in ITEMS if not (args.source / f"{name}.png").is_file()]
    if missing:
        parser.error("missing dessert PNGs: " + ", ".join(missing))
    for name in ITEMS:
        filename = FILE_NAMES.get(name, name)
        target = args.output / "desserts" / f"{filename}.bin"
        convert(args.source / f"{name}.png", target)
        print(f"{target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
