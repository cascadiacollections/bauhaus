"""Fetch artwork from Unsplash and museum APIs (Met Museum, Art Institute of Chicago)."""

import os
import random
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from io import BytesIO

import requests
from PIL import Image

from quality import MIN_ASPECT_RATIO, MIN_LANDSCAPE_ASPECT_RATIO, score_image

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

# Network failures tolerated before a source is declared unreachable.
MAX_ATTEMPTS = 10

# Candidate artworks examined before a source is declared unco-operative.
#
# This is a separate budget from MAX_ATTEMPTS, and deliberately larger. Both
# used to come out of one counter of ten, so a source that was perfectly
# healthy but kept offering portrait-format paintings — the Met routinely does;
# `landscapes_only` rejects every one of them — exhausted the same budget an
# outage would, and the run died having never seen a single network error. That
# is what happened on 2026-08-31: one NSFW title, two portrait rejections, and
# seven searches that returned nothing usable were enough to end the day with
# no artwork published at all.
MAX_CANDIDATES = 25

# Backoff between *network* retries. Without it a source that is down absorbs
# all ten attempts in about two seconds and the run dies having given the
# upstream no time to recover — the opposite of what a retry loop is for.
# Content rejections (NSFW title, quality gate) do not sleep: those consume a
# candidate but nothing upstream needs to recover.
RETRY_BACKOFF_BASE_SEC = 1.5
RETRY_BACKOFF_MAX_SEC = 20.0


def _retry_delay(attempt: int) -> float:
    """Exponential backoff with jitter for retry number ``attempt`` (0-based)."""
    capped = min(RETRY_BACKOFF_BASE_SEC * (2 ** attempt), RETRY_BACKOFF_MAX_SEC)
    # Jitter spreads retries so a shared outage does not resynchronise them.
    return capped * (0.5 + random.random() / 2)


def _sleep_before_retry(attempt: int) -> None:
    """Sleep between network retries, skipping the wait after the last one."""
    if attempt >= MAX_ATTEMPTS - 1:
        return
    time.sleep(_retry_delay(attempt))


def _exhausted(name: str, network_failures: int, candidates: int) -> RuntimeError:
    """Build the error for a source that ran out of one of its two budgets.

    The two exhaustion modes want different responses — an unreachable source
    is an outage to wait out, a source whose candidates all get filtered is a
    reason to try a different collection — so the message says which happened.
    """
    if network_failures >= MAX_ATTEMPTS:
        return RuntimeError(f"Failed to fetch from {name} after {MAX_ATTEMPTS} attempts")
    return RuntimeError(
        f"Failed to fetch from {name}: all {candidates} candidate artworks were "
        "rejected by the NSFW, subject, or quality filters"
    )


@dataclass(slots=True)
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
        d = {k: v for k, v in asdict(self).items() if k != "image_bytes"}
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
    return not SKIP_SUBJECT_PATTERN.search(title)


def is_landscape(title: str) -> bool:
    """Return True if the title strongly suggests a landscape or seascape."""
    return bool(LANDSCAPE_PATTERN.search(title))


USER_AGENT = "Bauhaus/0.1 (https://github.com/cascadiacollections/bauhaus; CC0 art service)"

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})


def _get(
    url: str,
    timeout: int = 30,
    headers: dict | None = None,
    params: dict | None = None,
) -> requests.Response:
    resp = _session.get(url, timeout=timeout, headers=headers, params=params)
    resp.raise_for_status()
    return resp


def _check_quality(image_bytes: bytes, landscape_orientation: bool = False) -> tuple[bool, str]:
    """Run quality scoring on raw image bytes.

    Args:
        image_bytes: Raw encoded image.
        landscape_orientation: When True, reject portrait-format images. The
            museum APIs offer no orientation filter of their own, so this is
            where that constraint is applied.

    Returns (passed, reason).  If passed is True, reason is empty.
    """
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return False, "could not decode image"
    min_ratio = MIN_LANDSCAPE_ASPECT_RATIO if landscape_orientation else MIN_ASPECT_RATIO
    result = score_image(img, min_aspect_ratio=min_ratio)
    if not result["pass"]:
        reasons = []
        if not result["resolution_ok"]:
            reasons.append(f"resolution {result['width']}x{result['height']}")
        if not result["aspect_ratio_ok"]:
            reasons.append(f"aspect ratio {result['width']}:{result['height']}")
        if not result["sharpness_ok"]:
            reasons.append(f"sharpness={result['sharpness']}")
        return False, ", ".join(reasons)
    return True, ""


def fetch_met(landscapes_only: bool = True, quality_gate: bool = True) -> Artwork:
    """Fetch a random public domain artwork from the Metropolitan Museum."""
    network_failures = 0
    candidates = 0
    while candidates < MAX_CANDIDATES and network_failures < MAX_ATTEMPTS:
        candidates += 1
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
                print(f"No Met results for department {dept_id} / '{query}'", file=sys.stderr)
                continue

            obj_id = random.choice(obj_ids)
            obj = _get(
                f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{obj_id}",
                timeout=15,
            ).json()

            img_url = obj.get("primaryImage", "")
            if not img_url:
                print(f"Met object {obj_id} has no primary image", file=sys.stderr)
                continue

            title = obj.get("title", "Unknown")
            if not is_safe_title(title):
                print(f"Skipping NSFW: {title}", file=sys.stderr)
                continue
            if landscapes_only and not is_preferred_subject(title):
                print(f"Skipping figurative: {title}", file=sys.stderr)
                continue

            img_resp = _get(img_url, timeout=60)

            if quality_gate:
                passed, reason = _check_quality(
                    img_resp.content, landscape_orientation=landscapes_only,
                )
                if not passed:
                    print(f"Quality gate rejected: {reason} ({title})", file=sys.stderr)
                    continue

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
            print(f"Met attempt {network_failures + 1} failed: {e}", file=sys.stderr)
            _sleep_before_retry(network_failures)
            network_failures += 1

    raise _exhausted("Met Museum", network_failures, candidates)


def fetch_artic(landscapes_only: bool = True, quality_gate: bool = True) -> Artwork:
    """Fetch a random public domain artwork from the Art Institute of Chicago."""
    network_failures = 0
    candidates = 0
    while candidates < MAX_CANDIDATES and network_failures < MAX_ATTEMPTS:
        candidates += 1
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
                print(f"No usable AIC artwork on page {page}", file=sys.stderr)
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

            if quality_gate:
                passed, reason = _check_quality(
                    img_resp.content, landscape_orientation=landscapes_only,
                )
                if not passed:
                    print(f"Quality gate rejected: {reason} ({title})", file=sys.stderr)
                    continue

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
            print(f"AIC attempt {network_failures + 1} failed: {e}", file=sys.stderr)
            _sleep_before_retry(network_failures)
            network_failures += 1

    raise _exhausted("AIC", network_failures, candidates)


def fetch_unsplash(landscapes_only: bool = True, quality_gate: bool = True) -> Artwork:
    """Fetch a random landscape photo from Unsplash."""
    try:
        access_key = os.environ["UNSPLASH_ACCESS_KEY"]
    except KeyError:
        raise RuntimeError(
            "UNSPLASH_ACCESS_KEY is not set. Unsplash needs an API key; the "
            "CC0 museum sources do not — try --source met or --source artic."
        ) from None
    network_failures = 0
    candidates = 0
    while candidates < MAX_CANDIDATES and network_failures < MAX_ATTEMPTS:
        candidates += 1
        try:
            params: dict = {}
            if landscapes_only:
                params["query"] = "landscape"
                params["orientation"] = "landscape"
            resp = _get(
                "https://api.unsplash.com/photos/random",
                headers={"Authorization": f"Client-ID {access_key}"},
                timeout=15,
                params=params,
            )
            data = resp.json()

            description = (data.get("description") or "") + " " + (data.get("alt_description") or "")
            if not is_safe_title(description):
                print(f"Skipping NSFW: {description.strip()}", file=sys.stderr)
                continue

            # Download UHD image
            raw_url = data["urls"]["raw"] + "&w=3840&q=85"
            img_resp = _get(raw_url, timeout=60)

            if quality_gate:
                passed, reason = _check_quality(
                    img_resp.content, landscape_orientation=landscapes_only,
                )
                if not passed:
                    print(f"Quality gate rejected: {reason}", file=sys.stderr)
                    continue

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
            print(f"Unsplash attempt {network_failures + 1} failed: {e}", file=sys.stderr)
            _sleep_before_retry(network_failures)
            network_failures += 1

    raise _exhausted("Unsplash", network_failures, candidates)


_FETCHERS: dict[str, Callable[[bool, bool], Artwork]] = {
    "unsplash": fetch_unsplash,
    "met": fetch_met,
    "artic": fetch_artic,
}

# Sources a failing source may fall back to. Both are CC0 and neither needs an
# API key, so a run that started on one and finished on the other publishes an
# image under exactly the licence terms the scheduled pipeline promises.
# Unsplash is deliberately absent: its images are not CC0, it needs a key that
# may not be configured, and asking for it is always a deliberate choice.
CC0_SOURCES = ("met", "artic")


def fetch_artwork(
    source: str = "unsplash",
    landscapes_only: bool = True,
    quality_gate: bool = True,
    fallback: bool = True,
) -> Artwork:
    """Fetch artwork from the specified source.

    Args:
        source: "unsplash", "met", or "artic"
        landscapes_only: When True (default), bias toward landscapes/seascapes
                         and filter out portraits, small objects, etc.
        quality_gate: When True (default), reject images that fail resolution,
                      aspect ratio, or sharpness checks during fetching.
        fallback: When True (default), try the remaining CC0 collections if the
                  requested one comes up empty. One museum having a bad morning
                  used to mean no artwork was published for that date at all —
                  and because /api/today follows latest.json, the site then
                  served the *previous* day's image with nothing to say it was
                  stale. Two independent collections is a much cheaper way to
                  keep the day's slot filled than a retry of the whole run.
    """
    fetcher = _FETCHERS.get(source)
    if not fetcher:
        raise ValueError(f"Unknown source: {source}. Available: {', '.join(_FETCHERS)}")

    chain = [source]
    if fallback:
        chain += [s for s in CC0_SOURCES if s != source]

    errors: list[str] = []
    for name in chain:
        try:
            return _FETCHERS[name](landscapes_only=landscapes_only, quality_gate=quality_gate)
        except RuntimeError as exc:
            errors.append(f"{name}: {exc}")
            if name != chain[-1]:
                print(f"Source '{name}' produced nothing — falling back.", file=sys.stderr)

    raise RuntimeError("Every source failed — " + "; ".join(errors))
