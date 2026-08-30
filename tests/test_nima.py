"""Tests for nima.py — NIMA aesthetic scoring on a MobileNet-v1 backbone.

Two tiers. The architecture tests run everywhere on a randomly-initialised
network: they check the shapes, the TensorFlow-compatible padding, and the
preprocessing contract, none of which need trained weights. The scoring tests
need the converted weights and skip when `just download-models` has not run —
CI's test job deliberately does not fetch the 13 MB archive.
"""

from pathlib import Path

import pytest
import torch
from PIL import Image, ImageFilter

from nima import (
    INPUT_SIZE,
    WEIGHTS_PATH,
    NimaMobileNet,
    _load_model,
    preprocess,
    score,
)

_STYLES = Path(__file__).resolve().parent.parent / "styles"

requires_weights = pytest.mark.skipif(
    not WEIGHTS_PATH.exists(),
    reason="converted NIMA weights absent — run `just download-models`",
)


def _sample_photo() -> Image.Image:
    """A real painting from the shipped style references.

    Synthetic test patterns are outside anything NIMA saw in training, so its
    scores on them carry no signal. The style references are actual artwork at
    real resolution, which is what the model is being asked to judge in
    production.
    """
    return Image.open(_STYLES / "monet-water-lilies.jpg").convert("RGB")


class TestArchitecture:
    def test_forward_returns_a_ten_bin_distribution(self):
        model = NimaMobileNet().eval()
        with torch.no_grad():
            out = model(torch.zeros(1, 3, INPUT_SIZE, INPUT_SIZE))

        assert out.shape == (1, 10)
        assert out.min() >= 0.0
        assert out.sum().item() == pytest.approx(1.0, abs=1e-5)

    def test_stride_two_padding_is_bottom_right_only(self):
        """TensorFlow's 'same' padding is asymmetric for stride 2.

        PyTorch's `padding=1` would pad all four sides, shifting the feature map
        by a pixel at every downsampling layer — a mismatch that still produces
        plausible-looking scores, so only an explicit check catches it.
        """
        x = torch.ones(1, 1, 2, 2)
        padded = NimaMobileNet._pad_same(x, stride=2)

        assert padded.shape == (1, 1, 3, 3)
        assert padded[0, 0, 0, 0] == 1.0  # original content stays top-left
        assert padded[0, 0, 2, 2] == 0.0  # padding lands bottom-right

    def test_stride_one_padding_is_symmetric(self):
        padded = NimaMobileNet._pad_same(torch.ones(1, 1, 2, 2), stride=1)

        assert padded.shape == (1, 1, 4, 4)
        assert padded[0, 0, 0, 0] == 0.0
        assert padded[0, 0, 3, 3] == 0.0

    def test_downsampling_reaches_the_expected_feature_map(self):
        """224 → 7×7 across five stride-2 stages, at 1024 channels.

        A stride placed on the wrong block still yields a 10-bin output, but
        against a feature map the trained weights were never fitted to.
        """
        model = NimaMobileNet().eval()
        captured = {}
        model.conv_pw_13.register_forward_hook(
            lambda _module, _inputs, output: captured.update(shape=output.shape)
        )

        with torch.no_grad():
            model(torch.zeros(1, 3, INPUT_SIZE, INPUT_SIZE))

        assert captured["shape"] == (1, 1024, 7, 7)


class TestPreprocess:
    def test_shape_and_layout(self):
        tensor = preprocess(Image.new("RGB", (800, 600), (128, 128, 128)))

        assert tensor.shape == (1, 3, INPUT_SIZE, INPUT_SIZE)

    def test_scales_to_minus_one_to_one(self):
        white = preprocess(Image.new("RGB", (32, 32), (255, 255, 255)))
        black = preprocess(Image.new("RGB", (32, 32), (0, 0, 0)))

        assert white.min().item() == pytest.approx(1.0)
        assert black.max().item() == pytest.approx(-1.0)

    def test_grayscale_input_is_converted(self):
        tensor = preprocess(Image.new("L", (64, 64), 200))

        assert tensor.shape == (1, 3, INPUT_SIZE, INPUT_SIZE)


class TestMissingWeights:
    def test_load_raises_an_actionable_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="download-models"):
            _load_model(tmp_path / "not-there.npz")


@requires_weights
class TestScoring:
    def test_score_is_on_the_ava_scale(self):
        result = score(_sample_photo())

        assert 1.0 <= result["mean"] <= 10.0
        assert result["std"] > 0.0

    def test_blurring_an_image_lowers_its_score(self):
        """The clearest signal that the converted weights actually work.

        A network with mismapped weights still emits a well-formed
        distribution; one that ranks a degraded image below its original is
        responding to image content.
        """
        original = _sample_photo()
        blurred = original.filter(ImageFilter.GaussianBlur(12))

        assert score(blurred)["mean"] < score(original)["mean"]

    def test_scoring_is_deterministic(self):
        image = _sample_photo()

        assert score(image) == score(image)

    def test_converted_weights_cover_every_parameter(self):
        """Catches a rename on either side of the conversion.

        `_load_model` loads with strict=True, so a missing or surplus tensor
        raises here rather than silently leaving part of the network at its
        random initialisation.
        """
        model = _load_model(WEIGHTS_PATH)

        assert set(model.state_dict()) == set(NimaMobileNet().state_dict())
