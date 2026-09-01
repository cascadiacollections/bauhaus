"""Tests for fetch.py — title filtering and Artwork dataclass."""

from unittest.mock import MagicMock, patch

import pytest
import requests

import fetch
from fetch import (
    _FETCHERS,
    Artwork,
    fetch_artwork,
    is_landscape,
    is_preferred_subject,
    is_safe_title,
)

# --- is_safe_title ---

class TestIsSafeTitle:
    def test_clean_title(self):
        assert is_safe_title("Wheat Field with Cypresses") is True

    def test_clean_landscape(self):
        assert is_safe_title("View of the Grand Canal") is True

    def test_blocks_nude(self):
        assert is_safe_title("Reclining Nude") is False

    def test_blocks_bathers(self):
        assert is_safe_title("The Bathers") is False

    def test_blocks_odalisque(self):
        assert is_safe_title("Grande Odalisque") is False

    def test_blocks_venus(self):
        assert is_safe_title("The Birth of Venus") is False

    def test_blocks_nymph(self):
        assert is_safe_title("Nymphs and Satyr") is False

    def test_case_insensitive(self):
        assert is_safe_title("NUDE DESCENDING A STAIRCASE") is False

    def test_empty_string(self):
        assert is_safe_title("") is True

    def test_partial_word_no_match(self):
        # "minute" contains "nude" substring but \b boundary should prevent match
        assert is_safe_title("A Minute of Silence") is True


# --- is_preferred_subject ---

class TestIsPreferredSubject:
    def test_landscape_passes(self):
        assert is_preferred_subject("View of the Seine") is True

    def test_blocks_portrait(self):
        assert is_preferred_subject("Portrait of a Lady") is False

    def test_blocks_self_portrait(self):
        assert is_preferred_subject("Self-Portrait with Straw Hat") is False

    def test_blocks_bust(self):
        assert is_preferred_subject("Bust of Voltaire") is False

    def test_blocks_small_object(self):
        assert is_preferred_subject("Silver Teapot") is False

    def test_blocks_armor(self):
        assert is_preferred_subject("Suit of Armor") is False

    def test_empty_string(self):
        assert is_preferred_subject("") is True


# --- is_landscape ---

class TestIsLandscape:
    def test_landscape_keyword(self):
        assert is_landscape("Italian Landscape with Bridge") is True

    def test_seascape_keyword(self):
        assert is_landscape("Seascape at Sunset") is True

    def test_mountain(self):
        assert is_landscape("View of Mountain and Valley") is True

    def test_sunset(self):
        assert is_landscape("Sunset on the River") is True

    def test_forest(self):
        assert is_landscape("Path Through the Forest") is True

    def test_not_landscape(self):
        assert is_landscape("The Dance Class") is False

    def test_still_life_not_landscape(self):
        assert is_landscape("Still Life with Fruit") is False

    def test_empty_string(self):
        assert is_landscape("") is False


# --- Artwork.to_metadata ---

class TestArtworkToMetadata:
    def _make_artwork(self, source: str = "met", **kwargs) -> Artwork:
        defaults = dict(
            title="Wheat Field with Cypresses",
            artist="Vincent van Gogh",
            date="1889",
            source=source,
            source_url="https://www.metmuseum.org/art/collection/search/436535",
            image_bytes=b"\xff\xd8\xff\xe0fake-jpeg",
            photographer="",
            photographer_url="",
        )
        defaults.update(kwargs)
        return Artwork(**defaults)

    def test_returns_dict(self):
        meta = self._make_artwork().to_metadata()
        assert isinstance(meta, dict)

    def test_excludes_image_bytes(self):
        meta = self._make_artwork().to_metadata()
        assert "image_bytes" not in meta

    def test_met_license_cc0(self):
        meta = self._make_artwork(source="met").to_metadata()
        assert meta["license"] == "CC0-1.0"
        assert meta["license_url"] == "https://creativecommons.org/publicdomain/zero/1.0/"

    def test_artic_license_cc0(self):
        meta = self._make_artwork(source="artic").to_metadata()
        assert meta["license"] == "CC0-1.0"

    def test_unsplash_license(self):
        meta = self._make_artwork(source="unsplash").to_metadata()
        assert meta["license"] == "Unsplash License"

    def test_unsplash_license_url(self):
        meta = self._make_artwork(source="unsplash").to_metadata()
        assert meta["license_url"] == "https://unsplash.com/license"

    def test_photographer_in_metadata(self):
        meta = self._make_artwork(
            source="unsplash",
            photographer="Jane Doe",
            photographer_url="https://unsplash.com/@janedoe",
        ).to_metadata()
        assert meta["photographer"] == "Jane Doe"
        assert meta["photographer_url"] == "https://unsplash.com/@janedoe"

    def test_preserves_fields(self):
        meta = self._make_artwork().to_metadata()
        assert meta["title"] == "Wheat Field with Cypresses"
        assert meta["artist"] == "Vincent van Gogh"
        assert meta["date"] == "1889"
        assert meta["source"] == "met"
        assert meta["source_url"] == "https://www.metmuseum.org/art/collection/search/436535"
        assert meta["content_type"] == "image/jpeg"


# --- fetch_artwork source registration ---

class TestFetchArtworkSources:
    def test_all_sources_registered(self):
        """Every documented source resolves to a fetcher.

        Asserts against the registry rather than calling fetch_artwork(): the
        previous version called it for real — up to 10 live HTTP requests per
        source, from CI — inside `except (RuntimeError, Exception)`, which
        swallows the very ValueError it claimed to be checking for and so could
        not fail for its stated reason.
        """
        assert set(_FETCHERS) == {"unsplash", "met", "artic"}
        for fetcher in _FETCHERS.values():
            assert callable(fetcher)

    def test_unknown_source_raises(self):
        import pytest
        with pytest.raises(ValueError, match="Unknown source"):
            fetch_artwork("invalid_source")


# --- _check_quality orientation gate ---

class TestCheckQualityOrientation:
    """The museum APIs have no orientation filter, so _check_quality is where
    portrait-format artwork is kept out of the landscape-only pipeline."""

    @staticmethod
    def _jpeg(width: int, height: int) -> bytes:
        from io import BytesIO

        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        for i in range(0, width, 10):
            draw.line([(i, 0), (i, height)], fill=(0, 0, 0), width=2)
        for j in range(0, height, 10):
            draw.line([(0, j), (width, j)], fill=(0, 0, 0), width=2)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    def test_portrait_allowed_when_orientation_not_required(self):
        from fetch import _check_quality

        passed, _ = _check_quality(self._jpeg(600, 900))
        assert passed is True

    def test_portrait_rejected_when_landscape_required(self):
        from fetch import _check_quality

        passed, reason = _check_quality(self._jpeg(600, 900), landscape_orientation=True)
        assert passed is False
        assert "aspect ratio" in reason

    def test_landscape_accepted_when_landscape_required(self):
        from fetch import _check_quality

        passed, reason = _check_quality(self._jpeg(1200, 800), landscape_orientation=True)
        assert passed is True
        assert reason == ""

    def test_undecodable_bytes_rejected(self):
        from fetch import _check_quality

        passed, reason = _check_quality(b"not an image", landscape_orientation=True)
        assert passed is False
        assert "decode" in reason


class TestRetryBackoff:
    """Network retries back off; content rejections do not.

    Without a delay a source that is down absorbed all ten attempts in about
    two seconds, so the retry loop gave the upstream no chance to recover.
    """

    def test_delay_grows_with_attempt(self):
        with patch("fetch.random.random", return_value=1.0):
            delays = [fetch._retry_delay(i) for i in range(4)]
        assert delays == sorted(delays)
        assert delays[0] < delays[-1]

    def test_delay_is_capped(self):
        with patch("fetch.random.random", return_value=1.0):
            assert fetch._retry_delay(50) <= fetch.RETRY_BACKOFF_MAX_SEC

    def test_delay_is_jittered(self):
        """Jitter keeps a shared outage from resynchronising every retry."""
        with patch("fetch.random.random", return_value=0.0):
            low = fetch._retry_delay(3)
        with patch("fetch.random.random", return_value=1.0):
            high = fetch._retry_delay(3)
        assert low < high

    def test_no_sleep_after_the_final_attempt(self):
        """The last failure raises immediately — nothing left to wait for."""
        with patch("fetch.time.sleep") as sleep:
            fetch._sleep_before_retry(fetch.MAX_ATTEMPTS - 1)
        assert sleep.call_count == 0

    def test_sleeps_between_earlier_attempts(self):
        with patch("fetch.time.sleep") as sleep:
            fetch._sleep_before_retry(0)
        assert sleep.call_count == 1
        assert sleep.call_args.args[0] > 0

    def test_network_failure_backs_off_then_raises(self):
        """Ten failed attempts, nine waits, one clean RuntimeError."""
        with patch("fetch._get", side_effect=requests.RequestException("down")), \
             patch("fetch.time.sleep") as sleep, \
             pytest.raises(RuntimeError, match="after 10 attempts"):
            fetch.fetch_met()
        assert sleep.call_count == fetch.MAX_ATTEMPTS - 1

    def test_content_rejection_does_not_sleep(self):
        """An empty search result is not an outage — retry straight away."""
        empty = MagicMock()
        empty.json.return_value = {"objectIDs": []}
        with patch("fetch._get", return_value=empty), \
             patch("fetch.time.sleep") as sleep, \
             pytest.raises(RuntimeError):
            fetch.fetch_met()
        assert sleep.call_count == 0


class TestBudgets:
    """Network failures and content rejections draw on separate budgets.

    One shared counter of ten meant a healthy source offering ten portrait
    paintings in a row looked exactly like a source that was down, and the run
    died having never seen a network error — which is how 2026-08-31 ended with
    no artwork published.
    """

    @staticmethod
    def _rejected_candidate() -> MagicMock:
        """A Met search hit whose title the subject filter always rejects."""
        resp = MagicMock()
        resp.json.side_effect = lambda: {
            "objectIDs": [1],
            "title": "Portrait of a Lady",
            "primaryImage": "https://example.invalid/x.jpg",
        }
        return resp

    def test_candidate_budget_exceeds_network_budget(self):
        assert fetch.MAX_CANDIDATES > fetch.MAX_ATTEMPTS

    def test_content_rejections_get_their_own_budget(self):
        """Rejected candidates are tried MAX_CANDIDATES times, not ten."""
        with patch("fetch._get", return_value=self._rejected_candidate()) as get, \
             patch("fetch.time.sleep") as sleep, \
             pytest.raises(RuntimeError, match="candidate artworks were rejected"):
            fetch.fetch_met()
        assert sleep.call_count == 0
        # Two calls per candidate: the search, then the object lookup.
        assert get.call_count == fetch.MAX_CANDIDATES * 2

    def test_network_exhaustion_still_reports_attempts(self):
        """An outage stops at MAX_ATTEMPTS rather than burning every candidate."""
        with patch("fetch._get", side_effect=requests.RequestException("down")), \
             patch("fetch.time.sleep"), \
             pytest.raises(RuntimeError, match=f"after {fetch.MAX_ATTEMPTS} attempts"):
            fetch.fetch_artic()


class TestSourceFallback:
    """A source that comes up empty must not cost the day its artwork."""

    @staticmethod
    def _artwork(source: str) -> Artwork:
        return Artwork(
            title="Landscape", artist="Anon", date="1880",
            source=source, source_url="https://example.invalid/1",
            image_bytes=b"jpeg",
        )

    def test_falls_back_to_the_other_cc0_source(self):
        artic = MagicMock(return_value=self._artwork("artic"))
        met = MagicMock(side_effect=RuntimeError("Failed to fetch from Met Museum"))
        with patch.dict(fetch._FETCHERS, {"met": met, "artic": artic}):
            result = fetch_artwork("met")
        assert result.source == "artic"
        assert met.call_count == 1

    def test_no_fallback_when_the_first_source_works(self):
        artic = MagicMock()
        met = MagicMock(return_value=self._artwork("met"))
        with patch.dict(fetch._FETCHERS, {"met": met, "artic": artic}):
            assert fetch_artwork("met").source == "met"
        assert artic.call_count == 0

    def test_unsplash_is_never_a_fallback_target(self):
        """Its licence is not CC0 and it needs a key that may not be set."""
        assert "unsplash" not in fetch.CC0_SOURCES

    def test_unsplash_falls_back_to_cc0_sources(self):
        unsplash = MagicMock(side_effect=RuntimeError("no key"))
        met = MagicMock(return_value=self._artwork("met"))
        with patch.dict(fetch._FETCHERS, {"unsplash": unsplash, "met": met}):
            assert fetch_artwork("unsplash").source == "met"

    def test_fallback_can_be_disabled(self):
        met = MagicMock(side_effect=RuntimeError("empty"))
        artic = MagicMock()
        with patch.dict(fetch._FETCHERS, {"met": met, "artic": artic}), \
             pytest.raises(RuntimeError, match="empty"):
            fetch_artwork("met", fallback=False)
        assert artic.call_count == 0

    def test_every_source_failing_names_them_all(self):
        met = MagicMock(side_effect=RuntimeError("met is down"))
        artic = MagicMock(side_effect=RuntimeError("aic is down"))
        with patch.dict(fetch._FETCHERS, {"met": met, "artic": artic}), \
             pytest.raises(RuntimeError) as exc:
            fetch_artwork("met")
        assert "met is down" in str(exc.value)
        assert "aic is down" in str(exc.value)

    def test_unknown_source_still_raises_before_any_fetch(self):
        with pytest.raises(ValueError, match="Unknown source"):
            fetch_artwork("museum-of-nowhere")
