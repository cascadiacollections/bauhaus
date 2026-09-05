"""Unit tests for the pure logic module — no FastAPI, no Workers runtime."""

from __future__ import annotations

import pytest

from bauhaus_api import logic


class TestFormatNegotiation:
    @pytest.mark.parametrize("value", ["avif", "webp", "jpeg", "auto", "AVIF", "Auto"])
    def test_accepts_supported_values(self, value):
        assert logic.has_invalid_format({"format": value}) is False

    @pytest.mark.parametrize("value", ["png", "jpg", "", "gif"])
    def test_rejects_unsupported_values(self, value):
        assert logic.has_invalid_format({"format": value}) is True

    def test_absent_param_is_not_invalid(self):
        assert logic.has_invalid_format({}) is False

    def test_explicit_param_beats_accept_header(self):
        assert logic.negotiate_format("image/avif,image/webp", {"format": "jpeg"}) == "jpeg"

    def test_avif_wins_over_webp(self):
        assert logic.negotiate_format("image/avif,image/webp", {}) == "avif"

    def test_webp_when_no_avif(self):
        assert logic.negotiate_format("image/webp,image/*", {}) == "webp"

    def test_jpeg_is_the_floor(self):
        assert logic.negotiate_format("*/*", {}) == "jpeg"

    def test_auto_falls_through_to_accept(self):
        assert logic.negotiate_format("image/avif", {"format": "auto"}) == "avif"


class TestCandidateKeys:
    def test_negotiated_format_then_jpeg_fallback(self):
        assert logic.candidate_keys("stylized/2026/09/04", "avif") == [
            ("stylized/2026/09/04.avif", "image/avif"),
            ("stylized/2026/09/04.jpg", "image/jpeg"),
        ]

    def test_jpeg_has_no_redundant_fallback(self):
        assert logic.candidate_keys("stylized/2026/09/04", "jpeg") == [
            ("stylized/2026/09/04.jpg", "image/jpeg")
        ]

    def test_progressive_outranks_negotiated_format(self):
        keys = logic.candidate_keys("s/d", "avif", progressive=True)
        assert keys[0] == ("s/d.progressive.jpg", "image/jpeg")

    def test_progressive_outranks_strip(self):
        keys = logic.candidate_keys("s/d", "jpeg", progressive=True, strip=True)
        assert [k for k, _ in keys] == ["s/d.progressive.jpg", "s/d.stripped.jpg", "s/d.jpg"]


class TestEtags:
    @pytest.mark.parametrize(
        ("header", "etag"),
        [
            ('"abc"', '"abc"'),
            ("*", '"abc"'),
            ('W/"abc"', '"abc"'),
            ('"abc"', 'W/"abc"'),
            ('"x", "abc"', '"abc"'),
        ],
    )
    def test_matches(self, header, etag):
        assert logic.etag_matches(header, etag) is True

    @pytest.mark.parametrize(("header", "etag"), [('"abc"', '"def"'), ('"abc"', None)])
    def test_does_not_match(self, header, etag):
        assert logic.etag_matches(header, etag) is False


class TestArchiveQuery:
    def test_defaults(self):
        assert logic.parse_archive_query({}) == (logic.ARCHIVE_DEFAULT_LIMIT, None)

    def test_parses_valid_values(self):
        assert logic.parse_archive_query({"limit": "5", "before": "2026-09-04"}) == (
            5,
            "2026-09-04",
        )

    @pytest.mark.parametrize("limit", ["abc", "-1", "0", "1001", "1.5", ""])
    def test_rejects_bad_limit(self, limit):
        with pytest.raises(logic.ArchiveQueryError):
            logic.parse_archive_query({"limit": limit})

    @pytest.mark.parametrize("before", ["2026-9-4", "yesterday", "20260904"])
    def test_rejects_bad_before(self, before):
        with pytest.raises(logic.ArchiveQueryError):
            logic.parse_archive_query({"before": before})


class TestArchivePage:
    dates = ["2026-09-01", "2026-09-02", "2026-09-03"]

    def test_newest_first(self):
        page = logic.build_archive_page(self.dates, True, 10, None)
        assert page["dates"] == ["2026-09-03", "2026-09-02", "2026-09-01"]
        assert page["count"] == 3
        assert page["total"] == 3
        assert "next" not in page
        assert "truncated" not in page

    def test_paging_emits_next(self):
        page = logic.build_archive_page(self.dates, True, 2, None)
        assert page["dates"] == ["2026-09-03", "2026-09-02"]
        assert page["next"] == "/api/archive?limit=2&before=2026-09-02"

    def test_before_is_exclusive(self):
        page = logic.build_archive_page(self.dates, True, 10, "2026-09-02")
        assert page["dates"] == ["2026-09-01"]
        # total stays the size of the archive, not the size of the page
        assert page["total"] == 3

    def test_incomplete_walk_is_flagged(self):
        assert logic.build_archive_page(self.dates, False, 10, None)["truncated"] is True


class TestMetadataKeys:
    def test_extracts_date(self):
        assert logic.date_from_metadata_key("metadata/2026/09/04.json") == "2026-09-04"

    @pytest.mark.parametrize(
        "key",
        [
            "metadata/2026/09/04.json.sig",  # would double-count signed days
            "stylized/2026/09/04.jpg",
            "metadata/2026/9/4.json",
            "latest.json",
        ],
    )
    def test_ignores_other_keys(self, key):
        assert logic.date_from_metadata_key(key) is None


class TestHeaders:
    def test_today_overrides_stored_cache_control(self):
        headers = logic.image_headers("k.jpg", '"e"', "public, max-age=99", "image/jpeg", True)
        assert headers["Cache-Control"] == logic.TODAY_CACHE

    def test_stored_cache_control_wins_for_dated(self):
        headers = logic.image_headers("k.jpg", '"e"', "public, max-age=99", "image/jpeg", False)
        assert headers["Cache-Control"] == "public, max-age=99"

    def test_immutable_is_the_fallback(self):
        headers = logic.image_headers("k.jpg", None, None, "image/jpeg", False)
        assert headers["Cache-Control"] == logic.IMMUTABLE_CACHE
        assert "ETag" not in headers

    @pytest.mark.parametrize(
        ("key", "variant"),
        [
            ("s/d.progressive.jpg", "progressive"),
            ("s/d.stripped.jpg", "stripped"),
            ("s/d.avif", "baseline"),
        ],
    )
    def test_variant_header(self, key, variant):
        assert logic.image_headers(key, None, None, "image/jpeg", False)["X-Variant"] == variant

    def test_not_modified_repeats_caching_and_vary(self):
        headers = logic.not_modified_headers('"e"', True)
        assert headers["Cache-Control"] == logic.TODAY_CACHE
        assert headers["Vary"] == "Accept"
        assert headers["Access-Control-Allow-Origin"] == "*"


class TestTelemetryHelpers:
    def test_default_origins_when_unset(self):
        assert logic.get_allowed_origins(None) == {
            "https://kevintcoughlin.com",
            "https://www.kevintcoughlin.com",
        }

    def test_blank_falls_back_to_default(self):
        assert logic.get_allowed_origins("   ") == logic.get_allowed_origins(None)

    def test_splits_and_trims(self):
        assert logic.get_allowed_origins("https://a.com, https://b.com,") == {
            "https://a.com",
            "https://b.com",
        }

    @pytest.mark.parametrize("ua", ["iPhone", "Android 14", "Mobile Safari"])
    def test_mobile_uas(self, ua):
        assert logic.classify_ua(ua) == "mobile"

    def test_desktop_ua(self):
        assert logic.classify_ua("Mozilla/5.0 (Macintosh)") == "desktop"

    def test_splits_url(self):
        assert logic.split_url("https://x.com/a/b?q=1") == ("x.com", "/a/b")

    @pytest.mark.parametrize("value", ["not a url", "", None, "/relative"])
    def test_malformed_url_is_empty(self, value):
        assert logic.split_url(value) == ("", "")

    def test_vitals_shape(self):
        point = logic.vitals_data_point(
            {"name": "LCP", "rating": "good", "navigationType": "navigate",
             "url": "https://x.com/p", "value": 1200.5},
            "mobile",
        )
        assert point == {
            "blobs": ["LCP", "good", "navigate", "x.com", "/p", "mobile"],
            "doubles": [1200.5],
            "indexes": ["x.com"],
        }

    def test_vitals_tolerates_missing_fields(self):
        point = logic.vitals_data_point({}, "desktop")
        assert point["blobs"] == ["", "", "", "", "", "desktop"]
        assert point["doubles"] == [0.0]

    def test_error_shape(self):
        point = logic.error_data_point(
            {"message": "boom", "source": "https://x.com/a.js", "lineno": 3, "colno": 7},
            "desktop",
        )
        assert point == {
            "blobs": ["boom", "https://x.com/a.js", "x.com", "/a.js", "desktop"],
            "doubles": [3.0, 7.0],
            "indexes": ["x.com"],
        }

    def test_non_numeric_doubles_become_zero(self):
        point = logic.error_data_point({"lineno": "abc"}, "desktop")
        assert point["doubles"] == [0.0, 0.0]


class TestHealth:
    def test_fresh_is_ok(self):
        import time

        status, body = logic.health_payload("2026-09-04", time.mktime((2026, 9, 4, 12, 0, 0, 0, 0, 0)))
        assert status == 200
        assert body["status"] == "ok"

    def test_two_days_stale_is_503(self):
        from datetime import UTC, datetime

        now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC).timestamp()
        status, body = logic.health_payload("2026-09-04", now)
        assert status == 503
        assert body["status"] == "stale"
        assert body["stale_days"] == 2

    def test_one_day_stale_is_still_ok(self):
        from datetime import UTC, datetime

        now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC).timestamp()
        status, _ = logic.health_payload("2026-09-04", now)
        assert status == 200

    def test_unparseable_date_is_unhealthy(self):
        status, body = logic.health_payload("not-a-date", 0)
        assert status == 503
        assert body["stale_days"] is None


def test_date_path():
    assert logic.date_path("2026-09-04") == "2026/09/04"
