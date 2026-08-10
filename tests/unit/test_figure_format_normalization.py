"""Image bytes must match the file extension.

2026-08-10: an entire run aborted with a non-retryable 400 from Anthropic —
"The image was specified using the image/png media type, but the image
appears to be a image/jpeg image". Image models return whatever format they
like and the file was saved as .png regardless; LaTeX sniffs content so the
paper compiled, which let the mismatch survive all the way to the reviewer's
vision call. Our own shipped test fixture had the same defect.
"""

from pathlib import Path

import pytest

pytest.importorskip("PIL")
from PIL import Image

from ark.figure_manifest import normalize_image_formats, _actual_format


def _write(path: Path, fmt: str, size=(40, 30)):
    img = Image.new("RGB", size, (120, 90, 200))
    img.save(path, fmt)


def test_jpeg_bytes_named_png_are_re_encoded(tmp_path):
    f = tmp_path / "fig_concept.png"
    _write(f, "JPEG")
    assert _actual_format(f) == ".jpg"          # the lie

    fixed = normalize_image_formats(tmp_path)

    assert fixed == ["fig_concept.png"]
    assert _actual_format(f) == ".png"          # bytes now match the name
    assert Image.open(f).size == (40, 30)       # and it is still the picture


def test_filename_is_preserved(tmp_path):
    """Renaming would break main.tex / the manifest / concept_figures.json."""
    f = tmp_path / "fig_pipeline.png"
    _write(f, "JPEG")
    normalize_image_formats(tmp_path)
    assert f.exists()
    assert not list(tmp_path.glob("*.jpg"))


def test_correct_files_are_left_alone(tmp_path):
    png, jpg = tmp_path / "a.png", tmp_path / "b.jpg"
    _write(png, "PNG")
    _write(jpg, "JPEG")
    before = (png.read_bytes(), jpg.read_bytes())
    assert normalize_image_formats(tmp_path) == []
    assert (png.read_bytes(), jpg.read_bytes()) == before


def test_jpg_and_jpeg_are_the_same_format(tmp_path):
    f = tmp_path / "c.jpeg"
    _write(f, "JPEG")
    assert normalize_image_formats(tmp_path) == []


def test_rgba_png_named_jpg_converts_without_crashing(tmp_path):
    f = tmp_path / "d.jpg"
    Image.new("RGBA", (10, 10), (1, 2, 3, 4)).save(f, "PNG")
    assert normalize_image_formats(tmp_path) == ["d.jpg"]
    assert _actual_format(f) == ".jpg"


def test_non_images_and_missing_dir_are_ignored(tmp_path):
    (tmp_path / "manifest.json").write_text("{}")
    assert normalize_image_formats(tmp_path) == []
    assert normalize_image_formats(tmp_path / "nope") == []


def test_shipped_fixture_is_honest():
    """The fixture we ship must not carry the defect it once had."""
    figs = Path(__file__).resolve().parents[2] / "test_fixtures" / "cheap_run" / "figures"
    for img in figs.glob("*.png"):
        assert _actual_format(img) == ".png", f"{img.name} is not really a PNG"
