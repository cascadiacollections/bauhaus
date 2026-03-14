# Research: Modern Style Transfer Architectures

> **Issue:** [#9 — Evaluate modern style transfer architectures (CAST, StyTR², AesPA-Net)](https://github.com/cascadiacollections/bauhaus/issues/9)
> **Date:** 2026-03-14
> **Status:** Complete

## Summary

bauhaus currently uses **AdaIN** (Adaptive Instance Normalization, ICCV 2017) for style transfer.
This document evaluates three newer architectures — **CAST**, **StyTR²**, and **AesPA-Net** — on
quality, speed, license compatibility, and model size impact, then provides a migration recommendation.

---

## 1. Current baseline — AdaIN

| Property | Value |
|---|---|
| **Paper** | Huang & Belongie, ICCV 2017 |
| **Approach** | Align channel-wise mean/std of content features to style features |
| **Encoder** | VGG-19 (normalized), truncated at `relu4_1` (31 layers) |
| **Decoder** | Learned mirror of the encoder |
| **Model size** | ~94 MB (VGG-19 encoder + decoder) |
| **Inference (GPU, 512×512)** | ~21 ms on Tesla P100 |
| **Inference (CPU, 512×512)** | ~1–3 s (estimated, GitHub Actions runner) |
| **Code license** | MIT ([naoto0804/pytorch-AdaIN](https://github.com/naoto0804/pytorch-AdaIN)) |
| **Weight license** | MIT (released with the code) |
| **Strengths** | Real-time, simple, well-understood, tiny dependency surface |
| **Weaknesses** | Can blur fine content structure; limited texture fidelity on complex styles |

### How bauhaus uses AdaIN today

```
content image ──► VGG-19 encoder ──► content features ─┐
                                                        ├─► AdaIN ─► α blend ─► decoder ─► output
style image   ──► VGG-19 encoder ──► style features   ─┘
```

- Alpha (default 0.8) controls content–style blending strength.
- Max inference resolution is 1920 px; output is resized back to original dimensions.
- Runs on CPU in GitHub Actions (no GPU); total wall time is dominated by model load + inference.

---

## 2. CAST — Contrastive Arbitrary Style Transfer

| Property | Value |
|---|---|
| **Paper** | Zhang et al., SIGGRAPH 2022 |
| **Approach** | Contrastive learning to learn discriminative style representations; domain enhancement module |
| **Encoder** | VGG-19 backbone + multi-layer style projector |
| **Additional modules** | Style projector, domain enhancement, contrastive loss head |
| **Model size** | ~150–250 MB (estimated; weights not bundled in repo) |
| **Inference (GPU, 512×512)** | Comparable to AdaIN (near real-time); slight overhead from projector |
| **Inference (CPU, 512×512)** | ~2–5 s (estimated, proportional to GPU overhead) |
| **Code license** | **Apache-2.0** ([zyxElsa/CAST_pytorch](https://github.com/zyxElsa/CAST_pytorch)) ✅ |
| **Weight license** | **Not explicitly provided** — weights are not bundled; must train or find author-hosted checkpoints ⚠️ |
| **Strengths** | Better style consistency, handles rare/diverse styles well, robust fine-grained textures |
| **Weaknesses** | More complex architecture; pre-trained weights availability is limited; training required |

### Quality vs AdaIN

CAST produces **more consistent stylization** with fewer local distortions, particularly on challenging or out-of-distribution styles. Fine-grained textures are better preserved. However, for typical impressionist styles (Monet, Van Gogh) that bauhaus uses, the quality gap over AdaIN is **moderate rather than dramatic**.

### Integration complexity

- Requires swapping the encoder-decoder pipeline and adding the contrastive projector modules.
- No official pre-trained checkpoint release — would need to train from scratch on a style dataset.
- Training requires GPU resources not currently part of the bauhaus workflow.

---

## 3. StyTR² — Style Transfer with Transformers

| Property | Value |
|---|---|
| **Paper** | Deng et al., CVPR 2022 |
| **Approach** | Dual transformer encoders (content + style) with transformer decoder; Content-Aware Positional Encoding (CAPE) |
| **Encoder** | Separate content & style transformer encoders + VGG backbone for features |
| **Model size** | ~500 MB+ (VGG + ViT embedding + transformer + decoder, multiple checkpoint files) |
| **Inference (GPU, 256×256)** | ~58 ms on Tesla P100 |
| **Inference (GPU, 512×512)** | ~182 ms on Tesla P100 |
| **Inference (CPU, 512×512)** | ~5–20 s (estimated, transformers are significantly slower on CPU) |
| **Code license** | **No license specified** ([diyiiyiii/StyTR-2](https://github.com/diyiiyiii/StyTR-2)) ⛔ |
| **Weight license** | **No license specified** — hosted on Google Drive ⛔ |
| **Strengths** | Best content preservation; global context via self-attention; unbiased stylization; excellent iterative stability |
| **Weaknesses** | 4–5× slower than AdaIN on GPU; much larger model; no explicit license |

### Quality vs AdaIN

StyTR² is the **highest-quality** option evaluated. It provides:
- **Superior content preservation** — structural fidelity maintained even across multiple stylization passes.
- **Better style fidelity** — consistently lower style loss in quantitative evaluations.
- **Reduced content leak** — transformer architecture separates content and style more cleanly.
- **Richer detail** — self-attention captures both global context and local texture.

### Integration complexity

- Major architectural change: dual-encoder + transformer decoder replaces simple encoder-decoder.
- Multiple large checkpoint files to download and manage.
- Significantly higher CPU inference time — problematic for GitHub Actions with no GPU.
- **License is a blocker** — no explicit open-source license means bauhaus (MIT) cannot safely adopt it.

---

## 4. AesPA-Net — Aesthetic Pattern-Aware Style Transfer

| Property | Value |
|---|---|
| **Paper** | Hong et al., ICCV 2023 |
| **Approach** | Pattern repeatability metric + enhanced attention for local/global aesthetic patterns; self-supervisory training |
| **Encoder** | VGG-19 backbone + transformer + custom attention |
| **Model size** | ~150–250 MB (VGG-19 + decoder + transformer checkpoints) |
| **Inference (GPU, 512×512)** | <1 s estimated (not precisely benchmarked in paper) |
| **Inference (CPU, 512×512)** | ~3–10 s (estimated) |
| **Code license** | **No license specified** ([Kibeom-Hong/AesPA-Net](https://github.com/Kibeom-Hong/AesPA-Net)) ⛔ |
| **Weight license** | **No license specified** — hosted on Google Drive ⛔ |
| **Strengths** | Best handling of repetitive patterns; aesthetically harmonious results; reduced repetitive artifacts |
| **Weaknesses** | No license; limited speed benchmarks; newer with less community validation |

### Quality vs AdaIN

AesPA-Net excels at **pattern-aware stylization** — it handles repetitive textures (brush strokes, textile patterns) more harmoniously. For bauhaus's curated fine-art styles (impressionist, post-impressionist, ukiyo-e), this is beneficial but not transformative since those styles are already handled reasonably by AdaIN.

### Integration complexity

- Requires VGG-19 + transformer + decoder (three separate checkpoint downloads).
- Older dependency requirements (PyTorch 1.7.1, CUDA 11.1) may conflict with bauhaus's Python 3.14 + modern PyTorch stack.
- **License is a blocker** — no explicit open-source license.

---

## 5. Comparison matrix

| Criterion | AdaIN (current) | CAST | StyTR² | AesPA-Net |
|---|---|---|---|---|
| **Quality (general)** | Good | Better | Best | Better |
| **Content preservation** | Moderate | Good | Excellent | Good |
| **Style fidelity** | Good | Very good | Excellent | Very good (pattern-aware) |
| **GPU inference (512²)** | ~21 ms | ~30–50 ms (est.) | ~182 ms | ~200 ms (est.) |
| **CPU inference (512²)** | ~1–3 s | ~2–5 s | ~5–20 s | ~3–10 s |
| **Model size** | ~94 MB | ~150–250 MB | ~500 MB+ | ~150–250 MB |
| **Code license** | MIT ✅ | Apache-2.0 ✅ | None ⛔ | None ⛔ |
| **Weight license** | MIT ✅ | Unclear ⚠️ | None ⛔ | None ⛔ |
| **Pre-trained weights** | Released ✅ | Must train ⚠️ | Google Drive | Google Drive |
| **Integration effort** | N/A (current) | Medium | High | High |
| **PyTorch compat** | Modern ✅ | Modern ✅ | Older (1.4+) ⚠️ | Older (1.7.1) ⚠️ |
| **Community maturity** | High | Medium | Medium | Low |

---

## 6. Impact on bauhaus infrastructure

### Container image size

| Architecture | Model size delta | Image size impact |
|---|---|---|
| AdaIN (current) | baseline (~94 MB) | baseline |
| CAST | +60–160 MB | +6–17% |
| StyTR² | +400 MB+ | +40%+ |
| AesPA-Net | +60–160 MB | +6–17% |

### GitHub Actions runtime (CPU-only, no GPU)

bauhaus runs style transfer on GitHub Actions free-tier runners (2-core CPU, 7 GB RAM).
Transformer-based architectures (StyTR², AesPA-Net) would increase wall-clock time
from the current ~5–10 s total pipeline to potentially **30–60+ s**, approaching the 10-minute job timeout.

### Model download time

Models are cached via GitHub Actions cache (`adain-models-v1`). Initial download times:
- AdaIN: ~94 MB → ~5 s on GitHub-hosted runner
- CAST: ~200 MB → ~10 s
- StyTR²: ~500 MB+ → ~25 s+
- AesPA-Net: ~200 MB → ~10 s

---

## 7. License compatibility analysis

bauhaus is licensed under **MIT** and outputs are **CC0-1.0**. Any adopted model must have code and weights licensed under a compatible open-source license.

| Architecture | Code license | Weight license | Compatible with MIT? |
|---|---|---|---|
| AdaIN | MIT | MIT | ✅ Yes |
| CAST | Apache-2.0 | Not specified | ⚠️ Code yes, weights unclear |
| StyTR² | None specified | None specified | ⛔ No — cannot redistribute |
| AesPA-Net | None specified | None specified | ⛔ No — cannot redistribute |

**StyTR² and AesPA-Net are not viable** for bauhaus without obtaining explicit license grants from their authors.

---

## 8. Recommendation

### Do not migrate at this time.

**Rationale:**

1. **License blockers.** The two highest-quality candidates (StyTR², AesPA-Net) have **no open-source license**, making them incompatible with bauhaus's MIT license. Incorporating unlicensed code or weights would create legal risk.

2. **CAST is the only viable alternative**, but it requires training from scratch (no released pre-trained weights), moderate quality improvement over AdaIN for bauhaus's curated impressionist styles, and adds architectural complexity.

3. **CPU inference cost.** bauhaus runs on GitHub Actions free-tier (CPU only). Transformer-based models would increase inference time by 3–10× and risk hitting job timeouts. AdaIN's ~1–3 s CPU inference is well-suited to this constraint.

4. **Marginal quality gain.** For bauhaus's use case (daily generation of stylized museum artwork at web resolution using curated fine-art styles), AdaIN produces good results. The quality improvements from newer architectures, while real, are most noticeable on challenging styles and high-resolution details — scenarios less relevant to bauhaus's pipeline.

5. **Maintenance simplicity.** AdaIN's implementation is 145 lines of straightforward PyTorch. The encoder/decoder architecture is well-understood and easy to maintain. Newer architectures would significantly increase code complexity.

### Future considerations

- **If StyTR² or AesPA-Net receive an open-source license**, re-evaluate — StyTR² would be the top pick for quality.
- **If bauhaus adds GPU support** (e.g., self-hosted runner or cloud GPU), the inference-time concern diminishes and CAST becomes more attractive.
- **If CAST releases pre-trained weights** under a permissive license, it could be a drop-in quality improvement worth the moderate integration effort.
- **Monitor [MicroAST](https://github.com/EndyWon/MicroAST)** (AAAI 2023) — a lightweight style transfer model designed for mobile/edge that could further reduce model size while maintaining quality.

---

## References

1. Huang, X. & Belongie, S. (2017). *Arbitrary Style Transfer in Real-time with Adaptive Instance Normalization.* ICCV 2017. [arXiv:1703.06868](https://arxiv.org/abs/1703.06868)
2. Zhang, Y. et al. (2022). *Domain Enhanced Arbitrary Image Style Transfer via Contrastive Learning.* SIGGRAPH 2022. [GitHub](https://github.com/zyxElsa/CAST_pytorch)
3. Deng, Y. et al. (2022). *StyTr²: Image Style Transfer with Transformers.* CVPR 2022. [arXiv:2105.14576](https://arxiv.org/abs/2105.14576)
4. Hong, K. et al. (2023). *AesPA-Net: Aesthetic Pattern-Aware Style Transfer Networks.* ICCV 2023. [arXiv:2307.09724](https://arxiv.org/abs/2307.09724)
5. Wang, Z. et al. (2023). *MicroAST: Towards Super-Fast Ultra-Resolution Arbitrary Style Transfer.* AAAI 2023. [GitHub](https://github.com/EndyWon/MicroAST)
