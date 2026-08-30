"""NIMA (Neural Image Assessment) aesthetic scoring on a MobileNet-v1 backbone.

Implements the inference half of Talebi & Milanfar's NIMA (TIP 2018): a CNN
predicts a 10-bin rating distribution over the AVA aesthetic scale, and the
mean of that distribution is the aesthetic score (1–10). The standard deviation
comes free and is a usable confidence signal — a wide distribution means the
model is unsure, not that the image is mediocre.

The weights are the MobileNet-v1 model from titu1994/neural-image-assessment
(MIT, 0.0804 EMD on the AVA validation split), trained in Keras. They are
converted to a NumPy archive by ``models/download_models.py`` so this module
needs nothing beyond torch and numpy — both already project dependencies. See
that script for the conversion and the checksum pin.

NIMA was trained on photographs, not stylized art, so treat the absolute score
as arbitrary units. What it is good for is *relative* ranking across bauhaus's
own outputs and catching the failure modes (blur, mud, washed-out colour) that
the heuristic metrics in ``quality.py`` only partly cover.
"""

from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch import nn

WEIGHTS_PATH = Path(__file__).resolve().parent.parent / "models" / "weights" / "nima_mobilenet.npz"

# NIMA scores images at the backbone's native training resolution.
INPUT_SIZE = 224

# Keras' BatchNormalization default, and what the upstream model was trained
# with. torch.nn.BatchNorm2d defaults to 1e-5, which would shift every
# activation slightly and quietly change the scores.
_BN_EPS = 1e-3

# MobileNet-v1 (alpha=1.0) depthwise-separable stack: (in_channels, out_channels, stride).
_BLOCKS = (
    (32, 64, 1),
    (64, 128, 2),
    (128, 128, 1),
    (128, 256, 2),
    (256, 256, 1),
    (256, 512, 2),
    (512, 512, 1),
    (512, 512, 1),
    (512, 512, 1),
    (512, 512, 1),
    (512, 512, 1),
    (512, 1024, 2),
    (1024, 1024, 1),
)


class NimaMobileNet(nn.Module):
    """MobileNet-v1 feature extractor with NIMA's 10-bin rating head.

    Module names mirror the Keras layer names (``conv1``, ``conv_dw_1``,
    ``conv_pw_1``, …) so the weight conversion is a name-for-name mapping with
    nothing inferred from ordering.
    """

    def __init__(self) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(3, 32, 3, stride=2, padding=0, bias=False)
        self.conv1_bn = nn.BatchNorm2d(32, eps=_BN_EPS)

        for i, (in_ch, out_ch, stride) in enumerate(_BLOCKS, start=1):
            # Depthwise: one 3×3 kernel per input channel (groups == in_ch).
            setattr(self, f"conv_dw_{i}",
                    nn.Conv2d(in_ch, in_ch, 3, stride=stride, padding=0, groups=in_ch, bias=False))
            setattr(self, f"conv_dw_{i}_bn", nn.BatchNorm2d(in_ch, eps=_BN_EPS))
            # Pointwise: 1×1 projection to the block's output width.
            setattr(self, f"conv_pw_{i}", nn.Conv2d(in_ch, out_ch, 1, stride=1, padding=0, bias=False))
            setattr(self, f"conv_pw_{i}_bn", nn.BatchNorm2d(out_ch, eps=_BN_EPS))

        self.dense = nn.Linear(1024, 10)

    @staticmethod
    def _pad_same(x: torch.Tensor, stride: int) -> torch.Tensor:
        """Replicate TensorFlow's ``padding='same'`` for a 3×3 kernel.

        For stride 1 the padding is symmetric (1 on each side) and matches
        PyTorch's ``padding=1``. For stride 2 on an even-sized input TensorFlow
        pads only the bottom and right — asymmetric padding PyTorch cannot
        express as a ``padding=`` argument, so it is applied explicitly. Getting
        this wrong shifts the whole feature map by a pixel at every stride-2
        layer, which is exactly the sort of error that still produces
        plausible-looking scores.
        """
        return nn.functional.pad(x, (0, 1, 0, 1) if stride == 2 else (1, 1, 1, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Return the 10-bin rating distribution (softmax over the AVA scale)."""
        x = nn.functional.relu6(self.conv1_bn(self.conv1(self._pad_same(x, 2))))

        for i, (_, _, stride) in enumerate(_BLOCKS, start=1):
            x = getattr(self, f"conv_dw_{i}")(self._pad_same(x, stride))
            x = nn.functional.relu6(getattr(self, f"conv_dw_{i}_bn")(x))
            x = getattr(self, f"conv_pw_{i}")(x)
            x = nn.functional.relu6(getattr(self, f"conv_pw_{i}_bn")(x))

        x = x.mean(dim=(2, 3))  # global average pooling
        return nn.functional.softmax(self.dense(x), dim=1)


def _load_model(weights_path: Path) -> NimaMobileNet:
    """Build the network and load converted weights, or raise if they are absent."""
    if not weights_path.exists():
        raise FileNotFoundError(
            f"NIMA weights not found at {weights_path}. "
            "Run `just download-models` (uv run --script models/download_models.py)."
        )

    model = NimaMobileNet()
    with np.load(weights_path) as archive:
        state = {k: torch.from_numpy(archive[k]) for k in archive.files if not k.startswith("_")}
    # strict=True: a renamed or missing tensor is a conversion bug, and a
    # partially-initialised network would still emit confident-looking scores.
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


# Keyed by path so a test or caller pointing at different weights gets those
# weights, rather than whichever archive happened to load first.
_MODELS: dict[Path, NimaMobileNet] = {}


def _get_model(weights_path: Path = WEIGHTS_PATH) -> NimaMobileNet:
    """Return the cached model for a weights file, loading it on first use."""
    if weights_path not in _MODELS:
        _MODELS[weights_path] = _load_model(weights_path)
    return _MODELS[weights_path]


def preprocess(image: Image.Image) -> torch.Tensor:
    """Resize to 224×224 and scale to [-1, 1], the backbone's training input."""
    resized = image.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
    arr = np.asarray(resized, dtype=np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)


def score(image: Image.Image, weights_path: Path = WEIGHTS_PATH) -> dict:
    """Score an image on the AVA aesthetic scale.

    Returns ``{"mean": float, "std": float}`` where ``mean`` is the 1–10
    aesthetic score and ``std`` is the spread of the predicted distribution.
    """
    model = _get_model(weights_path)
    with torch.no_grad():
        probs = model(preprocess(image))[0]

    bins = torch.arange(1.0, 11.0)
    mean = float((probs * bins).sum())
    variance = float((probs * (bins - mean) ** 2).sum())

    return {"mean": round(mean, 3), "std": round(variance ** 0.5, 3)}
