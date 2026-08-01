"""End-to-end orchestration tests for main().

main() is where the pipeline's ordering, gating and plumbing live, and it was
the largest wholly untested surface in the project: every module it calls had
unit tests, but nothing exercised the sequence that wires them together. These
tests patch the expensive leaves (network, model, R2) and assert on what main()
does with them.
"""

import os
import sys
from datetime import date
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

import main as main_mod
from fetch import Artwork
from upload import AlreadyPublishedError

# Environment variables main() reads that would otherwise leak in from the
# developer's shell and change what these tests exercise.
_CONTROLLED_ENV = frozenset({
    "GPG_KEY_ID", "GPG_PASSPHRASE", "GPG_PRIVATE_KEY",
    "STYLE_MODE", "LANDSCAPES_ONLY", "MEMORY_PROFILE",
    "GENERATE_VARIANTS", "MAX_SIZE", "METRICS_OUT", "METRICS_LABEL",
})


def _jpeg(width: int = 400, height: int = 300) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), (90, 120, 160)).save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _artwork() -> Artwork:
    return Artwork(
        title="A Wide Valley",
        artist="Test Painter",
        date="1889",
        source="met",
        source_url="https://www.metmuseum.org/art/collection/search/1",
        image_bytes=_jpeg(),
    )


class _Harness:
    """Patches every expensive dependency of main() and records the calls."""

    def __init__(self, argv: list[str], env: dict[str, str] | None = None):
        self.argv = ["main.py", *argv]
        self.env = env or {}
        self.upload = MagicMock(return_value={"stylized": "stylized/2026/01/02.jpg"})
        self.sign = MagicMock(return_value=b"-----BEGIN PGP SIGNATURE-----")
        self.fetch = MagicMock(return_value=_artwork())
        self.ensure_models = MagicMock()
        # Default: the date is free. Give it a side_effect of
        # AlreadyPublishedError to exercise the preflight rejection.
        self.assert_unpublished = MagicMock()

    def __enter__(self):
        style_img = Image.new("RGB", (256, 256), (200, 60, 40))
        stylized = Image.new("RGB", (400, 300), (30, 90, 150))

        transfer = MagicMock()
        transfer.transfer.return_value = stylized

        # Start from the real environment minus every variable these tests care
        # about, so a developer's exported GPG_* or STYLE_MODE cannot change the
        # result, then layer this case's values on top.
        env = {k: v for k, v in os.environ.items() if k not in _CONTROLLED_ENV}
        env.update(self.env)

        self._patches = [
            patch.object(sys, "argv", self.argv),
            patch.dict(os.environ, env, clear=True),
            patch.object(main_mod, "fetch_artwork", self.fetch),
            patch.object(main_mod, "pick_style", return_value=(style_img, {"style_title": "Rain"})),
            patch.object(main_mod, "ensure_models", self.ensure_models),
            patch.object(main_mod, "StyleTransfer", MagicMock(return_value=transfer)),
            patch.object(main_mod, "upload", self.upload),
            patch.object(main_mod, "assert_unpublished", self.assert_unpublished),
            patch.object(main_mod, "sign_metadata", self.sign),
            patch.object(main_mod, "utc_today", return_value=date(2026, 1, 2)),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestMainOrchestration:
    def test_uploads_with_the_fetched_and_stylized_artwork(self):
        with _Harness(["--source", "met"]) as h:
            main_mod.main()

        assert h.upload.call_count == 1
        kwargs = h.upload.call_args.kwargs
        args = h.upload.call_args.args
        metadata = args[2]
        assert metadata["title"] == "A Wide Valley"
        assert metadata["style_title"] == "Rain"
        assert metadata["license"] == "CC0-1.0"
        assert kwargs["today"] == date(2026, 1, 2)

    def test_models_are_ensured_before_style_transfer(self):
        """A missing or corrupt weight file must surface before the GPU work."""
        with _Harness([]) as h:
            main_mod.main()
        assert h.ensure_models.call_count == 1

    def test_source_flag_reaches_the_fetcher(self):
        with _Harness(["--source", "artic"]) as h:
            main_mod.main()
        assert h.fetch.call_args.args[0] == "artic"

    def test_landscapes_only_defaults_on(self):
        with _Harness([]) as h:
            main_mod.main()
        assert h.fetch.call_args.kwargs["landscapes_only"] is True

    def test_any_subject_disables_the_landscape_filter(self):
        with _Harness(["--any-subject"]) as h:
            main_mod.main()
        assert h.fetch.call_args.kwargs["landscapes_only"] is False

    def test_dry_run_never_uploads(self, tmp_path):
        with _Harness(["--dry-run"]) as h, patch.object(main_mod, "OUTPUT_DIR", tmp_path):
            main_mod.main()
        assert h.upload.call_count == 0
        assert (tmp_path / "stylized.jpg").exists()
        assert (tmp_path / "metadata.json").exists()

    def test_overwrite_flag_is_plumbed_through(self):
        with _Harness(["--overwrite"]) as h:
            main_mod.main()
        assert h.upload.call_args.kwargs["overwrite"] is True

    def test_overwrite_defaults_off(self):
        with _Harness([]) as h:
            main_mod.main()
        assert h.upload.call_args.kwargs["overwrite"] is False

    def test_already_published_exits_nonzero(self):
        with _Harness([]) as h:
            h.upload.side_effect = AlreadyPublishedError("2026-01-02 is already published")
            with pytest.raises(SystemExit) as exc:
                main_mod.main()
        assert exc.value.code == 1


class TestPreflight:
    """The write-once check runs before the work, not after it.

    upload() still holds the authoritative check — it is what narrows the race
    window — but reaching it costs a fetch, a style transfer and a full variant
    encode. On a collision all of that is thrown away.
    """

    def test_preflight_runs_before_the_fetch(self):
        with _Harness([]) as h:
            h.assert_unpublished.side_effect = AlreadyPublishedError("2026-01-02 …")
            with pytest.raises(SystemExit):
                main_mod.main()
        assert h.fetch.call_count == 0
        assert h.upload.call_count == 0

    def test_preflight_uses_the_same_date_as_the_upload(self):
        """One date for both checks, or a run across midnight UTC publishes over itself."""
        with _Harness([]) as h:
            main_mod.main()
        assert h.assert_unpublished.call_args.args[0] == date(2026, 1, 2)
        assert h.upload.call_args.kwargs["today"] == date(2026, 1, 2)

    def test_overwrite_skips_the_preflight(self):
        """--overwrite means publish regardless; asking R2 first proves nothing."""
        with _Harness(["--overwrite"]) as h:
            main_mod.main()
        assert h.assert_unpublished.call_count == 0
        assert h.upload.call_count == 1

    def test_dry_run_never_touches_r2(self, tmp_path):
        """A dry run has no R2 credentials to preflight with."""
        with _Harness(["--dry-run"]) as h, patch.object(main_mod, "OUTPUT_DIR", tmp_path):
            main_mod.main()
        assert h.assert_unpublished.call_count == 0


class TestSkipIfPublished:
    """A scheduled run that finds the date published is a no-op, not a failure.

    The morning after publishing became write-once, an evening dispatch had
    already claimed the UTC date and the cron run went red — a red X and a
    high-priority page for a day whose art existed and was being served.
    """

    def test_skip_exits_zero_on_preflight_collision(self):
        with _Harness(["--skip-if-published"]) as h:
            h.assert_unpublished.side_effect = AlreadyPublishedError("2026-01-02 …")
            with pytest.raises(SystemExit) as exc:
                main_mod.main()
        assert exc.value.code == 0
        assert h.upload.call_count == 0

    def test_skip_exits_zero_when_upload_loses_the_race(self):
        """The preflight can pass and a concurrent generator still win."""
        with _Harness(["--skip-if-published"]) as h:
            h.upload.side_effect = AlreadyPublishedError("2026-01-02 …")
            with pytest.raises(SystemExit) as exc:
                main_mod.main()
        assert exc.value.code == 0

    def test_without_the_flag_a_collision_still_fails(self):
        """A manual dispatch asked for a publish; not getting one is an error."""
        with _Harness([]) as h:
            h.assert_unpublished.side_effect = AlreadyPublishedError("2026-01-02 …")
            with pytest.raises(SystemExit) as exc:
                main_mod.main()
        assert exc.value.code == 1

    def test_skip_does_not_suppress_an_unpublished_run(self):
        """The flag only changes the collision path — a free date still publishes."""
        with _Harness(["--skip-if-published"]) as h:
            main_mod.main()
        assert h.upload.call_count == 1

    def test_overwrite_and_skip_are_mutually_exclusive(self):
        """Contradictory intents: publish over it, versus stand down for it."""
        with _Harness(["--overwrite", "--skip-if-published"]):
            with pytest.raises(SystemExit) as exc:
                main_mod.main()
        assert exc.value.code == 2

    def test_signed_bytes_are_the_uploaded_bytes(self):
        """The signature must cover exactly the JSON that lands in R2."""
        with _Harness([], env={"GPG_KEY_ID": "ABC123"}) as h:
            main_mod.main()

        signed_text = h.sign.call_args.args[0]
        uploaded_metadata = h.upload.call_args.args[2]
        from upload import prepare_metadata_for_upload, serialize_metadata
        expected = serialize_metadata(
            prepare_metadata_for_upload(uploaded_metadata, today=date(2026, 1, 2)),
        ).decode()
        assert signed_text == expected
        assert h.upload.call_args.kwargs["metadata_sig"] == b"-----BEGIN PGP SIGNATURE-----"


class TestGpgGating:
    """Signing must engage for the configuration the README tells people to use."""

    def test_signs_with_private_key_alone(self):
        """The README's recipe yields a passphrase-less default key: no id, no passphrase.

        Gating on key id or passphrase meant following the README imported a
        key and then never signed with it.
        """
        with _Harness([], env={"GPG_PRIVATE_KEY": "-----BEGIN PGP PRIVATE KEY BLOCK-----"}) as h:
            main_mod.main()
        assert h.sign.call_count == 1
        assert h.sign.call_args.kwargs["key_id"] is None
        assert h.sign.call_args.kwargs["passphrase"] is None

    def test_signs_with_key_id(self):
        with _Harness([], env={"GPG_KEY_ID": "DEADBEEF"}) as h:
            main_mod.main()
        assert h.sign.call_count == 1
        assert h.sign.call_args.kwargs["key_id"] == "DEADBEEF"

    def test_signs_with_passphrase_only(self):
        with _Harness([], env={"GPG_PASSPHRASE": "hunter2"}) as h:
            main_mod.main()
        assert h.sign.call_count == 1

    def test_no_gpg_config_means_no_signature(self):
        with _Harness([]) as h:
            main_mod.main()
        assert h.sign.call_count == 0
        assert h.upload.call_args.kwargs["metadata_sig"] is None

    def test_failed_signing_does_not_abort_the_run(self):
        """sign_metadata() returns None on failure; publishing must continue."""
        with _Harness([], env={"GPG_KEY_ID": "DEADBEEF"}) as h:
            h.sign.return_value = None
            main_mod.main()
        assert h.upload.call_count == 1
        assert h.upload.call_args.kwargs["metadata_sig"] is None
