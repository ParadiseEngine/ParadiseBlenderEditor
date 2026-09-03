"""The cross-language byte contract, pinned by files rather than by vectors typed twice.

Every file under ``tests/fixtures/parity`` was written by the C# ``CanonicalTomlWriter`` and is
re-read and re-emitted byte-for-byte by ``ParityCorpusTests`` in the engine repo; this does the
same over the same bytes. A divergence here is the two writers disagreeing about a document,
which ``prefab-check`` would otherwise report on the first save (#29, ParadiseEngine#209).
"""

from __future__ import annotations

import glob
import os

import pytest

from paradise_assets.document import canonical_toml as ct
from paradise_assets.document import prefab

CORPUS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "parity")


def toml_fixtures() -> list[str]:
    return sorted(glob.glob(os.path.join(CORPUS, "*.toml")))


def test_the_corpus_is_present():
    assert len(toml_fixtures()) >= 3


@pytest.mark.parametrize("path", toml_fixtures(), ids=os.path.basename)
def test_a_canonical_toml_fixture_is_a_fixed_point_of_read_then_write(path):
    with open(path, "rb") as handle:
        original = handle.read()

    assert ct.dump_bytes(ct.loads(original.decode("utf-8"))) == original


def test_the_prefab_fixture_is_a_fixed_point_of_read_then_write():
    path = os.path.join(CORPUS, "prefab.prefab")
    with open(path, encoding="utf-8", newline="") as handle:
        original = handle.read()

    document = prefab.loads(original, "prefab.prefab")

    assert prefab.dumps(document) == original


def test_the_prefab_fixture_reads_what_the_csharp_reader_reads():
    """The values the corpus README says it exercises, so a fixed point cannot be a coincidence
    of two wrong readers agreeing."""
    path = os.path.join(CORPUS, "prefab.prefab")
    with open(path, encoding="utf-8") as handle:
        document = prefab.loads(handle.read(), "prefab.prefab")

    names = [o.name for o in document.objects]
    assert names[1] == ""                      # an empty name is a name, not an absence
    assert document.objects[3].target is not None and document.objects[3].dropped
    shapes = document.objects[0].components[4].data["Shapes"]
    assert all(isinstance(row, ct.InlineTable) for row in shapes)
    assert document.objects[2].prefab.path == "prefabs/box.prefab"
