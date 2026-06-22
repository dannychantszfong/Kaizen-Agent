"""Tests for the four built-in tools.

The star is `edit`: its match / no-match / multi-match branches are what make it
reliable for an LLM, so they each get a test.
"""

from __future__ import annotations

from spine.tools import BashTool, EditTool, ReadTool, WriteTool


# -- read -------------------------------------------------------------------


def test_read_returns_numbered_lines(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("first\nsecond\nthird\n", encoding="utf-8")

    result = ReadTool().execute(ReadTool.parameters(path=str(f)))

    assert not result.is_error
    assert "1\tfirst" in result.output
    assert "3\tthird" in result.output


def test_read_offset_and_limit_page_the_file(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)), encoding="utf-8")

    result = ReadTool().execute(ReadTool.parameters(path=str(f), offset=3, limit=2))

    assert "3\tline3" in result.output
    assert "4\tline4" in result.output
    assert "line5" not in result.output


def test_read_missing_file_is_an_error(tmp_path):
    result = ReadTool().execute(ReadTool.parameters(path=str(tmp_path / "nope.txt")))

    assert result.is_error
    assert "not found" in result.output.lower()


# -- write ------------------------------------------------------------------


def test_write_creates_file_and_parents(tmp_path):
    target = tmp_path / "nested" / "dir" / "out.txt"

    result = WriteTool().execute(WriteTool.parameters(path=str(target), content="hi\n"))

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "hi\n"


def test_write_overwrites(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("old", encoding="utf-8")

    WriteTool().execute(WriteTool.parameters(path=str(f), content="new"))

    assert f.read_text(encoding="utf-8") == "new"


# -- edit: the three branches ----------------------------------------------


def test_edit_exact_single_match_replaces(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")

    result = EditTool().execute(
        EditTool.parameters(path=str(f), old_string="x = 1", new_string="x = 42")
    )

    assert not result.is_error
    assert f.read_text(encoding="utf-8") == "x = 42\ny = 2\n"


def test_edit_no_match_is_a_recoverable_error(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("x = 1\n", encoding="utf-8")

    result = EditTool().execute(
        EditTool.parameters(path=str(f), old_string="z = 9", new_string="z = 0")
    )

    assert result.is_error
    assert "not found" in result.output.lower()
    # File is untouched.
    assert f.read_text(encoding="utf-8") == "x = 1\n"


def test_edit_multi_match_is_a_recoverable_error(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("a = 1\na = 1\n", encoding="utf-8")

    result = EditTool().execute(
        EditTool.parameters(path=str(f), old_string="a = 1", new_string="a = 2")
    )

    assert result.is_error
    assert "not unique" in result.output.lower()
    assert "2 matches" in result.output
    # File is untouched — never a silent edit.
    assert f.read_text(encoding="utf-8") == "a = 1\na = 1\n"


# -- bash -------------------------------------------------------------------


def test_bash_captures_stdout_and_exit_code():
    result = BashTool().execute(BashTool.parameters(command="echo hello"))

    assert not result.is_error
    assert "hello" in result.output
    assert "exit code: 0" in result.output


def test_bash_nonzero_exit_is_an_error():
    result = BashTool().execute(BashTool.parameters(command="exit 3"))

    assert result.is_error
    assert "exit code: 3" in result.output
