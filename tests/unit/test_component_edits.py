"""Tests for the component-edit overlay.

**What is actually under test is a promise about what does NOT change.** The save path takes
payloads from the re-read document so a component this addon has never heard of round-trips
verbatim, and adding an editor is the obvious way to break that. So most of these assert
absence: fields nobody touched keep the file's value, components nobody edited are untouched,
and a component the document dropped does not get invented out of a partial payload.

A plain ``dict`` stands in for the Blender object. That is not a mock of an interface -- it IS
the interface the overlay uses (``get``, ``in``, ``__setitem__``, ``__delitem__``), which is why
:mod:`paradise_assets.edits` imports no ``bpy``.
"""

from __future__ import annotations

from paradise_assets import edits


class FakeComponent:
    def __init__(self, component_id: str, data: dict) -> None:
        self.id = component_id
        self.data = data


class FakeEntry:
    """A document object, with the one method the overlay calls."""

    def __init__(self, components) -> None:
        self.components = list(components)

    def component(self, component_id: str):
        for component in self.components:
            if component.id == component_id:
                return component
        return None


def test_an_object_with_no_edits_reads_empty():
    assert edits.read({}) == {}
    assert edits.count({}) == 0


def test_set_then_read_round_trips():
    obj: dict = {}

    edits.set_field(obj, "comp-a", "Speed", 4.5)
    edits.set_field(obj, "comp-a", "Name", "fast")
    edits.set_field(obj, "comp-b", "Enabled", True)

    assert edits.read(obj) == {
        "comp-a": {"Speed": 4.5, "Name": "fast"},
        "comp-b": {"Enabled": True},
    }
    assert edits.count(obj) == 3
    assert edits.edited_fields(obj, "comp-a") == {"Speed": 4.5, "Name": "fast"}


def test_clearing_removes_the_key_entirely():
    # Not merely emptied: an object with no pending edits should carry no property at all, or
    # every object that was ever edited keeps a marker saying it was.
    obj: dict = {}
    edits.set_field(obj, "comp-a", "Speed", 1.0)
    edits.clear(obj, "comp-a", "Speed")

    assert edits.read(obj) == {}
    assert edits.EDITS_KEY not in obj


def test_clearing_one_field_leaves_the_others():
    obj: dict = {}
    edits.set_field(obj, "comp-a", "Speed", 1.0)
    edits.set_field(obj, "comp-a", "Name", "x")

    edits.clear(obj, "comp-a", "Speed")

    assert edits.read(obj) == {"comp-a": {"Name": "x"}}


def test_clearing_the_object_discards_everything():
    obj: dict = {}
    edits.set_field(obj, "comp-a", "Speed", 1.0)
    edits.set_field(obj, "comp-b", "Name", "x")

    edits.clear(obj)

    assert edits.read(obj) == {}


def test_apply_writes_only_the_edited_members():
    # THE property the whole design exists for. The payload has three members and one was edited;
    # the other two must come out exactly as the document had them, including the one this addon
    # has no schema for and could not have displayed.
    entry = FakeEntry([
        FakeComponent("comp-a", {"Speed": 1.0, "Table": "loot_a", "Mystery": [1, 2, 3]}),
    ])

    written = edits.apply_to(entry, {"comp-a": {"Speed": 9.5}})

    assert written == 1
    assert entry.component("comp-a").data == {
        "Speed": 9.5, "Table": "loot_a", "Mystery": [1, 2, 3],
    }


def test_apply_leaves_unedited_components_alone():
    entry = FakeEntry([
        FakeComponent("comp-a", {"Speed": 1.0}),
        FakeComponent("comp-b", {"Untouched": True}),
    ])

    edits.apply_to(entry, {"comp-a": {"Speed": 2.0}})

    assert entry.component("comp-b").data == {"Untouched": True}


def test_apply_skips_a_component_the_document_no_longer_carries():
    # The document moved on under the edit. Creating the component would produce one holding
    # ONLY the edited member -- missing every other field the game expects -- which loads and
    # then behaves as though half its configuration were absent. Skipping is the honest answer,
    # and the save path reports it as a dropped edit rather than passing over it.
    entry = FakeEntry([FakeComponent("comp-a", {"Speed": 1.0})])

    written = edits.apply_to(entry, {"comp-gone": {"Speed": 2.0}})

    assert written == 0
    assert len(entry.components) == 1


def test_a_corrupt_overlay_reads_as_empty_rather_than_throwing():
    # Hand-edited, or written by an older addon. Losing pending edits is the lesser harm: the
    # alternative is a save that cannot proceed at all, which strands the whole scene.
    assert edits.read({edits.EDITS_KEY: "{ not json"}) == {}
    assert edits.read({edits.EDITS_KEY: "[1, 2, 3]"}) == {}
    assert edits.read({edits.EDITS_KEY: 42}) == {}


def test_values_keep_their_type_through_the_overlay():
    # The reason the overlay is a JSON string rather than Blender ID properties: those normalize
    # an int to a float and a tuple to a list on the way through, and these values are written
    # into a document whose numbers are a cross-language contract.
    obj: dict = {}
    edits.set_field(obj, "c", "Count", 3)
    edits.set_field(obj, "c", "Ratio", 0.5)
    edits.set_field(obj, "c", "Flag", False)

    restored = edits.read(obj)["c"]

    assert isinstance(restored["Count"], int) and not isinstance(restored["Count"], bool)
    assert isinstance(restored["Ratio"], float)
    assert restored["Flag"] is False
