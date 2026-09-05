"""Creating a new ``*.prefab``, which is not finished until the watcher has identified it.

The addon writes documents and ``paradise assets watch`` writes identities, so ``create`` writes
the file and then blocks on the sidecar. The ``watcher`` fixture (``conftest.py``) plays that
part; without one running, a creation is expected to fail rather than to invent a guid.
"""

from __future__ import annotations

import os

import pytest

from paradise_assets.document import new_prefab, prefab, sidecar, well_known
from paradise_assets.document.project import ProjectLayout

GUID = "f1e65335-6f87-59a3-9cce-a7bf2622d3fe"

EXPECTED = (
    "schema_version = 1\n"
    "\n[[objects]]\n"
    "\n[[objects.components]]\n"
    f'id = "{well_known.META_ID}"\n'
    'type = "meta"\n'
    f'Guid = "{GUID}"\n'
    'Name = "Box"\n'
    "\n[[objects.components]]\n"
    f'id = "{well_known.TRANSFORM_ID}"\n'
    'type = "transform"\n'
    "Position = [0.0, 0.0, 0.0]\n"
    "Rotation = [0.0, 0.0, 0.0, 1.0]\n"
    "Scale = [1.0, 1.0, 1.0]\n"
)


def project(tmp_path) -> ProjectLayout:
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "project.toml").write_text('name = "test"\n', encoding="utf-8")
    return ProjectLayout(str(tmp_path))


class TestRootOnly:
    def test_a_root_only_document_is_the_shape_a_real_prefab_has(self):
        assert prefab.dumps(new_prefab.root_only("Box", GUID)) == EXPECTED

    def test_it_validates_as_a_document(self):
        new_prefab.root_only("Box").validate("x.prefab")

    def test_each_one_gets_its_own_object_identity(self):
        assert new_prefab.root_only("Box").root_guid != new_prefab.root_only("Box").root_guid

    def test_extra_meta_fields_ride_along(self):
        # How the mirror's GeneratedFrom marker gets onto a generated prefab's root.
        document = new_prefab.root_only("Box", GUID, meta={"GeneratedFrom": GUID})

        assert document.root().meta.data["GeneratedFrom"] == GUID


class TestCreate:
    def test_the_document_is_written_and_the_watcher_s_identity_comes_back(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)
        path = os.path.join(layout.assets, "prefabs", "box.prefab")

        reference = new_prefab.create(path, layout, new_prefab.root_only("Box", GUID))

        assert reference.path == "prefabs/box.prefab"
        assert reference.guid == sidecar.read(sidecar.path_for(path)).guid
        with open(path, encoding="utf-8") as handle:
            assert handle.read() == EXPECTED

    def test_with_no_watcher_it_refuses_rather_than_inventing_an_identity(self, tmp_path):
        layout = project(tmp_path)
        path = os.path.join(layout.assets, "box.prefab")

        with pytest.raises(new_prefab.CreateError, match=r"no \.meta appeared"):
            new_prefab.create(path, layout, new_prefab.root_only("Box"), timeout=0.2)

        # The document stays: it is valid content, and the watcher will identify it when one
        # runs. Deleting the author's new prefab would be the worse failure.
        assert os.path.isfile(path)

    def test_a_path_outside_assets_is_refused(self, tmp_path):
        layout = project(tmp_path)

        with pytest.raises(new_prefab.CreateError, match="outside"):
            new_prefab.create(str(tmp_path / "box.prefab"), layout, new_prefab.root_only("Box"))

    def test_an_existing_document_is_refused(self, tmp_path, watcher):
        layout = project(tmp_path)
        watcher(layout.root)
        path = os.path.join(layout.assets, "box.prefab")
        new_prefab.create(path, layout, new_prefab.root_only("Box", GUID))

        with pytest.raises(new_prefab.CreateError, match="already exists"):
            new_prefab.create(path, layout, new_prefab.root_only("Other"))

        with open(path, encoding="utf-8") as handle:
            assert handle.read() == EXPECTED

    def test_a_stray_sidecar_is_refused_rather_than_taken_over(self, tmp_path):
        layout = project(tmp_path)
        path = os.path.join(layout.assets, "box.prefab")
        os.makedirs(layout.assets, exist_ok=True)
        with open(sidecar.path_for(path), "w", encoding="utf-8") as handle:
            handle.write(f'schema_version = 1\nguid = "{GUID}"\n')

        with pytest.raises(new_prefab.CreateError, match="stray sidecar"):
            new_prefab.create(path, layout, new_prefab.root_only("Box"))

    def test_a_multi_root_document_is_refused_before_anything_is_written(self, tmp_path):
        layout = project(tmp_path)
        path = os.path.join(layout.assets, "box.prefab")
        document = new_prefab.root_only("Box")
        document.objects.append(new_prefab.root_only("Second").objects[0])

        with pytest.raises(prefab.PrefabDocumentError):
            new_prefab.create(path, layout, document)

        assert not os.path.exists(path)
