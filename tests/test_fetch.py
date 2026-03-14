"""Tests for fetch.py — title filtering and Artwork dataclass."""

from fetch import Artwork, is_safe_title, is_preferred_subject, is_landscape


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
    def _make_artwork(self) -> Artwork:
        return Artwork(
            title="Wheat Field with Cypresses",
            artist="Vincent van Gogh",
            date="1889",
            source="met",
            source_url="https://www.metmuseum.org/art/collection/search/436535",
            image_bytes=b"\xff\xd8\xff\xe0fake-jpeg",
        )

    def test_returns_dict(self):
        meta = self._make_artwork().to_metadata()
        assert isinstance(meta, dict)

    def test_excludes_image_bytes(self):
        meta = self._make_artwork().to_metadata()
        assert "image_bytes" not in meta

    def test_includes_license(self):
        meta = self._make_artwork().to_metadata()
        assert meta["license"] == "CC0-1.0"

    def test_preserves_fields(self):
        meta = self._make_artwork().to_metadata()
        assert meta["title"] == "Wheat Field with Cypresses"
        assert meta["artist"] == "Vincent van Gogh"
        assert meta["date"] == "1889"
        assert meta["source"] == "met"
        assert meta["source_url"] == "https://www.metmuseum.org/art/collection/search/436535"
        assert meta["content_type"] == "image/jpeg"
