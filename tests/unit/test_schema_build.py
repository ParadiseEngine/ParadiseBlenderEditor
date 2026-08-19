"""Tests for the schema-build watcher's pure half: the source stamp and the staleness test.

The timer and the subprocess are Blender-side and exercised against the real game project; what
must be exact is WHEN a rebuild is owed, and that is decided entirely by these two functions.
"""

from __future__ import annotations

import os

from paradise_blender.pipeline import schema_build


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        file.write(text)


class TestSourceStamp:
    def test_covers_sources_and_project_files(self, tmp_path):
        write(tmp_path / "Game.csproj")
        write(tmp_path / "A.cs")
        write(tmp_path / "Sub/B.cs")
        stamp = schema_build.source_stamp(str(tmp_path))
        assert stamp[1] == 3

    def test_build_output_and_vcs_dirs_are_excluded(self, tmp_path):
        """obj/ churns on every build; counting it would make each build look like a source
        change and the watcher would rebuild forever."""
        write(tmp_path / "A.cs")
        write(tmp_path / "obj/Debug/gen.cs")
        write(tmp_path / "bin/Debug/out.cs")
        write(tmp_path / ".git/hook.cs")
        assert schema_build.source_stamp(str(tmp_path))[1] == 1

    def test_editing_a_file_moves_the_stamp(self, tmp_path):
        write(tmp_path / "A.cs")
        before = schema_build.source_stamp(str(tmp_path))
        os.utime(tmp_path / "A.cs", ns=(before[0] + 1_000_000, before[0] + 1_000_000))
        assert schema_build.source_stamp(str(tmp_path)) != before

    def test_deleting_a_file_moves_the_stamp(self, tmp_path):
        """A deletion lowers no mtime — the file count is what catches it."""
        write(tmp_path / "A.cs")
        write(tmp_path / "B.cs")
        before = schema_build.source_stamp(str(tmp_path))
        os.remove(tmp_path / "B.cs")
        assert schema_build.source_stamp(str(tmp_path)) != before

    def test_unrelated_files_do_not_count(self, tmp_path):
        write(tmp_path / "A.cs")
        write(tmp_path / "notes.md")
        write(tmp_path / "data.json")
        assert schema_build.source_stamp(str(tmp_path))[1] == 1


class TestFailureSummary:
    MSBUILD = (
        "  Determining projects to restore...\n"
        "  ShiningPie.Core -> /x/bin/Debug/net10.0/ShiningPie.Core.dll\n"
        "/x/A.cs(38,1): error CS0116: A namespace cannot directly contain members [/x/Core.csproj]\n"
        "/x/A.cs(38,1): error CS0116: A namespace cannot directly contain members [/x/Core.csproj]\n"
        '/x/P.targets(37,5): error MSB3073: The command "dotnet exec" exited with code 127.\n'
        "\nBuild FAILED.\n    0 Warning(s)\n    2 Error(s)\n"
    )

    def test_extracts_compiler_errors_deduped_in_order(self):
        lines = schema_build.failure_summary(self.MSBUILD)
        assert len(lines) == 2
        assert "CS0116" in lines[0]
        assert "MSB3073" in lines[1]

    def test_falls_back_to_the_tail_when_nothing_matches_the_error_shape(self):
        """A crash or a missing SDK prints no MSBuild-formatted error line; showing nothing
        would tell the author less than the raw tail does."""
        lines = schema_build.failure_summary("something went wrong\nno SDK found\n")
        assert lines == ["something went wrong", "no SDK found"]

    def test_caps_the_line_count(self):
        output = "\n".join(f"/x/F.cs({i},1): error CS{i:04d}: boom" for i in range(30))
        assert len(schema_build.failure_summary(output)) == 10


class TestSchemaStaleness:
    def test_a_missing_dump_is_stale(self, tmp_path):
        write(tmp_path / "A.cs")
        assert schema_build.is_schema_stale(str(tmp_path), str(tmp_path / "missing.json"))

    def test_a_dump_older_than_the_sources_is_stale(self, tmp_path):
        """The state an author is in after editing C# with Blender closed — the one case a
        change-only watcher can never repair, so the first tick checks it explicitly."""
        schema = tmp_path / "authoring-schema.json"
        write(schema)
        write(tmp_path / "A.cs")
        source_time = os.stat(schema).st_mtime_ns + 5_000_000
        os.utime(tmp_path / "A.cs", ns=(source_time, source_time))
        assert schema_build.is_schema_stale(str(tmp_path), str(schema))

    def test_a_fresh_dump_is_not_stale(self, tmp_path):
        write(tmp_path / "A.cs")
        schema = tmp_path / "authoring-schema.json"
        write(schema)
        dump_time = schema_build.source_stamp(str(tmp_path))[0] + 5_000_000
        os.utime(schema, ns=(dump_time, dump_time))
        assert not schema_build.is_schema_stale(str(tmp_path), str(schema))
