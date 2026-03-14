# Research: Aesthetic Feedback Loop for Output Curation

> **Issue:** [#10 — Research: Aesthetic feedback loop for output curation](https://github.com/cascadiacollections/bauhaus/issues/10)
> **Date:** 2026-03-14
> **Status:** Complete

## Summary

bauhaus generates one stylized image per day but has no signal on whether the output is aesthetically
good or bad. This document evaluates automated scoring approaches — **NIMA** and the **LAION aesthetic
predictor** — alongside simple heuristic metrics, then designs a scoring/logging scheme for R2 metadata,
explores auto-tuning parameters from scores, and considers a manual feedback mechanism. A phased
integration plan is provided.

---

## 1. Current state — no quality signal

| Property | Value |
|---|---|
| **Generation frequency** | Once daily (4 AM UTC via GitHub Actions) |
| **Quality assessment** | None — output is uploaded unconditionally |
| **Metadata stored** | Title, artist, source, style, alpha, postprocessing config |
| **Infrastructure** | CPU-only GitHub Actions runner (2-core, 7 GB RAM), Cloudflare R2 + Workers |
| **Existing ML dependency** | PyTorch (for AdaIN style transfer) |

No score, rating, or feedback of any kind is recorded. Every generated image is published regardless of
aesthetic quality. There is no mechanism to learn from past outputs.

---

## 2. NIMA — Neural Image Assessment

| Property | Value |
|---|---|
| **Paper** | Talebi & Milanfar, TIP 2018 |
| **Approach** | CNN backbone predicting a 10-bin rating distribution; mean = aesthetic score |
| **Backbone options** | MobileNet-v2 (~16 MB), VGG-16 (~500 MB), Inception-v2 (~90 MB) |
| **Recommended backbone** | MobileNet-v2 (smallest, fastest, sufficient accuracy) |
| **Model size** | ~16 MB (MobileNet-v2 weights) |
| **Inference (GPU)** | ~5–10 ms per image |
| **Inference (CPU, 224×224)** | ~200–500 ms on modern Intel/AMD (GitHub Actions runner) |
| **Output** | Rating distribution [1–10]; mean ≈ aesthetic score, std ≈ uncertainty |
| **Training data** | AVA dataset (255k images with human aesthetic ratings) |
| **Code license** | MIT ([truskovskiyk/nima.pytorch](https://github.com/truskovskiyk/nima.pytorch)) ✅ |
| **Weight license** | MIT ✅ |
| **New dependencies** | None — uses PyTorch (already in bauhaus) + torchvision (already in bauhaus) |
| **Strengths** | Lightweight, well-understood, MIT-licensed, no new deps, distributional output |
| **Weaknesses** | Trained on photographs (AVA), not stylized artwork; may need fine-tuning for style-transfer outputs |

### How NIMA scoring would work in bauhaus

```
stylized image ──► resize to 224×224 ──► MobileNet-v2 ──► 10-bin softmax ──► mean score (1–10)
```

1. After style transfer and post-processing, score the output image.
2. Store the score (mean + std) in the metadata JSON alongside existing fields.
3. Model weights (~16 MB) cached via GitHub Actions cache, same pattern as AdaIN weights.

### Accuracy on stylized artwork

NIMA is trained on photographs, not stylized art. Scores may not perfectly align with human aesthetic
preferences for style-transferred images. However, NIMA still captures general quality signals: blur
detection, composition, color harmony, and visual salience — all relevant to stylized outputs. For
bauhaus's use case (relative ranking and trend detection, not absolute judgment), NIMA's signal is
sufficiently informative without fine-tuning.

---

## 3. LAION aesthetic predictor

| Property | Value |
|---|---|
| **Origin** | LAION-AI, used to curate LAION-Aesthetics dataset |
| **Approach** | Linear regression head on CLIP image embeddings |
| **Backbone** | CLIP ViT-L/14 (~1.3 GB) or ViT-B/32 (~400 MB) |
| **Predictor head size** | ~1–3 MB (single linear layer) |
| **Total model size** | ~400 MB (ViT-B/32) to ~1.3 GB (ViT-L/14) |
| **Inference (CPU, single image)** | ~200 ms–1 s (ViT-B/32), ~1–3 s (ViT-L/14) |
| **Output** | Single scalar aesthetic score (0–10 range) |
| **Training data** | SAC dataset (human aesthetic ratings aligned with CLIP embeddings) |
| **Code license** | MIT ([LAION-AI/aesthetic-predictor](https://github.com/LAION-AI/aesthetic-predictor)) ✅ |
| **Weight license** | MIT ✅ |
| **New dependencies** | `open-clip-torch` (~150 MB install) + CLIP model weights (~400 MB–1.3 GB) |
| **Strengths** | Trained on diverse art/photography; CLIP embeddings capture semantic quality; high correlation with human ratings (~0.92 Spearman on AVA) |
| **Weaknesses** | Large model download; adds ~400 MB+ to container/cache; new dependency (`open-clip-torch`) |

### How LAION scoring would work in bauhaus

```
stylized image ──► CLIP ViT-B/32 encoder ──► 512-dim embedding ──► linear layer ──► score (0–10)
```

1. Requires installing `open-clip-torch` and downloading CLIP weights.
2. CLIP ViT-B/32 is the minimum viable backbone (~400 MB).
3. Model weights would need caching in GitHub Actions (separate from AdaIN cache).

### Accuracy on stylized artwork

LAION's predictor was trained on a mix of photographs and artwork, making it potentially better
calibrated for stylized images than NIMA. However, the quality advantage is marginal for bauhaus's
use case (single daily image, relative ranking), and the infrastructure cost is significantly higher.

---

## 4. Simple heuristic metrics (no-ML baseline)

As a zero-cost complement to neural scoring, bauhaus can compute basic image quality heuristics using
only Pillow and NumPy (both already dependencies or trivially available):

| Metric | Method | What it measures | Implementation |
|---|---|---|---|
| **Sharpness** | Variance of Laplacian (edge filter) | Detail / blur level | `ImageFilter.FIND_EDGES` → variance (already in `quality.py`) |
| **Colorfulness** | Hasler–Süsstrunk metric | Color vibrancy / saturation | Channel difference statistics |
| **Contrast** | Grayscale standard deviation | Dynamic range | `np.std(grayscale)` |

- **Inference time:** <10 ms per image (pure NumPy, no model).
- **New dependencies:** None.
- **Value:** These metrics don't measure aesthetic quality directly, but they detect failure modes — a
  blurry, desaturated, or low-contrast output is almost certainly a bad generation. They are useful as
  **guard rails** even if a neural scorer is also used.

---

## 5. Comparison matrix

| Criterion | NIMA (MobileNet) | LAION (ViT-B/32) | Heuristic metrics |
|---|---|---|---|
| **Aesthetic accuracy** | Good (photograph-biased) | Very good (art-inclusive) | Low (proxy signals only) |
| **Model size** | ~16 MB | ~400 MB | 0 MB |
| **CPU inference** | ~200–500 ms | ~200 ms–1 s | <10 ms |
| **New dependencies** | None | `open-clip-torch` | None |
| **Code/weight license** | MIT ✅ | MIT ✅ | N/A ✅ |
| **Container image impact** | +16 MB (~2%) | +400 MB (~40%) | None |
| **GitHub Actions cache** | +16 MB (fits in existing cache) | +400 MB (new cache key) | None |
| **Integration effort** | Low | Medium | Very low |
| **Failure mode detection** | Good | Good | Excellent (purpose-built) |
| **Distributional output** | Yes (mean + std) | No (point estimate) | No |

---

## 6. Scoring and logging scheme — R2 metadata integration

### Extended metadata JSON schema

The existing metadata JSON (stored at `metadata/YYYY/MM/DD.json`) would gain an `aesthetic` object:

```json
{
  "title": "Landscape at Sunset",
  "artist": "Claude Monet",
  "source": "met",
  "alpha": 0.8,
  "style_mode": "curated",
  "postprocessing": {
    "color_harmonize": true,
    "sharpen": true,
    "upscale": false
  },
  "date": "2026-03-14",
  "generated_at": "2026-03-14T04:02:31.456789+00:00",
  "aesthetic": {
    "score": 5.83,
    "method": "heuristic-v1",
    "nima_mean": 5.83,
    "nima_std": 1.42,
    "sharpness": 1247.6,
    "colorfulness": 68.3,
    "contrast": 52.1
  }
}
```

### Design principles

1. **Additive, not breaking.** The `aesthetic` object is a new optional field. Existing consumers of the
   metadata JSON (the Worker API, EXIF embedding) continue to work unchanged.
2. **Score alongside generation.** Scoring happens in the pipeline between post-processing and upload,
   adding <1 s to the ~5–10 s total pipeline time.
3. **Immutable records.** Each day's metadata is written once and cached immutably. Scores are computed
   at generation time, not retroactively.
4. **Schema evolution.** New score fields (e.g., `laion_score`, `manual_rating`) can be added later
   without breaking existing records. Consumers should tolerate missing fields.

### Pipeline integration point

```
fetch → stylize → postprocess → [SCORE] → embed EXIF → upload
                                   ↑
                        compute scores here
                   (heuristics + NIMA in ~0.5 s)
```

### CLI flags

```
--score / --no-score    Enable/disable aesthetic scoring (default: on)
```

---

## 7. Auto-tuning parameters from scores

### What could be tuned

| Parameter | Current | Tuning strategy |
|---|---|---|
| **Alpha** (style strength) | Fixed 0.8 | Adjust based on which alpha values produce highest scores |
| **Style selection** | Day-of-year rotation | Weight rotation toward styles with higher average scores |
| **Source bias** | Default: Unsplash | Prefer sources whose content images yield higher scores |
| **Post-processing** | All enabled | Toggle harmonization/sharpening based on score impact |

### Feasibility assessment

**Not recommended at this time.** Auto-tuning requires a statistically meaningful sample of scored
outputs. At one image per day, accumulating enough data takes months:

- **Minimum viable sample per parameter value:** ~30 images (for a noisy signal like aesthetic score).
- **Alpha tuning (5 values):** 150 days to test 5 alpha levels with 30 samples each.
- **Style tuning (10 styles):** 300 days to score each style 30 times.
- **Confounding variables:** Source image quality, style–content pairing, and post-processing interact —
  isolating one parameter's effect is difficult without controlled experiments.

### Recommended path

1. **Phase 1 (now):** Log scores. Build the dataset.
2. **Phase 2 (after ~6 months of data):** Analyze score distributions per style, source, and alpha.
   Look for clear signals (e.g., "Hokusai consistently scores lower than Monet on landscape content").
3. **Phase 3 (if signals are clear):** Implement simple rules, not ML-based tuning. Example:
   - Drop styles that average >1σ below the mean.
   - Narrow alpha to the interquartile range of top-scoring outputs.

A simple JSON file could track rolling averages per style:

```json
{
  "monet-water-lilies": {"avg_score": 0.78, "count": 15},
  "hokusai-great-wave": {"avg_score": 0.82, "count": 14}
}
```

**Do not build an automated optimization loop** until there is strong evidence that parameter changes
reliably improve scores. Premature optimization with noisy data will produce erratic behavior.

---

## 8. Manual feedback mechanism

### Simple approach: GitHub Discussions

- Each day's output is posted as a GitHub Discussion (via Actions).
- Users react with 👍/👎.
- A weekly script reads reactions and updates a scores file.

**Pros:** Zero infrastructure cost; leverages existing GitHub ecosystem.
**Cons:** Requires GitHub account; low engagement expected.

### Future approach: Worker API endpoint

Add a `POST /api/:date/feedback` route to the Cloudflare Worker:

```
POST /api/2026-03-14/feedback
Content-Type: application/json

{ "rating": 1 }       ← thumbs up
{ "rating": -1 }      ← thumbs down
```

Store feedback in R2 at `feedback/YYYY/MM/DD.json`:

```json
{
  "date": "2026-03-14",
  "votes": [
    { "rating": 1, "ts": "2026-03-14T15:30:00Z" },
    { "rating": -1, "ts": "2026-03-14T16:45:00Z" }
  ],
  "summary": { "up": 1, "down": 1, "net": 0 }
}
```

| Consideration | Assessment |
|---|---|
| **No auth** | Anyone can vote. Acceptable for low-traffic internal/hobby use. Rate-limit by IP via Cloudflare rules if abuse occurs. |
| **No deduplication** | Multiple votes from the same user are possible. Acceptable at low volume; add fingerprinting later if needed. |
| **R2 read-modify-write** | Not atomic, but at one vote per hour (realistic), conflicts are negligible. |
| **Cost** | R2 Class A operations: $4.50/million. At <100 votes/day, cost rounds to $0. |

**Recommendation:** Start with GitHub Discussions for manual feedback — zero infrastructure cost.
Add the Worker API endpoint later if there is demand for a standalone web UI.

---

## 9. Recommendation

### Phase 1 — Heuristic metrics + NIMA scoring (recommended now)

**Add automated scoring to the generation pipeline with zero new dependencies.**

1. **Heuristic metrics** (sharpness, colorfulness, contrast): Implement using Pillow/NumPy.
   No new dependencies. <10 ms. Detects obvious failure modes.

2. **NIMA (MobileNet-v2)**: Implement using PyTorch + torchvision (both already dependencies).
   ~16 MB model download, ~200–500 ms inference on CPU. Provides a calibrated 1–10 aesthetic
   score with uncertainty (std).

3. **Store scores in metadata JSON**: Add an `aesthetic` object to the existing metadata schema.
   No breaking changes. Scores are logged from day one, building a dataset for future analysis.

**Rationale:**

- **Zero new dependencies.** NIMA with MobileNet-v2 uses PyTorch and torchvision, both already in
  `pyproject.toml`. Heuristic metrics use only Pillow (also already a dependency).
- **Minimal model size.** 16 MB for NIMA vs 400 MB+ for LAION. Fits within the existing GitHub
  Actions cache alongside the AdaIN weights (~94 MB).
- **Fast inference.** Total scoring adds <1 s to the pipeline (heuristics <10 ms + NIMA ~500 ms).
  The current pipeline runs in ~5–10 s total; this is a <20% increase.
- **MIT license.** Both NIMA implementations and weights are MIT-licensed, compatible with bauhaus.
- **Distributional output.** NIMA's 10-bin distribution provides both a mean score and a standard
  deviation, giving a measure of confidence. LAION provides only a point estimate.

### Phase 2 — Analysis and simple rules (after ~6 months)

- Analyze accumulated scores to identify patterns (style preferences, source quality, alpha sweet spots).
- Implement simple heuristic rules if clear signals emerge (e.g., avoid low-scoring style–source pairings).
- Do not implement automated parameter optimization — insufficient data density at one image/day.

### Phase 3 — Manual feedback (if demand exists)

- Start with GitHub Discussions (zero infrastructure cost).
- Add `POST /api/:date/feedback` endpoint to the Cloudflare Worker if a web UI is needed.
- Store votes in R2 alongside automated scores.

### Not recommended

| Approach | Reason |
|---|---|
| **LAION aesthetic predictor** | 25× larger model (~400 MB vs 16 MB), requires new dependency (`open-clip-torch`), marginal accuracy gain for bauhaus's use case. Reconsider if bauhaus adds GPU support or needs CLIP embeddings for other features. |
| **Automated parameter tuning** | Insufficient data at one image/day. Premature optimization with noisy scores would produce erratic parameter changes. Revisit after 6+ months of scoring data. |
| **NIMA fine-tuning on stylized art** | Requires a labeled dataset of stylized images with aesthetic ratings that does not exist. The base NIMA model is adequate for relative ranking and failure detection. |

---

## 10. Estimated effort

| Phase | Effort | Dependencies | Timeline |
|---|---|---|---|
| Phase 1 (heuristic + NIMA) | ~4 hours | None (existing PyTorch/torchvision) | Immediate |
| Phase 2 (analysis) | ~4 hours | Phase 1 data (6+ months) | After 6 months |
| Phase 3 (auto-tune rules) | ~4 hours | Phase 2 analysis | After 100+ outputs |
| Manual feedback (Discussions) | ~2 hours | GitHub Actions | Anytime |
| Manual feedback (Worker API) | ~4 hours | Cloudflare Worker changes | If demand exists |

---

## 11. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Heuristic score poorly correlates with human judgment | Medium | Validate against manual reviews; iterate scoring formula |
| NIMA scores biased toward photographs over art | Medium | Monitor score distributions; fine-tune on bauhaus outputs if needed; or use LAION which generalizes better to art |
| Auto-tuning overfits to scorer biases | Low | Cap style weight adjustments; maintain minimum selection probability for all styles |
| Scoring adds latency to pipeline | Very low | Heuristic: <10 ms; NIMA: ~500 ms — negligible vs. style transfer (~1–3 s) |

---

## References

1. Talebi, H. & Milanfar, P. (2018). *NIMA: Neural Image Assessment.* IEEE TIP. [arXiv:1709.05424](https://arxiv.org/abs/1709.05424)
2. Truskovskiy, K. *nima.pytorch — PyTorch NIMA implementation.* MIT License. [GitHub](https://github.com/truskovskiyk/nima.pytorch)
3. Schuhmann, C. et al. (2022). *LAION-Aesthetics.* [Blog](https://laion.ai/blog/laion-aesthetics/), [GitHub](https://github.com/LAION-AI/aesthetic-predictor)
4. Hasler, D. & Süsstrunk, S. (2003). *Measuring colourfulness in natural images.* SPIE Human Vision and Electronic Imaging.
5. Google Research. (2017). *Introducing NIMA: Neural Image Assessment.* [Blog](https://research.google/blog/introducing-nima-neural-image-assessment/)
6. Radford, A. et al. (2021). *Learning Transferable Visual Models From Natural Language Supervision.* ICML 2021. (CLIP)

