"""Reading a model's JSON and binary data -- a GLB's chunks, or a ``.gltf`` and its buffers.

The mirror has to pick a component, and a skinned mesh is a different component from a static
one in any game that declares both. The distinction is in the model, not in the schema.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

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


class TestReadGlb:
    def test_both_chunks_are_returned(self, tmp_path):
        path = write(tmp_path, "x.glb", glb({"meshes": [{}]}, binary=b"\x01\x02\x03\x04"))

        assert gltf.read_glb(path) == ({"meshes": [{}]}, b"\x01\x02\x03\x04")

    def test_a_chunk_that_runs_past_the_file_is_unreadable(self, tmp_path):
        data = bytearray(glb({"meshes": [{}]}, binary=b"\x00" * 8))
        data[-16:-12] = (64).to_bytes(4, "little")   # the BIN chunk claims 64 bytes it does not have

        assert gltf.read_glb(write(tmp_path, "x.glb", bytes(data))) == ({}, b"")


def project_with(tmp_path) -> str:
    """A project whose ``assets/models/`` is where the ``.gltf`` under test lives."""
    (tmp_path / "assets" / "models").mkdir(parents=True)
    (tmp_path / "assets" / "project.toml").write_text('name = "test"\n', encoding="utf-8")
    return str(tmp_path / "assets" / "models")


def gltf_text(tmp_path, document: dict, name: str = "crate.gltf") -> str:
    return write(Path(project_with(tmp_path)), name, json.dumps(document).encode("utf-8"))


class TestGltf:
    """A ``.gltf`` is read as the GLB it stands for: the same JSON, its buffers as the BIN chunk."""

    def test_an_external_bin_is_the_binary_chunk(self, tmp_path):
        path = gltf_text(tmp_path, {"buffers": [{"uri": "crate.bin", "byteLength": 4}],
                                    "bufferViews": [{"buffer": 0, "byteLength": 4}], "skins": [{}]})
        (tmp_path / "assets" / "models" / "crate.bin").write_bytes(b"\x01\x02\x03\x04")

        document, binary = gltf.read_glb(path)

        assert binary == b"\x01\x02\x03\x04"
        assert document["buffers"] == [{"byteLength": 4}], "the buffer still names a file"
        assert gltf.read_json(path) == document and gltf.has_skin(path)

    def test_buffers_are_concatenated_4_byte_aligned_and_views_follow(self, tmp_path):
        path = gltf_text(tmp_path, {
            "buffers": [{"uri": "data:application/octet-stream;base64,AQID", "byteLength": 3},
                        {"uri": "sub%20dir/second.bin", "byteLength": 2}],
            "bufferViews": [{"buffer": 0, "byteLength": 3}, {"buffer": 1, "byteOffset": 1, "byteLength": 1}],
        })
        (tmp_path / "assets" / "models" / "sub dir").mkdir()
        (tmp_path / "assets" / "models" / "sub dir" / "second.bin").write_bytes(b"\x08\x09")

        document, binary = gltf.read_glb(path)

        assert binary == b"\x01\x02\x03\x00\x08\x09"
        assert document["buffers"] == [{"byteLength": 6}]
        views = document["bufferViews"]
        assert [(view["buffer"], view.get("byteOffset", 0)) for view in views] == [(0, 0), (0, 5)]

    @pytest.mark.parametrize("uri", [
        "../../../outside.bin", "/etc/passwd", "https://example.com/a.bin", "file:///etc/passwd",
        "..%2F..%2F..%2Foutside.bin", "..\\..\\..\\outside.bin",
    ])
    def test_a_buffer_outside_assets_absolute_or_remote_is_refused(self, tmp_path, uri):
        (tmp_path / "outside.bin").write_bytes(b"\x00" * 4)
        path = gltf_text(tmp_path, {"buffers": [{"uri": uri, "byteLength": 4}]})

        with pytest.raises(gltf.GltfError, match=r"crate\.gltf names"):
            gltf.read_glb(path)
        assert "crate.gltf names" in (gltf.reference_refusal(path) or "")

    def test_an_image_outside_assets_is_a_reference_refusal(self, tmp_path):
        path = gltf_text(tmp_path, {"images": [{"uri": "../../../secret.png"}]})

        assert "outside" in (gltf.reference_refusal(path) or "")

    def test_a_file_in_another_folder_of_assets_is_fine(self, tmp_path):
        path = gltf_text(tmp_path, {"buffers": [{"uri": "../shared/crate.bin", "byteLength": 1}],
                                    "images": [{"uri": "../textures/wood.png"}]})
        (tmp_path / "assets" / "shared").mkdir()
        (tmp_path / "assets" / "shared" / "crate.bin").write_bytes(b"\x07")

        assert gltf.read_glb(path)[1] == b"\x07"
        assert gltf.reference_refusal(path) is None

    def test_a_bin_shorter_than_it_declares_is_refused(self, tmp_path):
        path = gltf_text(tmp_path, {"buffers": [{"uri": "crate.bin", "byteLength": 8}]})
        (tmp_path / "assets" / "models" / "crate.bin").write_bytes(b"\x00" * 4)

        with pytest.raises(gltf.GltfError, match="4 bytes of the 8"):
            gltf.read_glb(path)

    def test_a_file_that_is_not_json_is_unreadable(self, tmp_path):
        path = write(Path(project_with(tmp_path)), "x.gltf", b"\x00not json")

        assert gltf.read_json(path) == {} and gltf.read_glb(path) == ({}, b"")


class TestContainerFiles:
    """What a rewritten model is on disk: a GLB whole; a ``.gltf`` as JSON and its buffer."""

    def test_a_glb_is_written_as_it_is(self, tmp_path):
        data = glb({"asset": {"version": "2.0"}})
        assert gltf.container_files(str(tmp_path / "x.glb"), data) == [(str(tmp_path / "x.glb"), data)]

    def test_a_gltf_keeps_its_bin_and_the_bin_is_written_only_when_it_changes(self, tmp_path):
        path = gltf_text(tmp_path, {"asset": {"version": "2.0"}, "images": [{"uri": "wood.png"}],
                                    "buffers": [{"uri": "crate.bin", "byteLength": 4, "name": "geometry"}]})
        bin_path = str(tmp_path / "assets" / "models" / "crate.bin")
        Path(bin_path).write_bytes(b"\x01\x02\x03\x04")
        rewritten = glb({"asset": {"version": "2.0"}, "images": [{"uri": "wood.png"}],
                         "buffers": [{"byteLength": 8, "name": "geometry"}]}, binary=b"\x05" * 8)

        files = gltf.container_files(path, rewritten)

        assert [target for target, _data in files] == [bin_path, path], "the .bin lands before the JSON"
        assert files[0][1] == b"\x05" * 8
        written = json.loads(files[1][1])
        assert written["buffers"] == [{"byteLength": 8, "name": "geometry", "uri": "crate.bin"}]
        assert written["images"] == [{"uri": "wood.png"}]

        Path(bin_path).write_bytes(b"\x05" * 8)
        assert [target for target, _data in gltf.container_files(path, rewritten)] == [path]

    def test_an_embedded_buffer_stays_embedded(self, tmp_path):
        path = gltf_text(tmp_path, {"buffers": [{"uri": "data:application/octet-stream;base64,AQID",
                                                 "byteLength": 3}]})
        rewritten = glb({"buffers": [{"byteLength": 2}]}, binary=b"\x0a\x0b")

        (target, data), = gltf.container_files(path, rewritten)

        assert target == path
        write(tmp_path, "round.gltf", data)
        assert gltf.read_glb(str(tmp_path / "round.gltf"))[1] == b"\x0a\x0b"

    def test_a_gltf_whose_buffers_are_not_a_list_is_refused_by_name(self, tmp_path):
        path = gltf_text(tmp_path, {"buffers": {"uri": "a.bin", "byteLength": 1}})

        with pytest.raises(gltf.GltfError, match="buffers are not a list"):
            gltf.container_files(path, glb({"buffers": [{"byteLength": 1}]}, binary=b"\x00"))

    def test_a_gltf_split_across_buffers_is_refused(self, tmp_path):
        path = gltf_text(tmp_path, {"buffers": [{"uri": "a.bin", "byteLength": 1},
                                                {"uri": "b.bin", "byteLength": 1}]})

        assert "2 buffers" in (gltf.rewrite_refusal(path) or "")
        with pytest.raises(gltf.GltfError, match="2 buffers"):
            gltf.container_files(path, glb({"buffers": [{"byteLength": 1}]}, binary=b"\x00"))
