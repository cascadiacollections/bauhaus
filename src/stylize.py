"""AdaIN style transfer using VGG-19 encoder and learned decoder.

Based on naoto0804/pytorch-AdaIN (MIT license).
Paper: "Arbitrary Style Transfer in Real-time with Adaptive Instance Normalization"
       Xun Huang, Serge Belongie (ICCV 2017)
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image

MODELS_DIR = Path(__file__).resolve().parent.parent / "models" / "weights"

# Full VGG-19 architecture (normalized version from pytorch-AdaIN).
# The weights file contains all layers; we load them all then truncate to relu4_1.
_VGG_FULL = [
    nn.Conv2d(3, 3, 1),                                                  # 0
    nn.ReflectionPad2d(1), nn.Conv2d(3, 64, 3), nn.ReLU(),               # 1-3: relu1_1
    nn.ReflectionPad2d(1), nn.Conv2d(64, 64, 3), nn.ReLU(),              # 4-6: relu1_2
    nn.MaxPool2d(2, 2, ceil_mode=True),                                   # 7
    nn.ReflectionPad2d(1), nn.Conv2d(64, 128, 3), nn.ReLU(),             # 8-10: relu2_1
    nn.ReflectionPad2d(1), nn.Conv2d(128, 128, 3), nn.ReLU(),            # 11-13: relu2_2
    nn.MaxPool2d(2, 2, ceil_mode=True),                                   # 14
    nn.ReflectionPad2d(1), nn.Conv2d(128, 256, 3), nn.ReLU(),            # 15-17: relu3_1
    nn.ReflectionPad2d(1), nn.Conv2d(256, 256, 3), nn.ReLU(),            # 18-20
    nn.ReflectionPad2d(1), nn.Conv2d(256, 256, 3), nn.ReLU(),            # 21-23
    nn.ReflectionPad2d(1), nn.Conv2d(256, 256, 3), nn.ReLU(),            # 24-26
    nn.MaxPool2d(2, 2, ceil_mode=True),                                   # 27
    nn.ReflectionPad2d(1), nn.Conv2d(256, 512, 3), nn.ReLU(),            # 28-30: relu4_1
    nn.ReflectionPad2d(1), nn.Conv2d(512, 512, 3), nn.ReLU(),            # 31-33
    nn.ReflectionPad2d(1), nn.Conv2d(512, 512, 3), nn.ReLU(),            # 34-36
    nn.ReflectionPad2d(1), nn.Conv2d(512, 512, 3), nn.ReLU(),            # 37-39
    nn.MaxPool2d(2, 2, ceil_mode=True),                                   # 40
    nn.ReflectionPad2d(1), nn.Conv2d(512, 512, 3), nn.ReLU(),            # 41-43
    nn.ReflectionPad2d(1), nn.Conv2d(512, 512, 3), nn.ReLU(),            # 44-46
    nn.ReflectionPad2d(1), nn.Conv2d(512, 512, 3), nn.ReLU(),            # 47-49
    nn.ReflectionPad2d(1), nn.Conv2d(512, 512, 3), nn.ReLU(),            # 50-52
]

DECODER_LAYERS = [
    nn.ReflectionPad2d(1), nn.Conv2d(512, 256, 3), nn.ReLU(),
    nn.Upsample(scale_factor=2, mode="nearest"),
    nn.ReflectionPad2d(1), nn.Conv2d(256, 256, 3), nn.ReLU(),
    nn.ReflectionPad2d(1), nn.Conv2d(256, 256, 3), nn.ReLU(),
    nn.ReflectionPad2d(1), nn.Conv2d(256, 256, 3), nn.ReLU(),
    nn.ReflectionPad2d(1), nn.Conv2d(256, 128, 3), nn.ReLU(),
    nn.Upsample(scale_factor=2, mode="nearest"),
    nn.ReflectionPad2d(1), nn.Conv2d(128, 128, 3), nn.ReLU(),
    nn.ReflectionPad2d(1), nn.Conv2d(128, 64, 3), nn.ReLU(),
    nn.Upsample(scale_factor=2, mode="nearest"),
    nn.ReflectionPad2d(1), nn.Conv2d(64, 64, 3), nn.ReLU(),
    nn.ReflectionPad2d(1), nn.Conv2d(64, 3, 3),
]


def _build_encoder(models_dir: Path) -> nn.Sequential:
    # Load full VGG weights, then truncate to first 31 layers (up to relu4_1)
    full_vgg = nn.Sequential(*_VGG_FULL)
    full_vgg.load_state_dict(torch.load(
        models_dir / "vgg_normalised.pth", map_location="cpu", weights_only=True,
    ))
    encoder = nn.Sequential(*list(full_vgg.children())[:31])
    encoder.eval()
    return encoder


def _build_decoder(models_dir: Path) -> nn.Sequential:
    decoder = nn.Sequential(*DECODER_LAYERS)
    decoder.load_state_dict(torch.load(
        models_dir / "decoder.pth", map_location="cpu", weights_only=True,
    ))
    decoder.eval()
    return decoder


def _calc_mean_std(feat: torch.Tensor, eps: float = 1e-5):
    """Compute per-channel mean and std across spatial dimensions."""
    n, c = feat.size()[:2]
    feat_var = feat.view(n, c, -1).var(dim=2) + eps
    feat_std = feat_var.sqrt().view(n, c, 1, 1)
    feat_mean = feat.view(n, c, -1).mean(dim=2).view(n, c, 1, 1)
    return feat_mean, feat_std


def _adaptive_instance_norm(content_feat: torch.Tensor, style_feat: torch.Tensor) -> torch.Tensor:
    """AdaIN: sigma(y) * ((x - mu(x)) / sigma(x)) + mu(y)"""
    size = content_feat.size()
    style_mean, style_std = _calc_mean_std(style_feat)
    content_mean, content_std = _calc_mean_std(content_feat)
    normalized = (content_feat - content_mean.expand(size)) / content_std.expand(size)
    return normalized * style_std.expand(size) + style_mean.expand(size)


_to_tensor = transforms.Compose([
    transforms.ToTensor(),
])


def _load_image(img: Image.Image, max_size: int = 1920) -> torch.Tensor:
    """Resize and convert PIL Image to tensor."""
    w, h = img.size
    scale = min(max_size / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return _to_tensor(img).unsqueeze(0)


def gradient_alpha_mask(
    height: int,
    width: int,
    top_alpha: float = 0.9,
    bottom_alpha: float = 0.5,
) -> torch.Tensor:
    """Create a vertical gradient alpha mask (shape ``1×1×H×W``).

    Produces a linear ramp from *top_alpha* at the first row to
    *bottom_alpha* at the last row.  Useful for applying heavier style to
    sky / background (top) and lighter style to foreground (bottom).
    """
    ramp = torch.linspace(top_alpha, bottom_alpha, height).view(1, 1, height, 1)
    return ramp.expand(1, 1, height, width)


def luminance_alpha_mask(
    content_tensor: torch.Tensor,
    bright_alpha: float = 0.9,
    dark_alpha: float = 0.5,
    feature_size: tuple[int, int] | None = None,
) -> torch.Tensor:
    """Create an alpha mask driven by content luminance (shape ``1×1×H×W``).

    Brighter regions (e.g. sky) receive *bright_alpha*; darker regions
    (e.g. shadowed foreground) receive *dark_alpha*.  The luminance map
    is computed from the content image tensor (``1×3×H×W``) and optionally
    down-sampled to *feature_size* so it can be applied directly to the
    encoder feature maps.
    """
    # BT.601 luminance
    weights = torch.tensor([0.299, 0.587, 0.114]).view(1, 3, 1, 1)
    lum = (content_tensor * weights).sum(dim=1, keepdim=True)  # 1×1×H×W

    if feature_size is not None:
        lum = F.interpolate(lum, size=feature_size, mode="bilinear", align_corners=False)

    # Normalize to [0, 1] then scale to [dark_alpha, bright_alpha]
    lum_min = lum.min()
    lum_range = lum.max() - lum_min
    if lum_range > 0:
        lum = (lum - lum_min) / lum_range
    else:
        lum = torch.full_like(lum, 0.5)

    return dark_alpha + (bright_alpha - dark_alpha) * lum


class StyleTransfer:
    """AdaIN style transfer model."""

    def __init__(self, models_dir: Path | None = None):
        d = models_dir or MODELS_DIR
        self.encoder = _build_encoder(d)
        self.decoder = _build_decoder(d)

    @torch.no_grad()
    def transfer(
        self,
        content: Image.Image,
        style: Image.Image,
        alpha: float = 0.8,
        alpha_mask: torch.Tensor | None = None,
        max_size: int = 1920,
        alpha_mode: str = "uniform",
    ) -> Image.Image:
        """Apply style transfer.  Returns stylized PIL Image at original size.

        Parameters
        ----------
        alpha : float
            Uniform blending weight (used when *alpha_mask* is ``None``).
        alpha_mask : torch.Tensor, optional
            Spatial blending map of shape ``1×1×H×W`` whose values are in
            ``[0, 1]``.  When provided, per-region blending is used instead
            of the scalar *alpha*.  The mask is automatically resized to
            match the encoder feature-map resolution.
        max_size : int
            Maximum processing resolution in pixels.
        alpha_mode : str
            Blending mode — ``"uniform"`` (default), ``"gradient"``, or
            ``"luminance"``.  Ignored when *alpha_mask* is provided.
        """
        orig_w, orig_h = content.size
        content_tensor = _load_image(content, max_size)
        style_tensor = _load_image(style, max_size)

        content_feat = self.encoder(content_tensor)
        style_feat = self.encoder(style_tensor)

        adain_feat = _adaptive_instance_norm(content_feat, style_feat)

        if alpha_mask is not None:
            feat_h, feat_w = content_feat.shape[2:]
            mask = F.interpolate(
                alpha_mask, size=(feat_h, feat_w), mode="bilinear", align_corners=False,
            )
            blended = mask * adain_feat + (1 - mask) * content_feat
        elif alpha_mode == "gradient":
            _, _, fh, fw = content_feat.shape
            mask = gradient_alpha_mask(fh, fw, top_alpha=alpha, bottom_alpha=alpha * 0.5)
            blended = mask * adain_feat + (1 - mask) * content_feat
        elif alpha_mode == "luminance":
            mask = luminance_alpha_mask(
                content_tensor, bright_alpha=alpha, dark_alpha=alpha * 0.5,
            )
            mask = F.interpolate(
                mask, size=content_feat.shape[2:], mode="bilinear", align_corners=False,
            )
            blended = mask * adain_feat + (1 - mask) * content_feat
        else:
            blended = alpha * adain_feat + (1 - alpha) * content_feat

        output = self.decoder(blended)
        output = output.clamp(0, 1).squeeze(0)

        result = transforms.ToPILImage()(output)
        if result.size != (orig_w, orig_h):
            result = result.resize((orig_w, orig_h), Image.LANCZOS)
        return result
