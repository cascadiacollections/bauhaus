#!/usr/bin/env bash
# Download curated CC0 style reference images from museum APIs.
# Run once to populate styles/ directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

download() {
    local filename="$1" url="$2"
    if [[ -f "$filename" ]]; then
        echo "  exists: $filename"
        return
    fi
    if [[ -z "$url" ]]; then
        echo "  SKIP (no URL): $filename"
        return
    fi
    echo "  downloading: $filename"
    curl -sfL --max-time 60 -o "$filename" "$url" || echo "  FAILED: $filename"
}

echo "Downloading curated style references..."

# Met Museum (direct image URLs)
download "hokusai-great-wave.jpg" "https://images.metmuseum.org/CRDImages/as/original/DP141063.jpg"
download "cezanne-mont-sainte-victoire.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT1942.jpg"
download "turner-grand-canal.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT200.jpg"
download "hiroshige-sudden-shower.jpg" "https://images.metmuseum.org/CRDImages/as/original/DP141105.jpg"
download "seurat-circus-sideshow.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT1879.jpg"
download "degas-dance-class.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT46.jpg"
download "kandinsky-composition.jpg" "https://images.metmuseum.org/CRDImages/ma/original/DT1526.jpg"
download "klimt-mada-primavesi.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT2070.jpg"
download "vangogh-wheat-field.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT1567.jpg"
download "gauguin-ia-orana.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT1025.jpg"
download "hassam-allies-day.jpg" "https://images.metmuseum.org/CRDImages/ah/original/DT284586.jpg"
download "pissarro-boulevard-montmartre.jpg" "https://images.metmuseum.org/CRDImages/ep/original/DT38.jpg"
download "homer-northeaster.jpg" "https://images.metmuseum.org/CRDImages/ap/original/DP215540.jpg"

# Art Institute of Chicago (IIIF)
download "monet-water-lilies.jpg" "https://www.artic.edu/iiif/2/3c27b499-af56-f0d5-93b5-a7f2f1ad5813/full/843,/0/default.jpg"
download "renoir-near-the-lake.jpg" "https://www.artic.edu/iiif/2/f3c80e2d-57ab-4845-f1f0-71cf1f31b497/full/843,/0/default.jpg"
download "munch-two-women.jpg" "https://www.artic.edu/iiif/2/3f0f2b4e-dd83-e1c6-03e0-1d8b1c37ee86/full/843,/0/default.jpg"
download "cassatt-childs-bath.jpg" "https://www.artic.edu/iiif/2/68090c69-b48a-4e40-9b2e-d6e74dc8ae31/full/843,/0/default.jpg"

# These need manual sourcing (no direct URL available or not CC0):
# vangogh-starry-night.jpg — MoMA (not CC0 from Met)
# morisot-the-harbor.jpg
# klee-castle-and-sun.jpg
# marc-blue-horse.jpg
# signac-port-of-saint-tropez.jpg

echo ""
echo "Done. Check for any SKIP/FAILED messages above."
echo "Missing styles can be added manually or sourced from museum websites."
