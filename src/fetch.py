"""Fetch artwork from Unsplash and museum APIs (Met Museum, Art Institute of Chicago)."""

import os
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

# Met Museum departments biased toward landscapes and scenic art.
MET_DEPARTMENTS = [
    11,  # European Paintings (landscapes, seascapes)
    21,  # Modern Art
    6,   # Asian Art (landscapes, screens)
    19,  # Photographs
    9,   # Drawings and Prints
]

# Positive signal: title words that suggest landscapes/seascapes
LANDSCAPE_PATTERN = re.compile(
    r"\b(landscape|seascape|coast|shore|river|lake|sea|ocean|harbor|harbour|"
    r"mountain|valley|field|meadow|garden|forest|wood|woods|trees|"
    r"sunset|sunrise|morning|evening|night|sky|clouds|storm|rain|snow|winter|spring|summer|autumn|"
    r"bridge|road|path|village|town|church|cathedral|ruins|"
    r"view|scene|canal|pond|marsh|cliff|island|bay|cape|"
    r"moonlight|twilight|dawn|dusk)\b",
    re.IGNORECASE,
)

# Negative signal: skip portraits, figurative, and small objects
SKIP_SUBJECT_PATTERN = re.compile(
    r"\b(portrait|self-portrait|bust|head of|figure|figures|"
    r"man standing|woman standing|seated man|seated woman|"
    r"madonna|crucifixion|pietà|pieta|saint \w+|"
    r"plate|bowl|cup|vase|jug|pitcher|teapot|bottle|"
    r"coin|medal|badge|brooch|ring|necklace|bracelet|"
    r"nail|sword|dagger|helmet|armor|shield)\b",
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
    photographer: str = ""
    photographer_url: str = ""

    def to_metadata(self) -> dict:
        d = asdict(self)
        del d["image_bytes"]
        if self.source in ("met", "artic"):
            d["license"] = "CC0-1.0"
            d["license_url"] = "https://creativecommons.org/publicdomain/zero/1.0/"
        elif self.source == "unsplash":
            d["license"] = "Unsplash License"
            d["license_url"] = "https://unsplash.com/license"
        return d


def is_safe_title(title: str) -> bool:
    return not NSFW_PATTERN.search(title)


def is_preferred_subject(title: str) -> bool:
    """Return True if the title suggests a landscape/seascape (not a portrait or small object)."""
    if SKIP_SUBJECT_PATTERN.search(title):
        return False
    return True


def is_landscape(title: str) -> bool:
    """Return True if the title strongly suggests a landscape or seascape."""
    return bool(LANDSCAPE_PATTERN.search(title))


USER_AGENT = "Bauhaus/0.1 (https://github.com/cascadiacollections/bauhaus; CC0 art service)"

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def _get(url: str, timeout: int = 30) -> requests.Response:
    resp = _session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_met(landscapes_only: bool = True) -> Artwork:
    """Fetch a random public domain artwork from the Metropolitan Museum."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            dept_id = random.choice(MET_DEPARTMENTS)
            if landscapes_only:
                query = random.choice(["landscape", "seascape", "river", "coast",
                                       "mountain", "sunset", "harbor", "garden",
                                       "forest", "village", "sky", "winter"])
            else:
                query = "*"
            search = _get(
                f"https://collectionapi.metmuseum.org/public/collection/v1/search"
                f"?departmentId={dept_id}&hasImages=true&isPublicDomain=true&q={query}",
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
            if landscapes_only and not is_preferred_subject(title):
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


def fetch_artic(landscapes_only: bool = True) -> Artwork:
    """Fetch a random public domain artwork from the Art Institute of Chicago."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            page = random.randint(1, 5000)
            resp = _get(
                f"https://api.artic.edu/api/v1/artworks"
                f"?fields=id,title,artist_title,date_display,image_id,artwork_type_title"
                f"&is_public_domain=true&limit=1&page={page}",
                timeout=15,
            ).json()

            data = resp.get("data", [])
            if not data or not data[0].get("image_id"):
                continue

            item = data[0]
            title = item.get("title", "Unknown")
            artwork_type = item.get("artwork_type_title", "")

            if not is_safe_title(title):
                print(f"Skipping NSFW: {title}", file=sys.stderr)
                continue
            if landscapes_only and not is_preferred_subject(title):
                print(f"Skipping figurative: {title} [{artwork_type}]", file=sys.stderr)
                continue

            # Prefer paintings, prints, drawings, photographs — skip sculptures, textiles, etc.
            good_types = {"Painting", "Print", "Drawing and Watercolor", "Photograph",
                          "Woodblock Print", "Lithograph", "Etching"}
            if landscapes_only and artwork_type and artwork_type not in good_types:
                print(f"Skipping type '{artwork_type}': {title}", file=sys.stderr)
                continue

            image_id = item["image_id"]
            # Request max 3000px wide — AIC IIIF caps at source resolution
            iiif_url = f"https://www.artic.edu/iiif/2/{image_id}/full/3000,/0/default.jpg"
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


def fetch_unsplash(landscapes_only: bool = True) -> Artwork:
    """Fetch a random landscape photo from Unsplash."""
    access_key = os.environ["UNSPLASH_ACCESS_KEY"]
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = _session.get(
                "https://api.unsplash.com/photos/random"
                "?query=landscape&orientation=landscape",
                headers={"Authorization": f"Client-ID {access_key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            description = (data.get("description") or "") + " " + (data.get("alt_description") or "")
            if not is_safe_title(description):
                print(f"Skipping NSFW: {description.strip()}", file=sys.stderr)
                continue

            # Download UHD image
            raw_url = data["urls"]["raw"] + "&w=3840&q=85"
            img_resp = _get(raw_url, timeout=60)

            user = data.get("user", {})
            title = data.get("alt_description") or data.get("description") or "Untitled"

            return Artwork(
                title=title.capitalize() if title else "Untitled",
                artist=user.get("name", "Unknown"),
                date="",
                source="unsplash",
                source_url=data["links"]["html"],
                image_bytes=img_resp.content,
                content_type=img_resp.headers.get("Content-Type", "image/jpeg"),
                photographer=user.get("name", ""),
                photographer_url=user.get("links", {}).get("html", ""),
            )
        except requests.RequestException as e:
            print(f"Unsplash attempt {attempt + 1} failed: {e}", file=sys.stderr)

    raise RuntimeError(f"Failed to fetch from Unsplash after {MAX_ATTEMPTS} attempts")


def fetch_artwork(source: str = "unsplash", landscapes_only: bool = True) -> Artwork:
    """Fetch artwork from the specified source.

    Args:
        source: "unsplash", "met", or "artic"
        landscapes_only: When True (default), bias toward landscapes/seascapes
                         and filter out portraits, small objects, etc.
    """
    fetchers = {
        "unsplash": fetch_unsplash,
        "met": fetch_met,
        "artic": fetch_artic,
    }
    fetcher = fetchers.get(source)
    if not fetcher:
        raise ValueError(f"Unknown source: {source}. Available: {', '.join(fetchers)}")
    return fetcher(landscapes_only=landscapes_only)
