"""Tests for the export artifact cache.

The cache exists to skip work, so its failure mode is not an exception -- it is serving the wrong
bytes for a key, or dirtying a repo, or leaving a half-written entry behind after a crash. These
pin those three.
"""

from __future__ import annotations

import os

from paradise_blender.paths import ExportPaths
from paradise_blender.pipeline import cache as cache_module
from paradise_blender.pipeline.cache import ArtifactCache, artifact_cache, digest


class TestDigest:
    def test_stable_across_calls(self):
        assert digest("a", b"b") == digest("a", b"b")

    def test_str_and_bytes_are_the_same_input(self):
        assert digest("abc") == digest(b"abc")

    def test_part_boundaries_are_significant(self):
        """The real key is (image bytes, encode command line). Concatenating the parts would let
        a shifted boundary between them collide two different encodes onto one entry."""
        assert digest("ab", "c") != digest("a", "bc")

    def test_differs_on_any_part(self):
        assert digest("image", "--format srgb") != digest("image", "--format linear")


def _write(path: str, content: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class TestFetchAndStore:
    def test_round_trip(self, tmp_path):
        cache = ArtifactCache(str(tmp_path / "cache"))
        source = _write(str(tmp_path / "out" / "texture.ktx2"), b"encoded")
        cache.store("ktx2", "key", source)

        destination = str(tmp_path / "other" / "elsewhere.ktx2")
        assert cache.fetch("ktx2", "key", destination) is True
        with open(destination, "rb") as handle:
            assert handle.read() == b"encoded"

    def test_miss_leaves_destination_alone(self, tmp_path):
        cache = ArtifactCache(str(tmp_path / "cache"))
        destination = str(tmp_path / "out.ktx2")
        assert cache.fetch("ktx2", "absent", destination) is False
        assert not os.path.exists(destination)

    def test_kinds_do_not_collide(self, tmp_path):
        cache = ArtifactCache(str(tmp_path / "cache"))
        cache.store("ktx2", "key", _write(str(tmp_path / "a.ktx2"), b"texture"))
        assert cache.fetch("navmesh", "key", str(tmp_path / "b.bin")) is False

    def test_entry_keeps_the_destination_extension(self, tmp_path):
        """Entries are named <key><ext> so a cache directory stays readable when something looks
        wrong in it -- the alternative is a wall of undifferentiated hashes."""
        cache = ArtifactCache(str(tmp_path / "cache"))
        cache.store("navmesh", "abc", _write(str(tmp_path / "scene.navmesh.bin"), b"recast"))
        assert os.path.exists(tmp_path / "cache" / "navmesh" / "abc.bin")

    def test_store_leaves_no_partial_files(self, tmp_path):
        cache = ArtifactCache(str(tmp_path / "cache"))
        cache.store("ktx2", "key", _write(str(tmp_path / "a.ktx2"), b"texture"))
        entries = os.listdir(tmp_path / "cache" / "ktx2")
        assert entries == ["key.ktx2"]

    def test_storing_a_missing_source_is_a_no_op(self, tmp_path):
        cache = ArtifactCache(str(tmp_path / "cache"))
        cache.store("ktx2", "key", str(tmp_path / "never-written.ktx2"))
        assert cache.fetch("ktx2", "key", str(tmp_path / "out.ktx2")) is False


class TestDisabled:
    def test_every_operation_no_ops(self, tmp_path):
        cache = ArtifactCache(None)
        assert cache.enabled is False
        cache.store("ktx2", "key", _write(str(tmp_path / "a.ktx2"), b"texture"))
        assert cache.fetch("ktx2", "key", str(tmp_path / "b.ktx2")) is False


class TestSelfIgnore:
    def test_cache_directory_ignores_itself(self, tmp_path):
        """The cache lives inside a checkout; without this every project consuming the addon
        would need its own .gitignore rule, and the first one to forget commits the cache."""
        cache = ArtifactCache(str(tmp_path / "cache"))
        cache.store("ktx2", "key", _write(str(tmp_path / "a.ktx2"), b"texture"))

        with open(tmp_path / "cache" / ".gitignore", encoding="utf-8") as handle:
            assert handle.read().strip() == "*"


class TestLocation:
    def test_defaults_beside_the_data_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv(cache_module.LOCATION_ENV, raising=False)
        paths = ExportPaths(str(tmp_path / "project" / "data"))
        cache = artifact_cache(paths)
        assert cache.root == str(tmp_path / "project" / cache_module.DIRECTORY_NAME)

    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv(cache_module.LOCATION_ENV, str(tmp_path / "elsewhere"))
        cache = artifact_cache(ExportPaths(str(tmp_path / "project" / "data")))
        assert cache.root == str(tmp_path / "elsewhere")

    def test_env_can_disable(self, tmp_path, monkeypatch):
        monkeypatch.setenv(cache_module.LOCATION_ENV, "0")
        cache = artifact_cache(ExportPaths(str(tmp_path / "project" / "data")))
        assert cache.enabled is False
