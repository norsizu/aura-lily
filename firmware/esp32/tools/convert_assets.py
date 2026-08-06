#!/usr/bin/env python3
"""
将原始素材转换为 ESP32 ST7305 墨水屏用的 .bin 格式

场景背景: uint32_t width(400) + uint32_t height(300) + raw grayscale
人物图集: uint32_t width(540) + uint32_t height(720) + raw grayscale (0xFF=透明, 3x3 grid)

固定资源规格:
- 背景: aura_prototype_v8/bg 2.jpg (800x600，按 2x 精准缩小到 400x300)
- 默认服装: aura_prototype_v8/人物.png (1200x1800，3x3，每格 400x600)
- 夜间服装: aura_prototype_v8/人物_睡衣.png (可选；若缺失则复用默认服装)

用法:
  python3 tools/convert_assets.py
"""
import argparse
import struct
from pathlib import Path
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

SRC_DIR = Path(__file__).parent.parent.parent / "aura_prototype_v8"

OUT_DIR = Path(__file__).parent.parent / "assets"

# 目标尺寸
SCREEN_W, SCREEN_H = 400, 300
ATLAS_COLS, ATLAS_ROWS = 3, 3
POSE_W, POSE_H = 200, 300  # 每个 pose cell；对应 400x600 源图精确 2x 缩小
ATLAS_W = POSE_W * ATLAS_COLS  # 540
ATLAS_H = POSE_H * ATLAS_ROWS  # 720
SPRITE_COLS, SPRITE_ROWS = 8, 7
MINI_SPRITE_W, MINI_SPRITE_H = 36, 48
SRC_BG_W, SRC_BG_H = 800, 600
SRC_POSE_W, SRC_POSE_H = 400, 600
SRC_ATLAS_W = SRC_POSE_W * ATLAS_COLS
SRC_ATLAS_H = SRC_POSE_H * ATLAS_ROWS

# 粉色背景色 (sprite 的透明色)
PINK_THRESHOLD = 80  # 距离粉色 (245,11,244) 的容差


def is_pink(r, g, b, threshold=PINK_THRESHOLD):
    """判断像素是否是粉色背景"""
    return abs(r - 245) < threshold and g < 80 and abs(b - 244) < threshold


def resize_background(img: Image.Image) -> Image.Image:
    """背景图缩放到屏幕尺寸，4:3 素材优先用 BOX 保持线稿稳定。"""
    if img.size == (SCREEN_W * 2, SCREEN_H * 2):
        return img.resize((SCREEN_W, SCREEN_H), Image.BOX)
    return ImageOps.fit(img, (SCREEN_W, SCREEN_H), method=Image.LANCZOS)


def convert_background(src_path: Path, out_path: Path):
    """将背景图转为 400x300 灰度 .bin"""
    print(f"  背景: {src_path.name} → {out_path.name}")
    img = Image.open(src_path).convert("L")
    img = resize_background(img)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(struct.pack("<II", SCREEN_W, SCREEN_H))
        f.write(img.tobytes())

    size = out_path.stat().st_size
    print(f"    → {size} bytes (expected {SCREEN_W * SCREEN_H + 8})")


def convert_outfit_atlas(src_path: Path, out_path: Path):
    """
    将九宫格 pose sheet 转为 3x3 灰度 atlas .bin
    粉色背景 → 0xFF (透明)
    """
    print(f"  服装: {src_path.name} → {out_path.name}")
    img = Image.open(src_path).convert("RGBA")
    src_w, src_h = img.size

    if (src_w, src_h) != (SRC_ATLAS_W, SRC_ATLAS_H):
        print(f"    ⚠ 源图尺寸不是推荐规格 {SRC_ATLAS_W}x{SRC_ATLAS_H}，当前为 {src_w}x{src_h}")

    # 九宫格: 新规范固定每格 400x600，但仍保留容错读取
    cell_w = src_w // ATLAS_COLS
    cell_h = src_h // ATLAS_ROWS
    print(f"    源图 {src_w}x{src_h}, 每格 {cell_w}x{cell_h}")

    # 创建输出灰度图集
    atlas = bytearray(ATLAS_W * ATLAS_H)
    # 全部填充 0xFF (透明)
    for i in range(len(atlas)):
        atlas[i] = 0xFF

    for row in range(ATLAS_ROWS):
        for col in range(ATLAS_COLS):
            # 从源图裁切
            x0 = col * cell_w
            y0 = row * cell_h
            cell = img.crop((x0, y0, x0 + cell_w, y0 + cell_h))
            # 新规范下 400x600 -> 200x300 为精准 2x 缩小，优先保线条稳定
            if cell.size == (SRC_POSE_W, SRC_POSE_H) and (POSE_W, POSE_H) == (SRC_POSE_W // 2, SRC_POSE_H // 2):
                fitted = cell.resize((POSE_W, POSE_H), Image.BOX)
                fitted = fitted.filter(ImageFilter.SHARPEN)
                fitted = ImageEnhance.Contrast(fitted).enhance(1.08)
            else:
                fitted = ImageOps.contain(cell, (POSE_W, POSE_H), method=Image.LANCZOS)
            canvas = Image.new("RGBA", (POSE_W, POSE_H), (0, 0, 0, 0))
            paste_x = (POSE_W - fitted.width) // 2
            paste_y = (POSE_H - fitted.height) // 2
            canvas.alpha_composite(fitted, (paste_x, paste_y))

            # 转灰度，alpha < 128 的像素设为透明 (0xFF)
            cell_gray = canvas.convert("L")

            for y in range(POSE_H):
                for x in range(POSE_W):
                    r, g, b, a = canvas.getpixel((x, y))
                    if a < 128 or is_pink(r, g, b):
                        gray_val = 0xFF  # 透明
                    else:
                        gray_val = cell_gray.getpixel((x, y))
                        if gray_val == 0xFF:
                            gray_val = 0xFE

                    dst_x = col * POSE_W + x
                    dst_y = row * POSE_H + y
                    atlas[dst_y * ATLAS_W + dst_x] = gray_val

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(struct.pack("<II", ATLAS_W, ATLAS_H))
        f.write(bytes(atlas))

    size = out_path.stat().st_size
    print(f"    → {size} bytes (expected {ATLAS_W * ATLAS_H + 8})")

    # 统计透明像素
    transparent = sum(1 for b in atlas if b == 0xFF)
    total = ATLAS_W * ATLAS_H
    print(f"    透明像素: {transparent}/{total} ({transparent*100//total}%)")


def convert_mini_sprite_row(src_path: Path, row_idx: int, out_path: Path):
    """从 sprite atlas 提取单行动画，裁切透明边后缩成 3:4 小窗资源。"""
    print(f"  Sprite: row {row_idx} → {out_path.name}")
    img = Image.open(src_path).convert("RGBA")
    frame_w = img.width // SPRITE_COLS
    frame_h = img.height // SPRITE_ROWS
    frames = []

    for col in range(SPRITE_COLS):
        frame = img.crop((col * frame_w, row_idx * frame_h,
                          (col + 1) * frame_w, (row_idx + 1) * frame_h))
        mask = Image.new("L", frame.size, 0)
        frame_px = frame.load()
        mask_px = mask.load()

        for y in range(frame.height):
            for x in range(frame.width):
                r, g, b, a = frame_px[x, y]
                if a >= 128 and not is_pink(r, g, b):
                    mask_px[x, y] = 255

        bbox = mask.getbbox()
        cropped = frame.crop(bbox) if bbox else frame
        fitted = ImageOps.contain(cropped, (MINI_SPRITE_W, MINI_SPRITE_H), method=Image.NEAREST)
        canvas = Image.new("RGBA", (MINI_SPRITE_W, MINI_SPRITE_H), (0, 0, 0, 0))
        paste_x = (MINI_SPRITE_W - fitted.width) // 2
        paste_y = (MINI_SPRITE_H - fitted.height) // 2
        canvas.alpha_composite(fitted, (paste_x, paste_y))
        cell_gray = canvas.convert("L")

        frame_buf = bytearray(MINI_SPRITE_W * MINI_SPRITE_H)
        for y in range(MINI_SPRITE_H):
            for x in range(MINI_SPRITE_W):
                r, g, b, a = canvas.getpixel((x, y))
                if a < 128 or is_pink(r, g, b):
                    gray_val = 0xFF
                else:
                    gray_val = cell_gray.getpixel((x, y))
                    if gray_val == 0xFF:
                        gray_val = 0xFE
                frame_buf[y * MINI_SPRITE_W + x] = gray_val
        frames.append(bytes(frame_buf))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(struct.pack("<III", len(frames), MINI_SPRITE_W, MINI_SPRITE_H))
        for frame in frames:
            f.write(frame)

    print(f"    → {out_path.stat().st_size} bytes")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scene-dir",
        type=Path,
        help="Directory containing 客厅.png ... 商场.png scene sources",
    )
    parser.add_argument(
        "--scenes-only",
        action="store_true",
        help="Convert scene backgrounds without touching outfit or sprite assets",
    )
    args = parser.parse_args()

    src = SRC_DIR
    print(f"源目录: {src}")
    print(f"输出目录: {OUT_DIR}")

    # === 世界模型背景（每张独立 800x600 源图，不再由一张客厅图调色伪造） ===
    if args.scene_dir:
        scene_src_dir = args.scene_dir
        scene_sources = {
            "home_living_room": "客厅.png",
            "home_study": "书房.png",
            "home_bedroom": "卧室.png",
            "home_kitchen": "厨房.png",
            "home_balcony": "阳台.png",
            "street": "街道.png",
            "outside_cafe": "咖啡馆.png",
            "outside_shop": "便利店.png",
            "outside_park": "公园.png",
            "outside_mall": "商场.png",
        }
    else:
        scene_src_dir = src
        scene_sources = {
            "home_living_room": "scene_home_living_room.png",
            "home_study": "scene_home_study.png",
            "home_bedroom": "scene_home_bedroom.png",
            "home_kitchen": "scene_home_kitchen.png",
            "home_balcony": "scene_home_balcony.png",
            "street": "scene_outside_neighborhood.png",
            "outside_cafe": "scene_outside_cafe.png",
            "outside_shop": "scene_outside_shop.png",
            "outside_park": "scene_outside_park.png",
            "outside_mall": "scene_outside_mall.png",
        }
    converted_scenes = 0
    for scene_name, filename in scene_sources.items():
        scene_src = scene_src_dir / filename
        if scene_src.exists():
            convert_background(scene_src, OUT_DIR / "scenes" / f"{scene_name}.bin")
            converted_scenes += 1

    # 旧素材目录兼容；只有尚未提供世界模型背景时才使用。
    bg_src = src / "bg 2.jpg"
    if converted_scenes == 0 and bg_src.exists():
        # 同一张背景生成3个场景（暂时，后面可换不同图）
        # living_room: 原图
        convert_background(bg_src, OUT_DIR / "scenes" / "living_room.bin")

        # bedroom: 稍暗一些
        img_pil = resize_background(Image.open(bg_src).convert("RGB"))
        img_dark = ImageEnhance.Brightness(img_pil).enhance(0.6)
        img_dark_gray = img_dark.convert("L")
        out_path = OUT_DIR / "scenes" / "bedroom.bin"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(struct.pack("<II", SCREEN_W, SCREEN_H))
            f.write(img_dark_gray.tobytes())
        print(f"  背景: bedroom.bin (暗化版) → {out_path.stat().st_size} bytes")

        # study: 稍亮+高对比
        img_bright = ImageEnhance.Contrast(img_pil).enhance(1.3)
        img_bright_gray = img_bright.convert("L")
        out_path = OUT_DIR / "scenes" / "study.bin"
        with open(out_path, "wb") as f:
            f.write(struct.pack("<II", SCREEN_W, SCREEN_H))
            f.write(img_bright_gray.tobytes())
        print(f"  背景: study.bin (高对比版) → {out_path.stat().st_size} bytes")
    elif converted_scenes == 0:
        print(f"  ⚠ 背景源文件不存在: {bg_src}")

    if args.scenes_only:
        if converted_scenes != len(scene_sources):
            raise SystemExit(
                f"Expected {len(scene_sources)} scenes, converted {converted_scenes}"
            )
        print("\n✅ 场景转换完成！")
        return

    # === 人物图集 ===
    default_outfit_src = src / "人物.png"
    if default_outfit_src.exists():
        convert_outfit_atlas(default_outfit_src, OUT_DIR / "outfits" / "school_uniform.bin")
    else:
        print(f"  ⚠ 默认服装源文件不存在: {default_outfit_src}")

    sleepwear_src = src / "人物_睡衣.png"
    if sleepwear_src.exists():
        convert_outfit_atlas(sleepwear_src, OUT_DIR / "outfits" / "sleepwear_basic.bin")
    else:
        if default_outfit_src.exists():
            print("  未提供人物_睡衣.png，sleepwear_basic.bin 将复用默认服装")
            convert_outfit_atlas(default_outfit_src, OUT_DIR / "outfits" / "sleepwear_basic.bin")
        else:
            print("  ⚠ 无法生成 sleepwear_basic.bin：缺少默认服装源图")

    sprite_src = src / "sprite.png"
    if sprite_src.exists():
        convert_mini_sprite_row(sprite_src, 3, OUT_DIR / "sprites" / "work_sprite.bin")
        convert_mini_sprite_row(sprite_src, 6, OUT_DIR / "sprites" / "talk_sprite.bin")
    else:
        print(f"  ⚠ sprite 源文件不存在: {sprite_src}")

    print("\n✅ 完成！")


if __name__ == "__main__":
    main()
