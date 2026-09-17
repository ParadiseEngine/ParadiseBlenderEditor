"""Per-clip root motion in the ``[glb]`` sidecar domain.

The flow these pin: an artist flags one clip of a rigged GLB and optionally names its root
bone; the choice lands in ``<model>.glb.meta`` as ``clips`` entries keyed by the clip's glTF
index, merged into whatever the watcher and pipeline already wrote there -- and a GLB nobody
touched keeps a byte-identical sidecar.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from paradise_assets.document import canonical_toml, glb_clips

GUID = "97291278-960a-59b4-993d-39bf82c47b29"

SIDECAR = f'schema_version = 1\nguid = "{GUID}"\n'

#: What the pipeline's own writer produces for a GLB whose texture uris were resolved: an
#: ``optimize`` inline table and a ``references`` array of inline tables, either of which a
#: clip-settings write must not touch or respell.
RICH_SIDECAR = (
    SIDECAR
    + '\n[glb]\n'
    + 'optimize = { tolerance = 0.001, distance = 0.1 }\n'
    + 'references = [ { slot = "images/0", uri = "tex.png", '
    + f'guid = "{GUID}", path = "textures/tex.png" }} ]\n'
    + '\n[texture]\nsrgb = true\n'
)

#: root -- hip -- foot, the hierarchy the auto-detect reads ``root`` out of.
NODES = [
    {"name": "root", "children": [1]},
    {"name": "hip", "children": [2]},
    {"name": "foot"},
]
SKIN = [{"joints": [0, 1, 2]}]


_MTIME = [1_700_000_000]


def write_glb(path: Path, clips=(), nodes=NODES, skins=SKIN) -> str:
    """The smallest GLB that answers ``rig()``: a JSON chunk naming clips and a skin."""
    document = {"asset": {"version": "2.0"}}
    if nodes:
        document["nodes"] = list(nodes)
    document["animations"] = [{"name": clip} for clip in clips]
    if skins:
        document["skins"] = list(skins)

    payload = json.dumps(document).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    blob = (
        struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(payload))
        + struct.pack("<I4s", len(payload), b"JSON")
        + payload
    )
    path.write_bytes(blob)
    # A rewrite can land on the same size and the same mtime tick as the file it replaced,
    # which is exactly the stamp the rig cache keys on -- stamp each write a second apart so
    # "the re-export arrived" is never ambiguous to it.
    _MTIME[0] += 1
    os.utime(path, ns=(_MTIME[0] * 10**9, _MTIME[0] * 10**9))
    return str(path)


@pytest.fixture(autouse=True)
def clean_caches():
    glb_clips._RIG_CACHE.clear()
    glb_clips._META_CACHE.clear()
    yield


@pytest.fixture
def rigged(tmp_path):
    """A rigged GLB with three clips and its sidecar; returns ``(glb_path, meta_path)``."""
    glb = write_glb(
        tmp_path / "player.glb",
        clips=("Idle_Loop", "Walk_Loop", "Sprint_Loop"),
    )
    meta = tmp_path / "player.glb.meta"
    meta.write_text(SIDECAR, encoding="utf-8")
    return glb, str(meta)


def read_back(meta_path: str) -> dict:
    with open(meta_path, encoding="utf-8") as handle:
        return canonical_toml.loads(handle.read())


class TestRig:
    def test_clips_and_joints_come_off_the_json_chunk(self, rigged):
        info = glb_clips.rig(rigged[0])
        assert info.clips == ("Idle_Loop", "Walk_Loop", "Sprint_Loop")
        assert info.joints == ("root", "hip", "foot")

    def test_the_root_joint_is_the_one_no_joint_parents(self, tmp_path):
        # joints[0] is the hierarchy root on an exporter-written rig; the parent walk is what
        # finds it when a hand-made file orders the list differently.
        glb = write_glb(
            tmp_path / "rig.glb",
            clips=("Run",),
            nodes=[{"name": "hip"}, {"name": "root", "children": [0]}],
            skins=[{"joints": [0, 1]}],
        )
        assert glb_clips.rig(glb).root_joint == "root"

    def test_an_unreadable_or_static_file_has_no_clips(self, tmp_path):
        assert glb_clips.rig(str(tmp_path / "none.glb")) is None
        static = write_glb(tmp_path / "crate.glb", nodes=[], skins=[])
        assert glb_clips.rig(static).clips == ()


class TestWriting:
    def test_flagging_a_clip_writes_one_indexed_entry(self, rigged):
        glb, meta = rigged

        glb_clips.set_root_motion(glb, 1, True)

        assert glb_clips.read_settings(meta) == {
            1: glb_clips.ClipSetting(1, "Walk_Loop", root_motion=True)
        }
        domain = read_back(meta)["glb"]
        assert domain["clips"] == [
            {"index": 1, "name": "Walk_Loop", "root_motion": True}
        ]

    def test_the_flag_round_trips_back_to_byte_identical(self, rigged):
        glb, meta = rigged
        before = Path(meta).read_text(encoding="utf-8")

        glb_clips.set_root_motion(glb, 0, True)
        glb_clips.set_root_motion(glb, 0, False)

        # Off is the default, so a clip with nothing left to say leaves no entry -- and with
        # the entry goes the empty domain, leaving the file exactly as it was.
        assert Path(meta).read_text(encoding="utf-8") == before

    def test_a_root_bone_override_is_written_and_cleared(self, rigged):
        glb, meta = rigged

        glb_clips.set_root_bone(glb, 2, "hip")
        assert read_back(meta)["glb"]["clips"] == [
            {"index": 2, "name": "Sprint_Loop", "root_motion": False, "root_bone": "hip"}
        ]

        glb_clips.set_root_bone(glb, 2, "")
        assert "glb" not in read_back(meta)

    def test_an_unknown_bone_is_refused_naming_clip_and_bone(self, rigged):
        glb, meta = rigged
        before = Path(meta).read_text(encoding="utf-8")

        with pytest.raises(glb_clips.ClipSettingsError) as error:
            glb_clips.set_root_bone(glb, 1, "tail")

        assert "Walk_Loop" in str(error.value) and "tail" in str(error.value)
        assert Path(meta).read_text(encoding="utf-8") == before

    def test_a_clip_index_past_the_table_is_refused(self, rigged):
        with pytest.raises(glb_clips.ClipSettingsError, match="no clip 9"):
            glb_clips.set_root_motion(rigged[0], 9, True)

    def test_other_domains_and_glb_members_survive(self, tmp_path):
        glb = write_glb(tmp_path / "player.glb", clips=("Idle",))
        meta = tmp_path / "player.glb.meta"
        meta.write_text(RICH_SIDECAR, encoding="utf-8")

        glb_clips.set_root_motion(glb, 0, True)

        text = Path(meta).read_text(encoding="utf-8")
        # The members this edit did not touch keep the spelling the pipeline wrote them in:
        # optimize inline, references an inline-table array, the texture domain untouched.
        assert "optimize = { tolerance = 0.001, distance = 0.1 }" in text
        assert '"images/0"' in text and "tex.png" in text
        assert "[texture]" in text and "srgb = true" in text
        assert "root_motion = true" in text

    def test_no_identity_means_no_write(self, tmp_path):
        glb = write_glb(tmp_path / "new.glb", clips=("Run",))
        with pytest.raises(glb_clips.ClipSettingsError, match="no identity"):
            glb_clips.set_root_motion(glb, 0, True)
        assert not (tmp_path / "new.glb.meta").exists()

    def test_a_newer_schema_version_is_refused_not_rewritten(self, rigged):
        # sidecar.read already refuses a declared schema_version it cannot parse;
        # the writer must not touch a file the rest of the addon treats as unreadable.
        glb, meta = rigged
        before = f'schema_version = 2\nguid = "{GUID}"\n'
        Path(meta).write_text(before, encoding="utf-8")

        with pytest.raises(glb_clips.ClipSettingsError, match="schema_version 2"):
            glb_clips.set_root_motion(glb, 0, True)
        assert Path(meta).read_text(encoding="utf-8") == before

    def test_an_unparseable_sidecar_is_refused_not_rewritten(self, rigged):
        glb, meta = rigged
        Path(meta).write_text("guid = [not toml", encoding="utf-8")

        with pytest.raises(glb_clips.ClipSettingsError):
            glb_clips.set_root_motion(glb, 0, True)
        assert Path(meta).read_text(encoding="utf-8") == "guid = [not toml"


class TestReconcile:
    def test_a_renamed_clip_keeps_its_flag_under_the_new_name(self, rigged):
        glb, meta = rigged
        glb_clips.set_root_motion(glb, 1, True)

        # Re-export with a rename at the same index (also a different size, so the stamp
        # cache sees the file).
        write_glb(
            Path(glb),
            clips=("Idle_Loop", "Jog_Fwd_Loop", "Sprint_Loop"),
        )

        view = glb_clips.view(glb)
        assert view.clips[1].setting.root_motion is True
        assert any("Walk_Loop" in problem for problem in view.clips[1].problems)

        glb_clips.set_root_motion(glb, 0, True)
        assert glb_clips.read_settings(meta)[1].name == "Jog_Fwd_Loop"

    def test_a_reordered_clip_carries_its_flag_to_the_new_index(self, rigged):
        glb, _meta = rigged
        glb_clips.set_root_motion(glb, 1, True)

        # The same clips in a different order: Walk_Loop slid from index 1 to index 0.
        write_glb(Path(glb), clips=("Walk_Loop", "Idle_Loop", "Sprint_Loop"))

        view = glb_clips.view(glb)
        assert view.clips[0].setting.root_motion is True
        assert view.clips[1].setting.root_motion is False

    def test_a_removed_clip_orphans_its_setting_until_a_write_drops_it(self, rigged):
        glb, meta = rigged
        glb_clips.set_root_motion(glb, 2, True)

        write_glb(Path(glb), clips=("Idle_Loop",))

        view = glb_clips.view(glb)
        assert len(view.clips) == 1
        assert any("stale" in problem and "Sprint_Loop" in problem for problem in view.problems)

        # Whatever slid into the slot does not inherit the removed clip's flag.
        assert view.clips[0].setting.root_motion is False

        glb_clips.set_root_motion(glb, 0, True)
        settings = glb_clips.read_settings(meta)
        assert sorted(settings) == [0]

    def test_a_rekeyed_setting_displacing_another_reports_the_loser(self, rigged):
        # Stored {index=0,name="Idle_Loop"} + {index=1,name="Walk_Loop"}; the GLB comes
        # back as ("Walk_Loop", "New_Clip") — Walk_Loop re-keys onto index 0 and Idle's
        # entry loses its slot. The loser must surface as a problem, not vanish.
        glb, _meta = rigged
        glb_clips.set_root_motion(glb, 0, True)
        glb_clips.set_root_motion(glb, 1, True)

        write_glb(Path(glb), clips=("Walk_Loop", "New_Clip"))

        view = glb_clips.view(glb)
        assert view.clips[0].setting.root_motion is True  # Walk_Loop followed its name
        assert any("stale" in p and "Idle_Loop" in p for p in view.problems)


class TestView:
    def test_no_clips_means_no_section(self, tmp_path):
        static = write_glb(tmp_path / "crate.glb", nodes=[], skins=[])
        assert glb_clips.view(static) is None

    def test_a_flagged_clip_on_a_skinless_glb_warns(self, tmp_path):
        glb = write_glb(tmp_path / "floaty.glb", clips=("Drift",), skins=[])
        meta = tmp_path / "floaty.glb.meta"
        meta.write_text(SIDECAR, encoding="utf-8")
        glb_clips.set_root_motion(glb, 0, True)

        view = glb_clips.view(glb)
        assert any("no skin" in problem for problem in view.problems)

    def test_a_stored_bone_the_rig_no_longer_has_warns_naming_both(self, rigged):
        glb, meta = rigged
        with open(meta, "a", encoding="utf-8") as handle:
            handle.write(
                '\n[glb]\nclips = [ { index = 0, name = "Idle_Loop", '
                'root_motion = true, root_bone = "ponytail" } ]\n'
            )

        problems = glb_clips.view(glb).problems
        assert any("Idle_Loop" in p and "ponytail" in p for p in problems)

    def test_unidentified_glb_reports_it(self, tmp_path):
        glb = write_glb(tmp_path / "fresh.glb", clips=("Run",))
        assert glb_clips.view(glb).identified is False

    def test_a_newer_schema_version_reports_unidentified(self, tmp_path):
        glb = write_glb(tmp_path / "fresh.glb", clips=("Run",))
        meta = tmp_path / "fresh.glb.meta"
        meta.write_text(f'schema_version = 2\nguid = "{GUID}"\n', encoding="utf-8")
        assert glb_clips.view(glb).identified is False

    def test_a_missing_schema_version_still_identifies(self, tmp_path):
        # Older mints omit the key entirely; sidecar.read accepts them and so does this.
        glb = write_glb(tmp_path / "fresh.glb", clips=("Run",))
        meta = tmp_path / "fresh.glb.meta"
        meta.write_text(f'guid = "{GUID}"\n', encoding="utf-8")
        assert glb_clips.view(glb).identified is True
