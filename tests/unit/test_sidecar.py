"""Reading ``<asset>.meta``, and waiting for one to appear.

The addon mints no identities -- ``paradise assets watch`` does -- so what is tested here is the
reading half, its two deliberate divergences from C# ``SidecarMeta.Parse``, and the wait a
creation depends on.
"""

from __future__ import annotations

import threading

from paradise_assets.document import sidecar

GUID = "97291278-960a-59b4-993d-39bf82c47b29"

#: Byte-for-byte what `assets/prefabs/box.prefab.meta` holds in the ShiningPie checkout.
REAL_SIDECAR = 'schema_version = 1\nguid = "97291278-960a-59b4-993d-39bf82c47b29"\n'


def write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestRead:
    def test_a_real_sidecar_reads(self, tmp_path):
        assert sidecar.read(write(tmp_path, "x.meta", REAL_SIDECAR)).guid == GUID

    def test_an_uppercase_guid_comes_back_canonical(self, tmp_path):
        text = f'schema_version = 1\nguid = "{GUID.upper()}"\n'

        assert sidecar.read(write(tmp_path, "x.meta", text)).guid == GUID

    def test_settings_domains_are_kept_as_opaque_tables(self, tmp_path):
        text = REAL_SIDECAR + '\n[texture]\nsrgb = true\n'

        assert sidecar.read(write(tmp_path, "x.meta", text)).setting("texture") == {"srgb": True}

    def test_a_missing_file_reads_as_no_identity(self, tmp_path):
        assert sidecar.read(str(tmp_path / "nothing.meta")) is None

    def test_an_empty_guid_is_not_an_identity(self, tmp_path):
        text = 'schema_version = 1\nguid = "00000000-0000-0000-0000-000000000000"\n'

        assert sidecar.read(write(tmp_path, "x.meta", text)) is None

    def test_a_half_written_sidecar_reads_as_not_yet(self, tmp_path):
        # What `wait_for` waits through: the watcher is another process, so a truncated file is
        # a moment in time rather than an error.
        assert sidecar.read(write(tmp_path, "x.meta", 'schema_version = 1\nguid = "97291')) is None


class TestDivergencesFromCSharp:
    def test_a_sidecar_without_a_schema_version_still_carries_its_identity(self, tmp_path):
        # C# requires the key. Older mints and hand-written fixtures omit it, and the identity
        # in them is not in doubt.
        assert sidecar.read(write(tmp_path, "x.meta", f'guid = "{GUID}"\n')).guid == GUID

    def test_a_stray_scalar_at_the_root_does_not_cost_the_asset_its_identity(self, tmp_path):
        # C# refuses this, because it rewrites sidecars and would drop the key. Refusing here
        # would make the asset invisible to the catalogue, to references and to the mirror.
        text = f'schema_version = 1\nguid = "{GUID}"\nkind = "document"\n'

        meta = sidecar.read(write(tmp_path, "x.meta", text))

        assert meta.guid == GUID
        assert meta.settings == {}

    def test_a_declared_version_this_build_cannot_read_is_still_refused(self, tmp_path):
        assert sidecar.read(write(tmp_path, "x.meta", f'schema_version = 2\nguid = "{GUID}"\n')) is None


class TestWaitFor:
    def test_an_identity_already_there_comes_back_at_once(self, tmp_path):
        asset = tmp_path / "box.prefab"
        asset.write_text("schema_version = 1\n", encoding="utf-8")
        write(tmp_path, "box.prefab.meta", REAL_SIDECAR)

        assert sidecar.wait_for(str(asset), timeout=0.5).guid == GUID

    def test_it_waits_through_a_watcher_that_has_not_got_there_yet(self, tmp_path):
        asset = tmp_path / "box.prefab"
        asset.write_text("schema_version = 1\n", encoding="utf-8")
        timer = threading.Timer(0.15, lambda: write(tmp_path, "box.prefab.meta", REAL_SIDECAR))
        timer.start()
        try:
            assert sidecar.wait_for(str(asset), timeout=5.0, interval=0.02).guid == GUID
        finally:
            timer.cancel()

    def test_no_watcher_means_no_identity_rather_than_a_hang(self, tmp_path):
        asset = tmp_path / "box.prefab"
        asset.write_text("schema_version = 1\n", encoding="utf-8")

        assert sidecar.wait_for(str(asset), timeout=0.1, interval=0.02) is None


def test_path_for_appends_the_suffix():
    assert sidecar.path_for("/a/b/box.prefab") == "/a/b/box.prefab.meta"


def test_the_module_offers_no_way_to_mint_one():
    # Two minters race, and the loser's guid is dropped with a `Conflicted` log line. The rule
    # is enforced by there being no function to call.
    assert not hasattr(sidecar, "write")
    assert not hasattr(sidecar, "mint")
