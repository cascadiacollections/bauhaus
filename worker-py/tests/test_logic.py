"""Unit tests for the pure logic module — no FastAPI, no Workers runtime."""

from __future__ import annotations

import math
import re

import pytest
from fakes import feed_metadata

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


class TestFeed:
    """The Atom renderer, mirroring the helper tests in worker/test/feed.test.ts."""

    NOW = logic.parse_date_ms("2026-10-25T20:00:00Z")
    ORIGIN = "https://bauhaus.example"

    @staticmethod
    def entry(date: str, **overrides: object) -> logic.FeedEntry:
        return logic.FeedEntry(date, feed_metadata(date, **overrides))

    def test_escapes_the_five_xml_specials_and_drops_forbidden_characters(self):
        assert logic.escape_xml("a&b<c>d\"e'f\u0001g\ufffe") == "a&amp;b&lt;c&gt;d&quot;e&apos;fg"

    @pytest.mark.parametrize(
        ("scheme", "netloc", "expected"),
        [
            ("http", "bauhaus.example", "https://bauhaus.example"),
            ("http", "Bauhaus.Example:80", "https://bauhaus.example"),
            ("https", "bauhaus.example:443", "https://bauhaus.example"),
            ("http", "localhost:8787", "https://localhost:8787"),
        ],
    )
    def test_canonicalizes_the_origin_to_https(self, scheme, netloc, expected):
        assert logic.canonical_origin(scheme, netloc) == expected

    def test_uses_generated_at_truncated_to_the_second(self):
        t = logic.entry_timestamp(self.entry("2026-10-24"), self.NOW)
        assert t == logic.parse_date_ms("2026-10-24T04:09:21Z")

    @pytest.mark.parametrize(
        "generated_at",
        [None, "yesterday", "2026-10-20T04:00:00Z", "2026-12-01T04:00:00Z"],
        ids=["missing", "unparseable", "before the publish date", "long after (backfill)"],
    )
    def test_falls_back_to_midnight_utc(self, generated_at):
        t = logic.entry_timestamp(self.entry("2026-10-24", generated_at=generated_at), self.NOW)
        assert t == logic.parse_date_ms("2026-10-24T00:00:00Z")

    def test_never_returns_a_time_in_the_future(self):
        entry = self.entry("2026-10-25", generated_at="2026-10-25T23:00:00Z")
        assert logic.entry_timestamp(entry, self.NOW) == logic.parse_date_ms("2026-10-25T00:00:00Z")

    def test_an_impossible_date_is_nan_as_in_javascript(self):
        assert math.isnan(logic.entry_timestamp(logic.FeedEntry("2026-13-45", {}), self.NOW))
        assert logic.http_date(math.nan) == "Invalid Date"

    def test_titles_entries_title_dash_artist_falling_back_to_photographer(self):
        assert (
            logic.entry_title(self.entry("2026-10-24"))
            == "Sailing Boats Returning to Yabase, Lake Biwa — Utagawa Hiroshige"
        )
        photo = logic.FeedEntry("2026-06-09", {"title": "Scenery of mountain", "photographer": "John Lee"})
        assert logic.entry_title(photo) == "Scenery of mountain — John Lee"
        assert logic.entry_title(logic.FeedEntry("2026-06-09", {})) == "Bauhaus for 2026-06-09"

    def test_renders_absolute_semantic_class_free_content_with_full_credit(self):
        html = logic.entry_content(self.entry("2026-10-24"), self.ORIGIN)
        assert (
            '<img src="https://bauhaus.example/api/2026-10-24?format=jpeg" '
            'alt="Sailing Boats Returning to Yabase, Lake Biwa by Utagawa Hiroshige, '
            'restyled in the style of Vincent van Gogh" width="2000" height="1361">'
        ) in html
        assert (
            "<cite>Sailing Boats Returning to Yabase, Lake Biwa</cite> by Utagawa Hiroshige (ca. 1835)"
        ) in html
        assert (
            '<a href="https://www.metmuseum.org/art/collection/search/36534">'
            "The Metropolitan Museum of Art</a>"
        ) in html
        assert "<cite>Wheat Field with Cypresses</cite></a> by Vincent van Gogh" in html
        assert '<a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0-1.0</a>' in html
        assert not re.search(r"class=|style=|<script", html, re.IGNORECASE)
        assert not re.search(r'(src|href)="(?!https://)', html)

    def test_escapes_hostile_metadata_and_drops_non_http_links(self):
        entry = self.entry(
            "2026-10-24", title='<script>alert("x")</script> & co', source_url="javascript:alert(1)"
        )
        html = logic.entry_content(entry, self.ORIGIN)
        assert "<script>" not in html
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; co" in html
        assert "javascript:" not in html

    def test_a_non_string_field_is_an_error_as_in_the_worker(self):
        with pytest.raises(TypeError):
            logic.entry_title(logic.FeedEntry("2026-10-24", {"title": 5}))

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("https://www.metmuseum.org/art/collection/search/36534", None),
            ("http://example.com", "http://example.com/"),
            ("HTTPS://Unsplash.COM:443/@john lee/../x?utm=a b'c#frag ment",
             "https://unsplash.com/x?utm=a%20b%27c#frag%20ment"),
            ("https:example.com\\path", "https://example.com/path"),
            ("https://bücher.example/ü", "https://xn--bcher-kva.example/%C3%BC"),
            ("https://a.example:8443", "https://a.example:8443/"),
        ],
    )
    def test_links_are_serialized_as_url_href(self, value, expected):
        # Expected values are what `new URL(value).href` gives; None means unchanged.
        assert logic.http_url_href(value) == (expected or value)

    @pytest.mark.parametrize(
        "value", [None, "", 5, "javascript:alert(1)", "ftp://x", "/relative", "https://", "https://a b/", "https://a:99999/"]
    )
    def test_non_http_or_malformed_links_are_dropped(self, value):
        assert logic.http_url_href(value) is None

    def test_builds_a_document_with_stable_ids_self_link_and_newest_updated(self):
        xml = logic.build_atom_feed(
            [self.entry("2026-10-24"), self.entry("2026-10-23")], self.ORIGIN, self.NOW
        )
        assert xml.startswith(
            '<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom"'
        )
        assert f"<title>{logic.FEED_TITLE}</title>" in xml
        assert "<id>https://bauhaus.example/api/feed.atom</id>" in xml
        assert (
            '<link rel="self" type="application/atom+xml" href="https://bauhaus.example/api/feed.atom"/>'
        ) in xml
        assert '<link rel="alternate" type="text/html" href="https://kevintcoughlin.com/"/>' in xml
        assert "  <updated>2026-10-24T04:09:21Z</updated>" in xml
        assert "<id>https://bauhaus.example/api/2026-10-23</id>" in xml
        assert "<published>2026-10-23T04:09:21Z</published>" in xml
        assert '<category term="met" label="The Metropolitan Museum of Art"/>' in xml
        assert '<category term="Vincent van Gogh" label="Style: Vincent van Gogh"/>' in xml
        # Entry HTML is escaped once into the XML, so no raw markup leaks into it.
        assert (
            '<content type="html">&lt;figure&gt;&lt;img src=&quot;'
            "https://bauhaus.example/api/2026-10-24?format=jpeg&quot;"
        ) in xml
        assert xml.count("<entry>") == 2
        assert xml.endswith("</feed>\n")

    def test_is_byte_identical_across_rebuilds(self):
        entries = [self.entry("2026-10-24")]
        assert logic.build_atom_feed(entries, "https://b.example", self.NOW) == logic.build_atom_feed(
            entries, "https://b.example", self.NOW + 86_400_000
        )

    def test_derives_the_etag_from_the_dates_and_origin(self):
        a = logic.feed_etag(["2026-10-24", "2026-10-23"], "https://b.example")
        assert re.match(r'^"feed-[0-9a-f]{32}"$', a)
        assert logic.feed_etag(["2026-10-24", "2026-10-23"], "https://b.example") == a
        assert logic.feed_etag(["2026-10-25", "2026-10-24"], "https://b.example") != a
        assert logic.feed_etag(["2026-10-24", "2026-10-23"], "https://c.example") != a

    def test_etag_matches_the_workers_digest(self):
        # sha256("1\nhttps://b.example\n2026-10-24\n2026-10-23")[:16], as feedEtag computes it.
        assert logic.feed_etag(["2026-10-24", "2026-10-23"], "https://b.example") == (
            '"feed-81e175274fef0955dba6fe7221981fa4"'
        )

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Sat, 24 Oct 2026 04:09:21 GMT", True),
            ("Sat, 24 Oct 2026 04:09:20 GMT", False),
            ("2026-10-24T04:09:21Z", True),
            ("yesterday", False),
            ("", False),
        ],
    )
    def test_not_modified_since_has_second_precision(self, header, expected):
        last_modified = logic.parse_date_ms("2026-10-24T04:09:21.999Z")
        assert logic.not_modified_since(header, last_modified) is expected

    def test_last_modified_is_an_http_date(self):
        last_modified = logic.parse_date_ms("2026-10-24T04:09:21.999Z")
        assert logic.http_date(last_modified) == "Sat, 24 Oct 2026 04:09:21 GMT"
