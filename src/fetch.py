"""Fetch CC0 artwork from museum APIs (Met Museum, Art Institute of Chicago)."""

import random
import re
import sys
from dataclasses import dataclass, asdict
from io import BytesIO

import requests

NSFW_PATTERN = re.compile(
    r"\b(nude|naked|bather|bathers|bathing|odalisque|venus|cupid|"
    r"nymph|nymphs|erotic|sensual|courtesan|harem|leda|danae|susanna)\b",
    re.IGNORECASE,
)

# Met Museum departments biased toward landscapes, objects, and non-figurative art.
# Style transfer produces better results on these vs. portraits/figures.
MET_DEPARTMENTS = [
    6,   # Asian Art (landscapes, ceramics, screens)
    9,   # Drawings and Prints (landscapes, architecture)
    15,  # Musical Instruments
    17,  # Medieval Art (architecture, manuscripts)
    11,  # European Paintings (includes landscapes)
    21,  # Modern Art
    19,  # Photographs
]

# Prefer landscape/object/scene subjects — skip portraits and figurative works
PORTRAIT_PATTERN = re.compile(
    r"\b(portrait|self-portrait|bust|head of|figure|figures|"
    r"man standing|woman standing|seated man|seated woman|"
    r"madonna|crucifixion|pietà|pieta|saint \w+)\b",
    re.IGNORECASE,
)

MAX_ATTEMPTS = 10


@dataclass
class Artwork:
    title: str
    artist: str
    date: str
    source: str
    source_url: str
    image_bytes: bytes
    content_type: str = "image/jpeg"

    def to_metadata(self) -> dict:
        d = asdict(self)
        del d["image_bytes"]
        d["license"] = "CC0-1.0"
        return d


def is_safe_title(title: str) -> bool:
    return not NSFW_PATTERN.search(title)


def is_preferred_subject(title: str) -> bool:
    """Return True if the title suggests a landscape, object, or scene (not a portrait)."""
    return not PORTRAIT_PATTERN.search(title)


def _get(url: str, timeout: int = 30) -> requests.Response:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_met() -> Artwork:
    """Fetch a random public domain artwork from the Metropolitan Museum."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            dept_id = random.choice(MET_DEPARTMENTS)
            search = _get(
                f"https://collectionapi.metmuseum.org/public/collection/v1/search"
                f"?departmentId={dept_id}&hasImages=true&isPublicDomain=true&q=*",
                timeout=15,
            ).json()

            obj_ids = search.get("objectIDs") or []
            if not obj_ids:
                continue

            obj_id = random.choice(obj_ids)
            obj = _get(
                f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{obj_id}",
                timeout=15,
            ).json()

            img_url = obj.get("primaryImage", "")
            if not img_url:
                continue

            title = obj.get("title", "Unknown")
            if not is_safe_title(title):
                print(f"Skipping NSFW: {title}", file=sys.stderr)
                continue
            if not is_preferred_subject(title):
                print(f"Skipping figurative: {title}", file=sys.stderr)
                continue

            img_resp = _get(img_url, timeout=60)

            return Artwork(
                title=title,
                artist=obj.get("artistDisplayName", "Unknown artist"),
                date=obj.get("objectDate", ""),
                source="met",
                source_url=f"https://www.metmuseum.org/art/collection/search/{obj_id}",
                image_bytes=img_resp.content,
                content_type=img_resp.headers.get("Content-Type", "image/jpeg"),
            )
        except requests.RequestException as e:
            print(f"Met attempt {attempt + 1} failed: {e}", file=sys.stderr)

    raise RuntimeError(f"Failed to fetch from Met Museum after {MAX_ATTEMPTS} attempts")


def fetch_artic() -> Artwork:
    """Fetch a random public domain artwork from the Art Institute of Chicago."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            page = random.randint(1, 5000)
            resp = _get(
                f"https://api.artic.edu/api/v1/artworks"
                f"?fields=id,title,artist_title,date_display,image_id"
                f"&is_public_domain=true&limit=1&page={page}",
                timeout=15,
            ).json()

            data = resp.get("data", [])
            if not data or not data[0].get("image_id"):
                continue

            item = data[0]
            title = item.get("title", "Unknown")
            if not is_safe_title(title):
                print(f"Skipping NSFW: {title}", file=sys.stderr)
                continue
            if not is_preferred_subject(title):
                print(f"Skipping figurative: {title}", file=sys.stderr)
                continue

            image_id = item["image_id"]
            iiif_url = f"https://www.artic.edu/iiif/2/{image_id}/full/1920,/0/default.jpg"
            img_resp = _get(iiif_url, timeout=60)

            return Artwork(
                title=title,
                artist=item.get("artist_title") or "Unknown artist",
                date=item.get("date_display", ""),
                source="artic",
                source_url=f"https://www.artic.edu/artworks/{item['id']}",
                image_bytes=img_resp.content,
                content_type="image/jpeg",
            )
        except requests.RequestException as e:
            print(f"AIC attempt {attempt + 1} failed: {e}", file=sys.stderr)

    raise RuntimeError(f"Failed to fetch from AIC after {MAX_ATTEMPTS} attempts")


def fetch_artwork(source: str = "met") -> Artwork:
    """Fetch artwork from the specified source."""
    fetchers = {
        "met": fetch_met,
        "artic": fetch_artic,
    }
    fetcher = fetchers.get(source)
    if not fetcher:
        raise ValueError(f"Unknown source: {source}. Available: {', '.join(fetchers)}")
    return fetcher()
