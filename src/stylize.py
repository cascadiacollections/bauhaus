"""AdaIN style transfer using VGG-19 encoder and learned decoder.

Based on naoto0804/pytorch-AdaIN (MIT license).
Paper: "Arbitrary Style Transfer in Real-time with Adaptive Instance Normalization"
       Xun Huang, Serge Belongie (ICCV 2017)
"""

from pathlib import Path

import torch
import torch.nn as nn
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


def gradient_alpha_mask(height: int, width: int, top: float = 1.0, bottom: float = 0.5) -> torch.Tensor:
    """Create a vertical gradient alpha mask (stronger style at top, lighter at bottom).

    Useful for landscape images where sky (top) should receive heavier
    stylization and foreground structures (bottom) should preserve more detail.

    Returns a tensor of shape ``(1, 1, height, width)`` suitable for
    element-wise multiplication with feature maps.
    """
    ramp = torch.linspace(top, bottom, height).view(1, 1, height, 1).expand(1, 1, height, width)
    return ramp


def luminance_alpha_mask(
    content_tensor: torch.Tensor,
    bright_alpha: float = 1.0,
    dark_alpha: float = 0.5,
) -> torch.Tensor:
    """Create an alpha mask based on content luminance.

    Bright regions (sky, highlights) receive ``bright_alpha`` and dark
    regions (shadows, foreground) receive ``dark_alpha``.  Intermediate
    values are linearly interpolated.

    Args:
        content_tensor: Content image tensor of shape ``(1, 3, H, W)``.
        bright_alpha: Alpha for the brightest regions.
        dark_alpha: Alpha for the darkest regions.

    Returns:
        Tensor of shape ``(1, 1, H, W)``.
    """
    # ITU-R BT.601 luminance
    luminance = (
        0.299 * content_tensor[:, 0:1]
        + 0.587 * content_tensor[:, 1:2]
        + 0.114 * content_tensor[:, 2:3]
    )
    # Normalize to [0, 1]
    lo = luminance.min()
    hi = luminance.max()
    if hi - lo > 1e-6:
        luminance = (luminance - lo) / (hi - lo)
    else:
        luminance = torch.ones_like(luminance) * 0.5
    return dark_alpha + (bright_alpha - dark_alpha) * luminance


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
        max_size: int = 1920,
        alpha_mode: str = "uniform",
    ) -> Image.Image:
        """Apply style transfer. Returns stylized PIL Image at original content size.

        Args:
            content: Content image.
            style: Style reference image.
            alpha: Base style strength 0.0–1.0.
            max_size: Maximum processing resolution in pixels.
            alpha_mode: Blending mode — ``"uniform"`` (default), ``"gradient"``,
                or ``"luminance"``.
        """
        orig_w, orig_h = content.size
        content_tensor = _load_image(content, max_size)
        style_tensor = _load_image(style, max_size)

        content_feat = self.encoder(content_tensor)
        style_feat = self.encoder(style_tensor)

        adain_feat = _adaptive_instance_norm(content_feat, style_feat)

        if alpha_mode == "gradient":
            _, _, fh, fw = content_feat.shape
            mask = gradient_alpha_mask(fh, fw, top=alpha, bottom=alpha * 0.5)
            blended = mask * adain_feat + (1 - mask) * content_feat
        elif alpha_mode == "luminance":
            mask = luminance_alpha_mask(
                content_tensor, bright_alpha=alpha, dark_alpha=alpha * 0.5,
            )
            # Downsample mask to feature-map resolution
            mask = torch.nn.functional.interpolate(
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
