"""Telling a rigged model from a static one by reading the GLB's JSON chunk.

The mirror has to pick a component, and a skinned mesh is a different component from a static
one in any game that declares both. The distinction is in the model, not in the schema.
"""

from __future__ import annotations

import json
import struct

from paradise_assets.document import gltf


def glb(document: dict, binary: bytes = b"", magic: bytes = b"glTF") -> bytes:
    """A GLB container around ``document``, built the way the spec describes it."""
    payload = json.dumps(document).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    chunks = struct.pack("<I4s", len(payload), b"JSON") + payload
    if binary:
        binary += b"\x00" * (-len(binary) % 4)
        chunks += struct.pack("<I4s", len(binary), b"BIN\x00") + binary
    return struct.pack("<4sII", magic, 2, 12 + len(chunks)) + chunks


def write(tmp_path, name: str, data: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_a_model_with_a_skin_is_rigged(tmp_path):
    path = write(tmp_path, "hero.glb", glb({"skins": [{"joints": [0, 1]}], "meshes": [{}]}))

    assert gltf.has_skin(path) is True


def test_a_model_without_one_is_not(tmp_path):
    assert gltf.has_skin(write(tmp_path, "box.glb", glb({"meshes": [{}]}))) is False


def test_an_empty_skins_array_is_not_a_rig(tmp_path):
    assert gltf.has_skin(write(tmp_path, "box.glb", glb({"skins": []}))) is False


def test_the_json_chunk_is_found_after_another_chunk(tmp_path):
    # The spec puts JSON first, but a reader that assumed position would return nothing for a
    # legal file that does not.
    payload = json.dumps({"skins": [{}]}).encode("utf-8")
    other = struct.pack("<I4s", 4, b"BIN\x00") + b"\x00\x00\x00\x00"
    chunks = other + struct.pack("<I4s", len(payload), b"JSON") + payload
    data = struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks

    assert gltf.has_skin(write(tmp_path, "odd.glb", data)) is True


def test_the_binary_chunk_is_never_read(tmp_path):
    # What keeps this cheap enough to run on every poll of a whole project.
    path = write(tmp_path, "big.glb", glb({"meshes": [{}]}, binary=b"\xff" * 4096))

    assert gltf.has_skin(path) is False


class TestUnreadable:
    def test_a_file_that_is_not_a_glb_is_static(self, tmp_path):
        # Never "rigged": that would author a component the game may not even declare.
        assert gltf.has_skin(write(tmp_path, "x.glb", b"not a model at all")) is False

    def test_a_truncated_header_is_static(self, tmp_path):
        assert gltf.has_skin(write(tmp_path, "x.glb", b"glTF")) is False

    def test_a_wrong_magic_is_static(self, tmp_path):
        assert gltf.has_skin(write(tmp_path, "x.glb", glb({"skins": [{}]}, magic=b"XXXX"))) is False

    def test_a_missing_file_is_static(self, tmp_path):
        assert gltf.has_skin(str(tmp_path / "gone.glb")) is False

    def test_broken_json_is_static(self, tmp_path):
        payload = b'{"skins": ['
        chunks = struct.pack("<I4s", len(payload), b"JSON") + payload
        data = struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks

        assert gltf.has_skin(write(tmp_path, "x.glb", data)) is False


def test_the_answer_is_recomputed_when_the_model_changes(tmp_path):
    path = write(tmp_path, "x.glb", glb({"meshes": [{}]}))
    assert gltf.has_skin(path) is False

    # Same path, new content: the cache is keyed on (mtime, size), not on the path alone.
    (tmp_path / "x.glb").write_bytes(glb({"skins": [{"joints": [0]}], "meshes": [{}]}))

    assert gltf.has_skin(path) is True
