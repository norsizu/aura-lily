"""tools/build_emotion_faces.py：情绪脸资源打包脚本。

用 Pillow 现场造假 PNG，验证 bin 长度契约（105800 字节）和缺图报错；
再走一遍 CLI，确认退出码和 --preview。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_emotion_faces import (  # noqa: E402
    BIN_BYTES,
    EMOTIONS,
    FACE_SIZE,
    build_emotion_faces_bin,
    main,
    missing_emotions,
    render_preview,
)


def _write_fake_faces(src_dir: Path, size: tuple[int, int] = (200, 140)) -> None:
    """造 8 张假 PNG：非正方形、灰度单调递增，顺带覆盖居中裁切。"""
    src_dir.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(EMOTIONS):
        shade = (index * 255) // (len(EMOTIONS) - 1)
        Image.new("L", size, shade).save(src_dir / f"{name}.png")


def test_bin_is_105800_bytes_in_e1_order(tmp_path: Path):
    _write_fake_faces(tmp_path)
    blob = build_emotion_faces_bin(tmp_path)

    assert len(blob) == 105800
    assert BIN_BYTES == 105800
    assert len(EMOTIONS) == 8

    # 顺序即 E1：第 i 块 115×115 的均值单调递增（假图就是这么造的）
    block = FACE_SIZE * FACE_SIZE
    means = []
    for index in range(len(EMOTIONS)):
        chunk = blob[index * block : (index + 1) * block]
        means.append(sum(chunk) / len(chunk))
    assert means == sorted(means)


def test_non_square_input_is_center_cropped_to_115(tmp_path: Path):
    _write_fake_faces(tmp_path, size=(240, 120))
    blob = build_emotion_faces_bin(tmp_path)
    assert len(blob) == BIN_BYTES

    # 240×120 居中裁成 120×120 再缩到 115×115：整块是同一个灰度值
    from tools.build_emotion_faces import load_emotion_face

    face = load_emotion_face(tmp_path / "happy.png")
    assert face.size == (115, 115)
    assert face.mode == "L"
    assert len(set(face.tobytes())) == 1


def test_missing_face_raises_and_lists_every_gap(tmp_path: Path):
    _write_fake_faces(tmp_path)
    (tmp_path / "sad.png").unlink()
    (tmp_path / "surprised.png").unlink()

    assert missing_emotions(tmp_path) == ["sad", "surprised"]
    with pytest.raises(FileNotFoundError) as excinfo:
        build_emotion_faces_bin(tmp_path)
    message = str(excinfo.value)
    assert "sad" in message and "surprised" in message


def test_cli_writes_bin_and_preview_then_exits_zero(tmp_path: Path):
    src = tmp_path / "faces"
    out = tmp_path / "emotion_115.bin"
    preview = tmp_path / "preview.png"
    _write_fake_faces(src)

    assert main([str(src), "-o", str(out), "--preview", str(preview)]) == 0
    assert out.stat().st_size == 105800
    with Image.open(preview) as sheet:
        assert sheet.size == (115 * len(EMOTIONS), 115)


def test_cli_exits_nonzero_when_a_face_is_missing(tmp_path: Path, capsys):
    src = tmp_path / "faces"
    out = tmp_path / "emotion_115.bin"
    _write_fake_faces(src)
    (src / "pouty.png").unlink()

    assert main([str(src), "-o", str(out)]) == 2
    assert not out.exists()
    assert "pouty" in capsys.readouterr().err


def test_cli_rejects_a_missing_source_directory(tmp_path: Path, capsys):
    assert main([str(tmp_path / "nope")]) == 2
    assert "不是目录" in capsys.readouterr().err


def test_script_runs_as_a_subprocess(tmp_path: Path):
    src = tmp_path / "faces"
    out = tmp_path / "emotion_115.bin"
    _write_fake_faces(src)

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_emotion_faces.py"), str(src), "-o", str(out)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert out.stat().st_size == 105800


def test_preview_rejects_a_wrong_length_blob(tmp_path: Path):
    with pytest.raises(ValueError):
        render_preview(b"\x00" * 10, tmp_path / "preview.png")
