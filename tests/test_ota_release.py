import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "esp32"


def test_ota_partition_layout_is_dual_slot_and_within_16mb():
    rows = []
    for line in (FIRMWARE / "partitions.csv").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        rows.append((fields[0], fields[1], fields[2], int(fields[3], 0), int(fields[4], 0)))

    by_name = {row[0]: row for row in rows}
    assert by_name["ota_0"][2:] == ("ota_0", 0x020000, 0x280000)
    assert by_name["ota_1"][2:] == ("ota_1", 0x2A0000, 0x280000)
    assert by_name["assets"][3:] == (0x720000, 0x5F0000)
    assert by_name["storage"][3:] == (0xD10000, 0x2F0000)

    ordered = sorted(rows, key=lambda row: row[3])
    for previous, current in zip(ordered, ordered[1:]):
        assert previous[3] + previous[4] <= current[3]
    assert max(offset + size for _, _, _, offset, size in rows) == 0x1000000


def test_release_tool_emits_hashed_https_manifest(tmp_path):
    build = tmp_path / "build"
    assets = tmp_path / "assets"
    output = tmp_path / "release"
    build.mkdir()
    (assets / "scenes").mkdir(parents=True)
    app_bytes = b"test application image"
    resource_bytes = b"test park resource"
    (build / "aura_doudou.bin").write_bytes(app_bytes)
    (assets / "scenes" / "outside_park.bin").write_bytes(resource_bytes)

    subprocess.run(
        [
            sys.executable,
            str(FIRMWARE / "tools" / "make_ota_release.py"),
            "--version",
            "0.16",
            "--assets-version",
            "0.16.0",
            "--base-url",
            "https://updates.example.test/aura/stable",
            "--build-dir",
            str(build),
            "--assets-dir",
            str(assets),
            "--asset",
            "scenes/outside_park.bin",
            "--output",
            str(output),
        ],
        check=True,
    )

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1
    assert manifest["app"]["version"] == "0.16"
    assert manifest["app"]["url"] == "https://updates.example.test/aura/stable/aura-0.16.bin"
    assert manifest["app"]["size"] == len(app_bytes)
    assert manifest["app"]["sha256"] == hashlib.sha256(app_bytes).hexdigest()
    assert manifest["resources"]["version"] == "0.16.0"
    assert manifest["resources"]["files"] == [
        {
            "path": "scenes/outside_park.bin",
            "url": "https://updates.example.test/aura/stable/resources/0.16.0/scenes/outside_park.bin",
            "sha256": hashlib.sha256(resource_bytes).hexdigest(),
            "size": len(resource_bytes),
        }
    ]


def test_ota_source_enables_rollback_and_https_manifest():
    defaults = (FIRMWARE / "sdkconfig.defaults").read_text(encoding="utf-8")
    config = (FIRMWARE / "main" / "aura_config.h").read_text(encoding="utf-8")
    source = (FIRMWARE / "main" / "network" / "ota_update.c").read_text(encoding="utf-8")
    assert "CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y" in defaults
    kconfig = (FIRMWARE / "main" / "Kconfig.projbuild").read_text(encoding="utf-8")
    assert 'CONFIG_AURA_OTA_MANIFEST_URL=""' in defaults
    assert '#define AURA_OTA_MANIFEST_URL CONFIG_AURA_OTA_MANIFEST_URL' in config
    assert 'config AURA_OTA_MANIFEST_URL' in kconfig
    assert "esp_ota_get_next_update_partition" in source
    assert "esp_ota_mark_app_valid_cancel_rollback" in source
    assert "sha256_matches" in source
    assert "xTaskCreateWithCaps" in source
    assert "MALLOC_CAP_INTERNAL" in source
    assert "vTaskDeleteWithCaps" in source
    assert "OTA_HTTP_BUFFER_BYTES   2048" in source
    assert "content_length < 0" in source


def test_firmware_exposes_eleven_world_scenes_and_professional_outfit():
    messages = (FIRMWARE / "main" / "protocol" / "messages.c").read_text(encoding="utf-8")
    renderer = (FIRMWARE / "main" / "display" / "renderer.c").read_text(encoding="utf-8")
    main = (FIRMWARE / "main" / "main.c").read_text(encoding="utf-8")
    websocket = (FIRMWARE / "main" / "network" / "ws_client.c").read_text(encoding="utf-8")
    assert '"outside.park", "outside.mall", "outside.riverside"' in messages
    assert '"/scenes/street.bin"' in renderer
    assert '"/scenes/outside_neighborhood.bin"' not in renderer
    assert '"/scenes/outside_park.bin"' in renderer
    assert '"/scenes/outside_mall.bin"' in renderer
    assert '"/scenes/outside_riverside.bin"' in renderer
    assert '"/outfits/professional.bin"' in renderer
    assert '"/outfits/casual_b.bin"' not in renderer
    assert "scene > 7" not in renderer
    assert 'cJSON_GetObjectItem(payload, "outfit_mode")' in websocket
    assert "AUTO_OUTFIT_NIGHT_START" not in main

    scene_dir = FIRMWARE / "assets" / "scenes"
    outfit_dir = FIRMWARE / "assets" / "outfits"
    registered_scenes = [
        "home_living_room", "home_study", "home_bedroom", "home_kitchen",
        "home_balcony", "street", "outside_cafe", "outside_shop",
        "outside_park", "outside_mall", "outside_riverside",
    ]
    outfit_names = [
        "pajama", "dress", "nightdress", "casual_a", "professional",
        "winter", "qipao", "mamian", "hanfu",
    ]
    assert all((scene_dir / f"{name}.bin").stat().st_size == 120008 for name in registered_scenes)
    assert all((outfit_dir / f"{name}.bin").stat().st_size == 135008 for name in outfit_names)
    assert not (outfit_dir / "casual_b.bin").exists()


def test_outfit_atlas_cells_are_visible_and_bayer_anchor_contract_is_preserved():
    renderer = (FIRMWARE / "main" / "display" / "renderer.c").read_text(encoding="utf-8")
    converter = (FIRMWARE / "tools" / "convert_assets.py").read_text(encoding="utf-8")
    sheet_converter = (FIRMWARE / "tools" / "convert_outfit_sheet.py").read_text(encoding="utf-8")

    # Resource conversion must use the content-mask/shared-crop path for RGB
    # 1-bit sheets; ordinary alpha-only or threshold-only extraction regresses
    # the generated source sheets.
    assert "from convert_outfit_sheet import convert_sheet as convert_outfit_sheet" in converter
    assert "convert_outfit_sheet(source, target)" in converter
    assert "src_crop = (" in sheet_converter
    assert "py = POSE_H - fitted.height - 2" in sheet_converter

    # The calibrated default mode and Bayer table remain in the firmware; the
    # 25px character anchor is part of the established device layout.
    assert "static render_dither_mode_t s_dither_mode = DITHER_HYBRID_INK" in renderer
    assert "static const uint8_t s_bayer8[8][8]" in renderer
    assert "case DITHER_BAYER8:" in renderer
    assert "static int s_char_y_offset = 25" in renderer

    outfit_names = [
        "pajama", "dress", "nightdress", "casual_a", "professional",
        "winter", "qipao", "mamian", "hanfu",
    ]
    for name in outfit_names:
        data = (FIRMWARE / "assets" / "outfits" / f"{name}.bin").read_bytes()
        width, height = struct.unpack("<II", data[:8])
        assert (width, height) == (600, 900)
        packed = data[8:]
        pixels = []
        for value in packed:
            pixels.extend(((value >> 6) & 3, (value >> 4) & 3, (value >> 2) & 3, value & 3))
        pixels = pixels[: width * height]
        for pose in range(9):
            row, col = divmod(pose, 3)
            visible = sum(
                pixels[(row * 300 + y) * width + col * 200 + x] != 0
                for y in range(300)
                for x in range(200)
            )
            assert visible > 0, f"{name} pose {pose} is empty"
