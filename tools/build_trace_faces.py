#!/usr/bin/env python3
"""把 8 张情绪脸 PNG 打成 X-Ray 大屏用的内联图片模块。

lily-BuilderHub/art/emotion/<emotion>.png → integrations/hermes_lily_cli/trace_faces.py
（base64 PNG，大屏页面不引用任何外部资源）。只用 Pillow。
"""
from __future__ import annotations

import argparse
import base64
import io
from pathlib import Path

from PIL import Image

EMOTIONS = ("neutral", "happy", "excited", "shy", "sad", "worried", "pouty", "surprised")
REPO = Path(__file__).resolve().parents[1]


def encode_face(path: Path, size: int) -> str:
    image = Image.open(path).convert("L")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    # 原图是黑白线稿，压到 4 级灰度基本无损，体积小一个量级。
    image = image.quantize(colors=4)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=REPO / "lily-BuilderHub/art/emotion")
    parser.add_argument("--out", type=Path, default=REPO / "integrations/hermes_lily_cli/trace_faces.py")
    parser.add_argument("--size", type=int, default=280)
    args = parser.parse_args()
    missing = [name for name in EMOTIONS if not (args.src / f"{name}.png").exists()]
    if missing:
        parser.error("缺少情绪脸：" + ", ".join(missing))
    lines = [
        '"""X-Ray 大屏的情绪脸（由 tools/build_trace_faces.py 生成，不要手改）。"""',
        "",
        "TRACE_FACES = {",
    ]
    for name in EMOTIONS:
        lines.append(f'    "{name}": "data:image/png;base64,{encode_face(args.src / f"{name}.png", args.size)}",')
    lines.append("}")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[ok] {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
