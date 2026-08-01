#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Download the curated CC0 style reference images listed in styles.json.

Run once to populate the styles/ directory:

    uv run --script styles/download_styles.py

styles.json is the single source of truth — every entry's ``download_url``
is fetched to its ``filename``. Files that already exist are left alone,
so re-running is cheap and safe.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

STYLES_DIR = Path(__file__).resolve().parent
MANIFEST = STYLES_DIR / "styles.json"

# Museum image CDNs reject requests without a real User-Agent, so send the
# same one src/fetch.py uses against their APIs.
USER_AGENT = "Bauhaus/0.1 (https://github.com/cascadiacollections/bauhaus; CC0 art service)"

TIMEOUT_SEC = 60


def _fetch_to(url: str, dest: Path) -> None:
    """Stream *url* to *dest*, identifying ourselves like src/fetch.py does."""
    request = Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 — manifest URL
    with urlopen(request, timeout=TIMEOUT_SEC) as response, dest.open("wb") as fh:  # noqa: S310
        shutil.copyfileobj(response, fh)


def download_styles(styles_dir: Path = STYLES_DIR, force: bool = False) -> list[str]:
    """Download every style image declared in styles.json.

    Returns the list of filenames that could not be downloaded.
    """
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failed: list[str] = []

    for entry in entries:
        filename = entry["filename"]
        dest = styles_dir / filename

        if dest.exists() and not force:
            print(f"  exists: {filename}")
            continue

        url = entry.get("download_url")
        if not url:
            print(f"  SKIP (no download_url): {filename}", file=sys.stderr)
            failed.append(filename)
            continue

        print(f"  downloading: {filename}")
        try:
            _fetch_to(url, dest)
        except (URLError, OSError) as exc:
            dest.unlink(missing_ok=True)  # remove partial download
            print(f"  FAILED: {filename} ({exc})", file=sys.stderr)
            failed.append(filename)

    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="Re-download images that are already present")
    args = parser.parse_args()

    print("Downloading curated style references...")
    failed = download_styles(force=args.force)

    if failed:
        print(f"\n{len(failed)} style(s) could not be downloaded:", file=sys.stderr)
        for name in failed:
            print(f"  - {name}", file=sys.stderr)
        return 1

    print(f"\nAll {len(json.loads(MANIFEST.read_text(encoding='utf-8')))} styles ready in {STYLES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
