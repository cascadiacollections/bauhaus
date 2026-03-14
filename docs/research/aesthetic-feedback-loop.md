# Research: Aesthetic Feedback Loop for Output Curation

> **Issue:** [#7 — Aesthetic scoring and output curation](https://github.com/cascadiacollections/bauhaus/issues/7)
> **Date:** 2026-03-14
> **Status:** Complete

## Summary

This document evaluates approaches for automated aesthetic scoring of bauhaus
outputs and designs an integration plan for using scores to improve output
quality over time.

---

## 1. Problem statement

bauhaus generates one stylized image per day. Currently there is no signal on
whether a given output is aesthetically good or bad. A feedback loop could:

1. **Score each output** at generation time.
2. **Log scores** alongside existing metadata in R2.
3. **Inform future decisions** — style selection, alpha tuning, source bias.
4. **Enable curation** — surface the best outputs for display/sharing.

---

## 2. Scoring approaches evaluated

### 2a. NIMA — Neural Image Assessment

| Property | Value |
|---|---|
| **Paper** | Talebi & Milanfar, TIP 2018 |
| **Approach** | CNN (MobileNet-v2 or VGG-16 backbone) trained on AVA dataset to predict human aesthetic scores |
| **Output** | Distribution over 1–10 aesthetic score; mean ≈ expected rating |
| **Model size** | ~14 MB (MobileNet-v2) / ~500 MB (VGG-16) |
| **Inference (CPU)** | ~0.2 s (MobileNet-v2) / ~1 s (VGG-16) |
| **License** | Apache-2.0 ([idealo/image-quality-assessment](https://github.com/idealo/image-quality-assessment)) ✅ |
| **Weight license** | Released under the same repo ✅ |

**Pros:** Well-validated; compact MobileNet variant fits CPU budget; Apache-2.0 compatible.
**Cons:** Trained on photographs — may not generalize perfectly to stylized art.

### 2b. LAION Aesthetic Predictor

| Property | Value |
|---|---|
| **Paper** | Schuhmann et al., NeurIPS 2022 (LAION-5B) |
| **Approach** | Linear probe on CLIP embeddings, trained on SAC (Simulacra Aesthetic Captions) dataset |
| **Output** | Single aesthetic score (higher = more aesthetic) |
| **Model size** | ~3 MB (linear head) + ~350 MB (CLIP ViT-L/14) |
| **Inference (CPU)** | ~2–5 s (dominated by CLIP encoding) |
| **License** | MIT ([LAION-AI/aesthetic-predictor](https://github.com/LAION-AI/aesthetic-predictor)) ✅ |
| **Weight license** | MIT / OpenAI CLIP license ✅ |

**Pros:** Trained on diverse aesthetic data including art; strong generalization; MIT license.
**Cons:** Requires CLIP (350 MB download); CLIP CPU inference is slower than MobileNet-v2.

### 2c. Simple heuristic scoring (no model)

Combine lightweight metrics:
- **Colorfulness:** standard deviation of color channels (vibrant styles score higher).
- **Contrast:** RMS contrast of luminance channel.
- **Sharpness:** Laplacian variance (already implemented in `quality.py`).
- **Color harmony:** cosine similarity of histogram distributions between stylized and content.

**Pros:** Zero additional dependencies; instant computation; fully transparent.
**Cons:** Weak proxy for human aesthetic judgment; may not correlate well with perceived quality.

---

## 3. Comparison

| Criterion | NIMA (MobileNet) | LAION Aesthetic | Heuristic |
|---|---|---|---|
| **Accuracy** | Good | Very good | Weak |
| **Inference time (CPU)** | ~0.2 s | ~2–5 s | ~0.01 s |
| **Model size** | ~14 MB | ~353 MB | 0 |
| **License** | Apache-2.0 ✅ | MIT ✅ | N/A ✅ |
| **Art-specific** | Moderate | Good | Poor |
| **New dependencies** | TensorFlow/Keras or PyTorch | CLIP + PyTorch | None |
| **Integration effort** | Low | Medium | Very low |

---

## 4. Recommendation

### Phase 1: Heuristic scoring (immediate, no new dependencies)

Implement a composite heuristic score as a first step. This provides a baseline
signal with zero additional model downloads or dependencies:

```python
def aesthetic_heuristic(stylized: Image, content: Image) -> float:
    """0.0–1.0 composite score based on colorfulness, contrast, and sharpness."""
    ...
```

Store the score in metadata alongside existing fields:

```json
{
  "aesthetic_score": 0.72,
  "aesthetic_method": "heuristic-v1"
}
```

### Phase 2: NIMA MobileNet-v2 (when data available)

After collecting ~30 days of heuristic-scored outputs:

1. Add NIMA as an optional dependency.
2. Score outputs with both heuristic and NIMA.
3. Compare correlation — if NIMA provides significantly better signal, promote it to primary.
4. Keep heuristic as fallback for environments where NIMA is not installed.

### Phase 3: Auto-tuning (future)

Once enough scored outputs exist (100+):

1. **Style bias:** Weight style selection toward styles that historically produce higher scores.
2. **Alpha tuning:** Adjust default alpha if certain ranges consistently score better.
3. **Source bias:** Prefer content sources that produce better stylizations.

This could be as simple as a JSON file mapping styles to rolling average scores:

```json
{
  "monet-water-lilies": {"avg_score": 0.78, "count": 15},
  "hokusai-great-wave": {"avg_score": 0.82, "count": 14}
}
```

---

## 5. Integration plan

### Metadata schema extension

Add to the existing metadata JSON:

```json
{
  "aesthetic": {
    "score": 0.72,
    "method": "heuristic-v1",
    "colorfulness": 45.2,
    "contrast": 0.38,
    "sharpness": 1250.0
  }
}
```

### Pipeline integration point

Score after post-processing, before upload:

```
fetch → stylize → postprocess → **score** → embed EXIF → upload
```

### CLI flags

```
--score / --no-score    Enable/disable aesthetic scoring (default: on)
```

### Cloudflare Worker API extension

Add a `/api/best` endpoint that returns the highest-scored output from the last
N days, reading from metadata JSON files in R2.

---

## 6. Manual feedback mechanism

### Simple approach: GitHub Discussions

- Each day's output is posted as a GitHub Discussion (via Actions).
- Users react with 👍/👎.
- A weekly script reads reactions and updates a scores file.

### Future approach: Web UI

- Add thumbs up/down buttons to the Worker API frontend.
- Store votes in a KV namespace alongside R2 metadata.
- Requires Worker code changes and a simple frontend.

**Recommendation:** Start with GitHub Discussions for manual feedback — zero infrastructure cost, leverages existing GitHub ecosystem.

---

## 7. Estimated effort

| Phase | Effort | Dependencies | Timeline |
|---|---|---|---|
| Phase 1 (heuristic) | ~2 hours | None | Immediate |
| Phase 2 (NIMA) | ~4 hours | `tensorflow` or PyTorch NIMA port | After 30 days data |
| Phase 3 (auto-tune) | ~4 hours | Phase 1 or 2 data | After 100+ outputs |
| Manual feedback | ~2 hours | GitHub Actions | Anytime |

---

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Heuristic score poorly correlates with human judgment | Medium | Validate against manual reviews; iterate scoring formula |
| NIMA scores biased toward photographs over art | Medium | Fine-tune on bauhaus outputs if needed; or use LAION which generalizes better to art |
| Auto-tuning overfits to scorer biases | Low | Cap style weight adjustments; maintain minimum selection probability for all styles |
| Scoring adds latency to pipeline | Very low | Heuristic: <0.01 s; NIMA: ~0.2 s — negligible vs. style transfer (~1–3 s) |

---

## References

1. Talebi, H. & Milanfar, P. (2018). *NIMA: Neural Image Assessment.* IEEE TIP. [GitHub](https://github.com/idealo/image-quality-assessment)
2. Schuhmann, C. et al. (2022). *LAION-5B: An Open Large-Scale Dataset for Training Next Generation Image-Text Models.* NeurIPS 2022. [Aesthetic Predictor](https://github.com/LAION-AI/aesthetic-predictor)
3. Hasler, D. & Suesstrunk, S. (2003). *Measuring Colorfulness in Natural Images.* SPIE.
4. Radford, A. et al. (2021). *Learning Transferable Visual Models From Natural Language Supervision.* ICML 2021. (CLIP)
