"""Identity text, mirroring C# ``DocumentGuid``: what is accepted, and that every accepted
spelling comes out canonical."""

from __future__ import annotations

import pytest

from paradise_assets.document import guid

CANONICAL = "aaaaaaaa-0000-4000-8000-000000000002"


class TestParse:
    def test_canonical(self):
        assert str(guid.parse(CANONICAL)) == CANONICAL

    def test_uppercase_and_undashed_are_the_same_value(self):
        assert guid.canonical("AAAAAAAA-0000-4000-8000-000000000002") == CANONICAL
        assert guid.canonical("AAAAAAAA000040008000000000000002") == CANONICAL

    @pytest.mark.parametrize("text", [
        "{aaaaaaaa-0000-4000-8000-000000000002}",   # braced: .NET accepts, the format does not
        "urn:uuid:aaaaaaaa-0000-4000-8000-000000000002",
        "aaaaaaaa-0000-4000-8000-00000000000",      # 35 characters
        "aaaaaaaa-0000-4000-8000-0000000000zz",
        "aaaaaaaa0000-4000-8000-000000000002x",     # 36 characters, dashes misplaced
        "",
        None,
        42,
    ])
    def test_other_spellings_are_refused(self, text):
        assert guid.parse(text) is None
        assert not guid.is_text(text)

    def test_the_empty_guid_is_a_value_but_not_text(self):
        # C# reads it and then refuses `Guid.Empty` at every identity site; is_text folds that in.
        assert guid.parse("00000000-0000-0000-0000-000000000000") is not None
        assert not guid.is_text("00000000-0000-0000-0000-000000000000")

    def test_canonical_raises_on_junk(self):
        with pytest.raises(ValueError):
            guid.canonical("not-a-guid")
