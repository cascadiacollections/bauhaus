"""Tests for progressive JPEG encoding.

Tests the progressive JPEG encoding behavior that embed_exif() uses.
Kept separate from test_main.py to avoid the torch dependency for quick local runs.
"""

from io import BytesIO

from PIL import Image


# --- helpers ---

def _make_image(size=(64, 64)):
    return Image.new("RGB", size, (128, 100, 80))


def _encode_jpeg(image, progressive=False):
    """Encode a PIL image as JPEG with optional progressive flag (mirrors embed_exif logic)."""
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95, progressive=progressive)
    return buf.getvalue()


class TestProgressiveEncoding:
    """Tests for progressive JPEG encoding."""

    def test_baseline_produces_valid_jpeg(self):
        data = _encode_jpeg(_make_image(), progressive=False)
        assert data[:2] == b"\xff\xd8"  # JPEG SOI marker

    def test_progressive_produces_valid_jpeg(self):
        data = _encode_jpeg(_make_image(), progressive=True)
        assert data[:2] == b"\xff\xd8"  # JPEG SOI marker

    def test_progressive_default_is_false(self):
        """Default call (no progressive arg) should match progressive=False."""
        img = _make_image()
        default_bytes = _encode_jpeg(img)
        baseline_bytes = _encode_jpeg(img, progressive=False)
        assert default_bytes == baseline_bytes

    def test_progressive_differs_from_baseline(self):
        """Progressive encoding should produce different bytes than baseline."""
        img = _make_image(size=(256, 256))
        baseline = _encode_jpeg(img, progressive=False)
        progressive = _encode_jpeg(img, progressive=True)
        assert baseline != progressive

    def test_progressive_contains_multiple_sos_markers(self):
        """Progressive JPEG should contain multiple SOS (Start of Scan) markers."""
        img = _make_image(size=(256, 256))
        data = _encode_jpeg(img, progressive=True)
        # SOS marker is 0xFF 0xDA; progressive JPEGs have multiple scans
        sos_count = data.count(b"\xff\xda")
        assert sos_count > 1, f"Expected multiple SOS markers for progressive, got {sos_count}"

    def test_baseline_contains_single_sos_marker(self):
        """Baseline JPEG should contain exactly one SOS marker."""
        img = _make_image(size=(256, 256))
        data = _encode_jpeg(img, progressive=False)
        sos_count = data.count(b"\xff\xda")
        assert sos_count == 1, f"Expected 1 SOS marker for baseline, got {sos_count}"

    def test_progressive_roundtrips_through_pil(self):
        """Progressive JPEG should be readable by PIL."""
        data = _encode_jpeg(_make_image(size=(128, 128)), progressive=True)
        img = Image.open(BytesIO(data))
        assert img.mode == "RGB"
        assert img.size == (128, 128)
