# Research: Fine-tune Decoder on Curated High-Quality Stylizations

> **Issue:** [#10 — Research: Fine-tune decoder on curated high-quality stylizations](https://github.com/cascadiacollections/bauhaus/issues/10)
> **Date:** 2026-03-14
> **Status:** Complete

## Summary

The current AdaIN decoder (`decoder.pth`) uses generic pre-trained weights from
[naoto0804/pytorch-AdaIN](https://github.com/naoto0804/pytorch-AdaIN). These weights were trained on
MS-COCO content images paired with WikiArt style images — a broad dataset that spans many artistic
styles, photographic subjects, and quality levels. Fine-tuning on a curated dataset of high-quality
stylizations aligned with bauhaus's specific style palette (impressionist, post-impressionist,
ukiyo-e) could improve output consistency and reduce artifacts.

This document assesses the feasibility of fine-tuning, estimates the effort involved, and proposes
a training plan if viable.

---

## 1. Current decoder baseline

| Property | Value |
|---|---|
| **Architecture** | Mirror of VGG-19 encoder (conv → upsample, 3 upsample stages) |
| **Parameters** | ~3.5 M (9 Conv2d layers) |
| **Weight file** | `decoder.pth` (~15 MB) |
| **Training data (original)** | MS-COCO (content, ~80k images) + WikiArt (style, ~80k images) |
| **Training losses (original)** | Content loss (VGG `relu4_1` features) + style loss (mean/std of `relu1_1` through `relu4_1`) |
| **Known limitations** | Can blur fine content structure; occasional color bleeding; checkerboard artifacts from nearest-neighbor upsampling |

### Decoder layer breakdown

| Layer | Channels | Parameters |
|---|---|---|
| Conv2d(512 → 256, 3×3) | 512 → 256 | 1,179,904 |
| Conv2d(256 → 256, 3×3) × 3 | 256 → 256 | 1,770,240 |
| Conv2d(256 → 128, 3×3) | 256 → 128 | 295,040 |
| Conv2d(128 → 128, 3×3) | 128 → 128 | 147,584 |
| Conv2d(128 → 64, 3×3) | 128 → 64 | 73,792 |
| Conv2d(64 → 64, 3×3) | 64 → 64 | 36,928 |
| Conv2d(64 → 3, 3×3) | 64 → 3 | 1,731 |
| **Total** | | **3,505,219** |

At ~3.5 M parameters, the decoder is small enough to fine-tune efficiently on a single consumer
GPU and even (slowly) on CPU.

---

## 2. Dataset identification and curation

### Option A — Self-curated from bauhaus pipeline (recommended)

Generate a dataset using the current pipeline and hand-select the best outputs:

1. **Run the pipeline** on a diverse set of CC0 content images (landscapes, architecture,
   portraits, still life) from Unsplash, the Met, and AIC against all 10 curated styles.
2. **Generate ~500–1,000 content–style pairs** at 512×512 resolution.
3. **Manually curate** — keep only pairs where the output has:
   - Strong style presence without content degradation
   - No color bleeding or checkerboard artifacts
   - Good contrast and tonal range
4. **Target dataset size:** 200–400 high-quality curated pairs.

**Advantages:** Perfectly aligned with bauhaus's styles and content types; free to create;
CC0-compatible licensing; directly addresses observed failure modes.

**Disadvantages:** Requires manual curation effort (~2–4 hours); limited dataset size may
constrain generalization.

### Option B — Filtered subset of WikiArt + MS-COCO

Use the original training data but filter to bauhaus-relevant styles:

1. **WikiArt styles:** Filter to Impressionism, Post-Impressionism, Ukiyo-e, and Pointillism
   (matching bauhaus's 10 curated artists).
2. **MS-COCO content:** Filter to landscape/outdoor categories using COCO annotations.
3. **Quality filter:** Discard images below resolution threshold (< 512 px on shorter side).
4. **Target dataset size:** 5,000–10,000 style–content pairs.

**Advantages:** Larger dataset; reuses well-known public datasets; broader coverage.

**Disadvantages:** Less specific to bauhaus's exact content and style combinations; WikiArt
license is research-use only (not CC0) — this is a concern for MIT-licensed bauhaus.

### Option C — Synthetic ground truth via multi-pass refinement

Use the current model to generate initial stylizations, then apply postprocess corrections
(color harmonization, sharpening from `src/postprocess.py`) as pseudo ground truth:

1. **Generate pairs** using the existing pipeline with postprocessing.
2. **Train the decoder** to directly output the post-processed result, eliminating the need
   for the postprocessing step at inference time.

**Advantages:** Automates curation; could simplify the inference pipeline by baking
postprocessing into the decoder; no manual effort.

**Disadvantages:** The model can only learn what postprocessing already does — it cannot
discover genuinely better stylizations; risk of compounding artifacts.

### Recommendation: Option A (self-curated)

Option A provides the best signal for fine-tuning because it directly reflects human judgment
about what constitutes a "good" bauhaus output. The small dataset size (~200–400 pairs) is
sufficient for fine-tuning a pre-trained decoder with only 3.5 M parameters. Option B has
licensing concerns (WikiArt is research-use only). Option C is a reasonable follow-up if
Option A shows promise.

---

## 3. Fine-tuning strategies

### Strategy 1 — Full decoder fine-tuning (recommended)

Fine-tune all 3.5 M parameters with a reduced learning rate:

```
Frozen VGG-19 encoder
    ↓
Content features ──► AdaIN ──► Trainable decoder ──► Output
Style features   ──►       ↗
```

- **Learning rate:** 1e-5 to 5e-5 (10–50× lower than original training rate of 1e-4)
- **Optimizer:** Adam (β₁=0.9, β₂=0.999), same as original
- **Batch size:** 4–8 (limited by GPU memory at 512×512 resolution)
- **Epochs:** 10–20 over the curated dataset
- **Regularization:** Weight decay 1e-4; early stopping on validation loss

**Losses:**

| Loss | Weight | Purpose |
|---|---|---|
| Content loss (VGG `relu4_1` MSE) | 1.0 | Preserve content structure |
| Style loss (mean/std of `relu1_1`–`relu4_1`) | 10.0 | Match style statistics |
| Total variation loss | 1e-5 | Reduce checkerboard artifacts and noise |

The total variation loss is the key addition — it directly addresses the upsampling artifacts
that are the most common quality complaint with the current decoder.

### Strategy 2 — Last-layers-only fine-tuning

Freeze the first 6 layers (512→256 and 256→256 channels) and only fine-tune the last 3 layers
(128→64, 64→64, 64→3):

- **Trainable parameters:** ~112,451 (3.2% of total)
- **Advantage:** Very fast training; low risk of catastrophic forgetting
- **Disadvantage:** Limited capacity to fix deeper feature reconstruction issues

This is a good fallback if Strategy 1 produces unstable results or overfits on the small
curated dataset.

### Strategy 3 — Style-family-specific decoders

Train separate decoder variants for each style family:

| Family | Styles | Decoder |
|---|---|---|
| Impressionist | Monet, Degas, Cézanne | `decoder-impressionist.pth` |
| Post-Impressionist | Van Gogh, Gauguin, Seurat | `decoder-postimpressionist.pth` |
| Ukiyo-e | Hokusai, Hiroshige | `decoder-ukiyoe.pth` |
| Other | Turner, Klimt | `decoder-other.pth` |

- **Advantage:** Maximum quality per style family; each decoder can specialize
- **Disadvantage:** 4× model storage (~60 MB total); requires routing logic in inference;
  complicates `download_models.sh`; maintenance burden

**Not recommended** for initial exploration. Consider only if Strategy 1 reveals that a
single decoder cannot handle all 10 styles well.

### Recommended approach

Start with **Strategy 1** (full fine-tuning with TV loss). If overfitting occurs on the small
dataset, fall back to **Strategy 2** (last-layers-only). Evaluate on a held-out validation set
of 20–40 curated pairs.

---

## 4. Training cost estimates

### Compute requirements

| Resource | Strategy 1 (full) | Strategy 2 (last layers) |
|---|---|---|
| **GPU type (minimum)** | Any CUDA GPU with ≥4 GB VRAM | Any CUDA GPU with ≥4 GB VRAM |
| **GPU type (recommended)** | NVIDIA T4 / RTX 3060 or better | Same, or even integrated GPU |
| **Training time (T4 GPU)** | ~15–30 min (300 pairs, 20 epochs) | ~5–10 min |
| **Training time (RTX 3090)** | ~5–10 min | ~2–5 min |
| **Training time (CPU, M1 Mac)** | ~2–4 hours | ~30–60 min |
| **Training time (CPU, GH Actions)** | ~4–8 hours (not recommended) | ~1–2 hours |
| **Peak VRAM (512×512, batch 4)** | ~3–4 GB | ~2–3 GB |
| **Peak RAM (CPU)** | ~4–6 GB | ~3–4 GB |
| **Disk (dataset)** | ~200 MB (300 pairs at 512×512 JPEG) | Same |
| **Disk (checkpoints)** | ~15 MB per checkpoint | Same |

### Cloud GPU cost (if needed)

| Provider | GPU | Cost | Time for Strategy 1 |
|---|---|---|---|
| Google Colab (free) | T4 (15 GB) | $0 | ~20 min |
| Google Colab Pro | T4/A100 | $10/mo | ~10 min |
| Lambda Cloud | A10 (24 GB) | $0.75/hr | ~10 min ($0.13) |
| Vast.ai | RTX 3090 | ~$0.30/hr | ~10 min ($0.05) |

**Fine-tuning is extremely cheap** — the decoder is small (3.5 M params) and the dataset is
small (200–400 pairs). Even on a free-tier Colab T4, training completes in under 30 minutes.

---

## 5. CI viability

### Can we train in CI?

| Approach | Viable? | Notes |
|---|---|---|
| **GitHub Actions (CPU, free tier)** | ⚠️ Possible but slow | 4–8 hours for Strategy 1; risks 6-hour job timeout; blocks runner |
| **GitHub Actions (GPU, paid)** | ❌ Not available | GitHub-hosted GPU runners are in limited preview and expensive |
| **Self-hosted runner with GPU** | ✅ Viable | Requires maintaining a GPU machine; overkill for infrequent training |
| **Train locally, commit weights** | ✅ Recommended | Train once on local GPU or Colab; commit updated `decoder.pth` (~15 MB) |

### Recommended workflow: Train locally, commit weights

```
Developer machine (or Colab)           GitHub repository
┌─────────────────────────┐           ┌──────────────────────┐
│ 1. Curate dataset       │           │                      │
│ 2. Run training script  │ ──push──► │ decoder.pth (15 MB)  │
│ 3. Evaluate quality     │           │ training log (.md)   │
│ 4. Commit new weights   │           │                      │
└─────────────────────────┘           └──────────────────────┘
```

This approach is practical because:

1. **Fine-tuning is infrequent** — done once or when the style palette changes, not on every PR.
2. **The weight file is small** — `decoder.pth` at ~15 MB is well under GitHub's 100 MB file
   limit and comparable to the current committed style images.
3. **Reproducibility** — commit the training script and hyperparameters alongside the weights
   so the process can be repeated.
4. **No CI infrastructure changes** — the existing `download_models.sh` would be updated to
   fetch the fine-tuned weights from a GitHub release instead of naoto0804's release.

### Weight distribution

Rather than committing the 15 MB weight file directly to the Git history, the fine-tuned
weights should be published as a **GitHub Release asset** (matching the current pattern in
`download_models.sh`), and the download script updated accordingly. This keeps the repository
lightweight and avoids Git LFS.

---

## 6. Evaluation methodology

### Quantitative metrics

| Metric | What it measures | Tool |
|---|---|---|
| **Content loss** (VGG `relu4_1` MSE) | Structural fidelity to content image | PyTorch |
| **Style loss** (Gram matrix MSE) | Style pattern matching | PyTorch |
| **LPIPS** (Learned Perceptual Image Patch Similarity) | Perceptual quality vs reference | `lpips` Python package |
| **FID** (Fréchet Inception Distance) | Distribution quality vs curated set | `pytorch-fid` package |
| **Total variation** | Smoothness / artifact frequency | PyTorch (manual) |

### Qualitative evaluation

1. **Side-by-side comparison** — generate stylized outputs from the same content–style pairs
   using both the original and fine-tuned decoders.
2. **Blind A/B test** — present pairs without labels and record preference.
3. **Artifact audit** — specifically check for:
   - Checkerboard patterns (from nearest-neighbor upsampling)
   - Color bleeding at object boundaries
   - Loss of fine detail (text, faces, thin structures)
   - Over-stylization (style overpowers content)

### Success criteria

Fine-tuning is considered successful if:

- Content loss does not increase by more than 5% (preserves structure)
- Style loss does not increase by more than 5% (preserves stylization strength)
- Total variation decreases (fewer artifacts)
- Qualitative A/B test prefers fine-tuned output ≥60% of the time
- No new failure modes introduced (e.g., color shift, mode collapse)

---

## 7. Proposed training plan

### Phase 1 — Dataset curation (estimated: 4–6 hours)

1. Select 50 diverse CC0 content images (landscapes, architecture, portraits, still life)
   from Unsplash and museum APIs.
2. Generate 500 stylized outputs (50 content × 10 styles) using the current pipeline at
   512×512 resolution.
3. Manually review and label each output as "keep" (high quality) or "discard" (artifacts,
   poor style transfer).
4. Target: 200–400 curated pairs split 80/20 into training and validation sets.

### Phase 2 — Training script (estimated: 4–8 hours development)

Create `scripts/train_decoder.py` with:

- Data loading (content image, style image, curated output as reference)
- Loss computation (content loss + style loss + TV loss)
- Training loop with validation
- Checkpoint saving and best-model selection
- Logging (TensorBoard or simple CSV)
- CLI arguments for hyperparameters

The training script should depend only on existing project dependencies (`torch`,
`torchvision`, `pillow`) plus optionally `lpips` for evaluation.

### Phase 3 — Training and iteration (estimated: 2–4 hours)

1. Train Strategy 1 (full fine-tuning) for 20 epochs on a T4 GPU (Colab or local).
2. Evaluate on validation set using quantitative metrics.
3. If overfitting: reduce learning rate, add dropout, or fall back to Strategy 2.
4. Generate comparison outputs for all 10 styles and review qualitatively.
5. Iterate 2–3 times if needed.

### Phase 4 — Integration (estimated: 2–3 hours)

1. Publish fine-tuned `decoder.pth` as a GitHub Release asset.
2. Update `models/download_models.sh` to fetch from the new release URL.
3. Update cache key in `.github/workflows/generate.yml` (e.g., `adain-models-v2`).
4. Add the training script and a brief training log to the repository.
5. Update README.md to note that the decoder has been fine-tuned.

### Total estimated effort

| Phase | Hours | Dependencies |
|---|---|---|
| Dataset curation | 4–6 | Unsplash API access, manual review |
| Training script | 4–8 | Python development |
| Training + iteration | 2–4 | GPU access (Colab free tier sufficient) |
| Integration | 2–3 | GitHub Release, CI update |
| **Total** | **12–21 hours** | |

---

## 8. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Overfitting** on small curated dataset | Medium | Decoder memorizes training pairs; quality degrades on unseen content | Use validation set; early stopping; weight decay; fall back to Strategy 2 |
| **Catastrophic forgetting** — loses ability to handle styles outside the curated set | Low | Fine-tuned decoder fails on new styles if palette expands | Use low learning rate; mix curated + generic pairs; keep original weights as fallback |
| **Marginal improvement** — fine-tuning doesn't noticeably improve quality | Medium | Effort spent without visible benefit | Start with artifact-focused TV loss, which has a clear measurable target; set quantitative success criteria before training |
| **Dataset bias** — curated pairs over-represent certain content types | Low | Decoder performs well on landscapes but poorly on other subjects | Ensure content diversity in curation phase; include architecture, portraits, still life |
| **Weight file compatibility** — fine-tuned weights don't load correctly | Very low | Runtime error in production | Validate by running the full pipeline end-to-end before publishing |

---

## 9. Feasibility assessment

### Verdict: Viable and low-risk

Fine-tuning the AdaIN decoder is **feasible and recommended** as a low-effort improvement:

1. **Small model, cheap training.** At 3.5 M parameters, the decoder trains in under 30 minutes
   on a free Colab T4 GPU. There is no need for dedicated GPU infrastructure in CI.

2. **Clear improvement target.** The addition of total variation loss during fine-tuning directly
   addresses the most common artifact (checkerboard patterns from nearest-neighbor upsampling).
   This alone justifies the effort.

3. **Zero-risk deployment.** The fine-tuned `decoder.pth` is a drop-in replacement — no code
   changes to `src/stylize.py` are needed. If quality regresses, the original weights can be
   restored by reverting the download URL.

4. **Modest time investment.** The total estimated effort is 12–21 developer-hours, with the
   majority spent on dataset curation and training script development. Actual training compute
   is nearly free.

5. **Aligns with bauhaus's constraints.** Training locally and publishing weights as a GitHub
   Release keeps the CI pipeline unchanged and avoids GPU costs.

### Recommended next steps

1. Open an implementation issue for Phase 1 (dataset curation) and Phase 2 (training script).
2. Use Google Colab (free T4) for initial training experiments.
3. Evaluate results against the success criteria defined in Section 6.
4. If successful, publish fine-tuned weights and update `download_models.sh`.

---

## References

1. Huang, X. & Belongie, S. (2017). *Arbitrary Style Transfer in Real-time with Adaptive Instance Normalization.* ICCV 2017. [arXiv:1703.06868](https://arxiv.org/abs/1703.06868)
2. naoto0804/pytorch-AdaIN — MIT-licensed reference implementation. [GitHub](https://github.com/naoto0804/pytorch-AdaIN)
3. Lin, T.-Y. et al. (2014). *Microsoft COCO: Common Objects in Context.* ECCV 2014. [cocodataset.org](https://cocodataset.org)
4. Nichol, K. (2016). *Painter by Numbers (WikiArt).* [Kaggle](https://www.kaggle.com/c/painter-by-numbers)
5. Johnson, J. et al. (2016). *Perceptual Losses for Real-Time Style Transfer and Super-Resolution.* ECCV 2016. [arXiv:1603.08155](https://arxiv.org/abs/1603.08155)
6. Zhang, R. et al. (2018). *The Unreasonable Effectiveness of Deep Features as a Perceptual Metric (LPIPS).* CVPR 2018. [arXiv:1801.03924](https://arxiv.org/abs/1801.03924)
