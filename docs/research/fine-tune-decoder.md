# Research: Fine-Tune Decoder on Curated Stylizations

> **Issue:** [#2 — Fine-tune decoder on curated high-quality stylizations](https://github.com/cascadiacollections/bauhaus/issues/2)
> **Date:** 2026-03-14
> **Status:** Complete
> **Depends on:** [#1 — Architecture evaluation](https://github.com/cascadiacollections/bauhaus/issues/1) (closed — staying with AdaIN)

## Summary

This document assesses the feasibility of fine-tuning the AdaIN decoder
(`decoder.pth`) on a curated dataset of high-quality stylizations to improve
output consistency and reduce artifacts.

---

## 1. Current decoder

The decoder is a mirror of the VGG-19 encoder (up to `relu4_1`) with 3
nearest-neighbor upsample stages. It was trained by the original
[pytorch-AdaIN](https://github.com/naoto0804/pytorch-AdaIN) authors on the
MS-COCO content dataset and WikiArt style dataset using a combined content +
style loss.

| Property | Value |
|---|---|
| Architecture | 15-layer CNN (Conv + ReLU + Upsample) |
| Parameters | ~1.7 M |
| Weights | `decoder.pth` (~6.8 MB) |
| Training data | MS-COCO (content) + WikiArt (style) |
| Training loss | L_content (relu4_1 features) + λ × L_style (mean + std matching across layers) |
| Training hardware | Single GPU, ~10 hours on P100 |

---

## 2. Why fine-tune?

The generic decoder was trained on the full diversity of WikiArt. bauhaus uses
a curated subset of 10 impressionist/post-impressionist styles. Fine-tuning on
outputs that match bauhaus's aesthetic could:

1. **Reduce artifacts** on the specific styles we use (Monet, Van Gogh, Hokusai, etc.).
2. **Improve color fidelity** — the decoder learns better color reconstruction for our palette.
3. **Sharper output** — optimize for the resolution and subject matter (landscapes, seascapes) we process.
4. **Smaller style loss** on curated styles without sacrificing content preservation.

---

## 3. Training data strategy

### Option A: Self-curated dataset (recommended)

1. Run the bauhaus pipeline 500–1000 times across all 10 styles and multiple content sources.
2. Manually score outputs (or use a lightweight aesthetic scorer).
3. Retain the top ~200 high-quality pairs as training data.
4. Use the original content + style images as inputs and the curated output as a soft reference.

**Pros:** Directly optimizes for bauhaus's exact use case.
**Cons:** Requires manual curation effort; limited diversity may overfit.

### Option B: Filtered WikiArt subset

1. Filter WikiArt to the same artists/movements used in `styles.json`.
2. Train on ~5,000 style images from this subset + MS-COCO content images.
3. Standard AdaIN training loop, no manual curation needed.

**Pros:** More data, less manual effort.
**Cons:** Less targeted; may not outperform the generic weights significantly.

### Recommendation

**Option A** for a targeted quality lift. The small dataset size (200 pairs) is
sufficient for fine-tuning (not training from scratch) and keeps training fast.

---

## 4. Training procedure

### Loss function

Use the original AdaIN losses with an additional perceptual sharpness term:

```
L_total = L_content + λ_style × L_style + λ_sharp × L_sharp
```

Where:
- `L_content = ||f(g(t)) - t||₂` (content features of decoded output vs AdaIN target)
- `L_style = Σ ||μ(φ(g(t))) - μ(φ(s))||₂ + ||σ(φ(g(t))) - σ(φ(s))||₂` (multi-layer style)
- `L_sharp` = total variation loss encouraging clean edges (optional)

### Hyperparameters

| Parameter | Value |
|---|---|
| Learning rate | 1e-5 (10× lower than from-scratch training) |
| Optimizer | Adam (β₁=0.9, β₂=0.999) |
| Batch size | 4–8 (limited by GPU VRAM) |
| Epochs | 20–50 (early stopping on validation loss) |
| λ_style | 10.0 (same as original) |
| λ_sharp | 0.1 (mild sharpness regularization) |
| Image size | 512×512 random crops |

### Training time estimate

| GPU | Estimated time (200 pairs, 50 epochs) |
|---|---|
| RTX 3090 / A100 | ~30–60 minutes |
| T4 (Colab free) | ~1–2 hours |
| CPU (GitHub Actions) | **Not feasible** (~24+ hours) |

---

## 5. Infrastructure options

### Option 1: Local GPU training (recommended)

- Train locally on a developer machine with a GPU.
- Commit the fine-tuned `decoder.pth` to the repository (6.8 MB, under Git LFS threshold).
- Version weights as `decoder-v2.pth` to allow rollback.

**Pros:** Simple, no CI changes, one-time effort.
**Cons:** Requires a developer with GPU access.

### Option 2: Google Colab

- Free T4 GPU is sufficient for this workload.
- Upload training data, run notebook, download weights.
- Notebook can be versioned in `docs/training/`.

**Pros:** No local GPU needed; reproducible.
**Cons:** Colab sessions time out; requires manual intervention.

### Option 3: GitHub Actions with GPU runner

- GitHub does not offer free GPU runners.
- Self-hosted runners with GPU are possible but add operational complexity.

**Recommendation:** Not worth the infrastructure overhead for a one-time fine-tune.

---

## 6. Evaluation plan

### Quantitative

- **Content loss** (relu4_1 features) on held-out test set — should not increase.
- **Style loss** (multi-layer mean/std) on bauhaus's 10 styles — should decrease.
- **LPIPS** (perceptual similarity) between fine-tuned and generic outputs — measure divergence.

### Qualitative

- Side-by-side comparison of 20 outputs (10 styles × 2 content images).
- Manual scoring on a 1–5 scale for: color accuracy, structural fidelity, artifact severity, overall aesthetics.

### Acceptance threshold

- Style loss improves by ≥10% on curated styles.
- Content loss does not increase by >5%.
- Manual review shows no regression on any style.

---

## 7. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Overfitting** to 10 styles | Medium | Validate on unseen styles; keep generic weights as fallback |
| **Color regression** on some styles | Low | Per-style evaluation before deployment |
| **Increased artifacts** at high resolution | Low | Test at 1920px (production max) during evaluation |
| **Training data too small** | Medium | Augment with random crops, flips; or fall back to Option B |

---

## 8. Recommendation

### Feasible but low priority.

**Rationale:**

1. **Moderate expected improvement.** The generic decoder already works well for impressionist styles. Fine-tuning will produce marginal rather than dramatic quality gains.

2. **One-time effort is small.** ~1–2 hours of GPU training + ~2 hours of curation and evaluation. Total effort: ~half a day.

3. **Low risk.** Fine-tuning cannot break the architecture — the decoder remains the same shape. Worst case: we revert to `decoder.pth`.

4. **Depends on other improvements first.** Higher-impact changes (#3 resolution increase, #4 quality scoring, #5 per-region alpha) should be completed and evaluated before investing in decoder fine-tuning. The cumulative effect of those changes may reduce the need for fine-tuning.

### Suggested timeline

- Complete #3, #4, #5 implementation and deploy.
- After 2–4 weeks of production data, curate a dataset from the best outputs.
- Fine-tune decoder using Option A + local GPU.
- A/B evaluate and deploy if quality improves.

---

## References

1. Huang, X. & Belongie, S. (2017). *Arbitrary Style Transfer in Real-time with Adaptive Instance Normalization.* ICCV 2017. [Training code](https://github.com/naoto0804/pytorch-AdaIN/blob/master/train.py)
2. Johnson, J. et al. (2016). *Perceptual Losses for Real-Time Style Transfer and Super-Resolution.* ECCV 2016.
3. Zhang, R. et al. (2018). *The Unreasonable Effectiveness of Deep Features as a Perceptual Metric.* CVPR 2018. (LPIPS)
