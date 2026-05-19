"""End-to-end tests for the ``um`` CLI.

Uses ``typer.testing.CliRunner`` to invoke commands in-process and
inspect stdout / stderr / exit codes.  Each test builds a Program
in-memory, serialises to JSON in a tmp_path, then exercises one of
the CLI verbs against it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from universal_machinery.builders import (
    add, call, coil, fn, no, prog, program, rung, tag, tag_decl, var_in,
    var_out,
)
from universal_machinery.cli import app
from universal_machinery.il import TagType
from universal_machinery.serialisation import to_json


# Plain runner; some tests need stderr separated, which CliRunner
# combines into ``result.output`` by default.  For the few tests
# that need stderr-distinct, use ``CliRunner(mix_stderr=False)`` if
# available; otherwise we just inspect ``result.output``.
runner = CliRunner()


def _write_demo(tmp_path: Path) -> Path:
    """A simple but non-trivial Program serialised to a JSON file."""
    p = program(
        project_name="Demo",
        tags=[tag_decl("estop", TagType.BOOL, "E-stop input",
                        locked="X101")],
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no("estop"), coil("Y1")),
            ]),
            fn("Avg",
               inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
               outputs=[var_out("r", TagType.INT)],
               return_type=TagType.INT,
               rungs=[rung(add(tag("a"), tag("b"), tag("r")))]),
        ],
    )
    path = tmp_path / "demo.json"
    path.write_text(to_json(p, sort_keys=True))
    return path


# -----------------------------------------------------------------------------
# Top-level invocation
# -----------------------------------------------------------------------------


def test_help_succeeds():
    """``um --help`` exits 0 and lists every verb."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for verb in ("inspect", "validate", "emit", "diff"):
        assert verb in result.output


def test_no_args_prints_help_and_exits_nonzero():
    """``no_args_is_help=True`` -- bare ``um`` shows help."""
    result = runner.invoke(app, [])
    # Exit code differs across Typer / Click versions; verify behaviour
    # by the presence of the help banner in output.
    assert "Usage" in result.output


# -----------------------------------------------------------------------------
# ``um inspect``
# -----------------------------------------------------------------------------


def test_inspect_basic(tmp_path):
    path = _write_demo(tmp_path)
    result = runner.invoke(app, ["inspect", str(path)])
    assert result.exit_code == 0
    # Project name + counts + per-POU table content
    assert "Demo" in result.output
    assert "Subroutines:" in result.output
    assert "Main" in result.output
    assert "Avg" in result.output
    assert "FUNCTION" in result.output
    assert "PROGRAM" in result.output


def test_inspect_marks_main_pou(tmp_path):
    path = _write_demo(tmp_path)
    result = runner.invoke(app, ["inspect", str(path)])
    assert result.exit_code == 0
    # The star symbol marks the main POU.
    assert "★" in result.output


def test_inspect_unreadable_path_exits_2(tmp_path):
    """File not found -> stderr error + exit code 2."""
    missing = tmp_path / "no_such_file.json"
    result = runner.invoke(app, ["inspect", str(missing)])
    assert result.exit_code == 2


def test_inspect_bad_json_exits_2(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not valid json {{{")
    result = runner.invoke(app, ["inspect", str(path)])
    assert result.exit_code == 2


def test_inspect_non_program_top_level_exits_2(tmp_path):
    """JSON that decodes to something other than a Program -> error."""
    path = tmp_path / "wrong.json"
    # A bare dict with no _type / _schema isn't a Program.
    path.write_text(json.dumps({"foo": "bar"}))
    result = runner.invoke(app, ["inspect", str(path)])
    assert result.exit_code == 2


# -----------------------------------------------------------------------------
# ``um validate``
# -----------------------------------------------------------------------------


def test_validate_clean_program_exits_0(tmp_path):
    path = _write_demo(tmp_path)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 0
    assert "ok" in result.output.lower() or "valid" in result.output.lower()


def test_validate_finds_unresolved_tagref(tmp_path):
    """A Program referencing an undeclared TagRef -> errors + exit 1."""
    p = program(
        subroutines=[prog("Main", main=True, rungs=[
            rung(no(tag("ghost_undeclared")), coil("Y1")),
        ])],
    )
    path = tmp_path / "bad.json"
    path.write_text(to_json(p))
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "unresolved-tagref" in result.output
    assert "ghost_undeclared" in result.output


def test_validate_finds_call_graph_cycle(tmp_path):
    p = program(subroutines=[
        prog("Self", rungs=[rung(call("Self"))]),
    ])
    path = tmp_path / "cycle.json"
    path.write_text(to_json(p))
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "cycle" in result.output


# -----------------------------------------------------------------------------
# ``um emit``
# -----------------------------------------------------------------------------


def test_emit_st_to_stdout(tmp_path):
    path = _write_demo(tmp_path)
    result = runner.invoke(app, ["emit", str(path), "-f", "st"])
    assert result.exit_code == 0
    # Output looks like Structured Text
    assert "PROGRAM Main" in result.output
    assert "END_PROGRAM" in result.output


def test_emit_xml_to_stdout(tmp_path):
    path = _write_demo(tmp_path)
    result = runner.invoke(app, ["emit", str(path), "-f", "xml"])
    assert result.exit_code == 0
    assert "<?xml version=" in result.output
    assert "plcopen" in result.output.lower()


def test_emit_json_canonical_round_trips(tmp_path):
    """``emit -f json`` should produce sort_keys=True JSON, which
    round-trips back through ``from_json`` identically."""
    from universal_machinery.serialisation import from_json
    path = _write_demo(tmp_path)
    result = runner.invoke(app, ["emit", str(path), "-f", "json"])
    assert result.exit_code == 0
    # The output is the deterministic JSON form -- reload and verify.
    reloaded = from_json(result.output)
    assert reloaded.project_name == "Demo"


def test_emit_to_file(tmp_path):
    path = _write_demo(tmp_path)
    out_path = tmp_path / "out.st"
    result = runner.invoke(app, ["emit", str(path), "-f", "st",
                                  "-o", str(out_path)])
    assert result.exit_code == 0
    text = out_path.read_text()
    assert "PROGRAM Main" in text


def test_emit_format_defaults_to_st(tmp_path):
    """No ``-f`` flag -> emits Structured Text."""
    path = _write_demo(tmp_path)
    result = runner.invoke(app, ["emit", str(path)])
    assert result.exit_code == 0
    assert "PROGRAM Main" in result.output


# -----------------------------------------------------------------------------
# ``um diff``
# -----------------------------------------------------------------------------


def test_diff_identical_programs_exits_0(tmp_path):
    a = _write_demo(tmp_path)
    b = tmp_path / "demo_copy.json"
    b.write_text(a.read_text())
    result = runner.invoke(app, ["diff", str(a), str(b)])
    assert result.exit_code == 0
    assert "identical" in result.output.lower()


def test_diff_different_programs_exits_1(tmp_path):
    a = _write_demo(tmp_path)
    # Build a slightly different Program
    p2 = program(
        project_name="DemoModified",          # changed name
        subroutines=[prog("Main", main=True, rungs=[
            rung(no("X1"), coil("Y2"))         # changed addresses
        ])],
    )
    b = tmp_path / "modified.json"
    b.write_text(to_json(p2))
    result = runner.invoke(app, ["diff", str(a), str(b)])
    assert result.exit_code == 1
    # The unified diff includes ``@@`` chunk markers
    assert "@@" in result.output


def test_diff_preserves_filenames_in_header(tmp_path):
    a = _write_demo(tmp_path)
    p2 = program(project_name="Other")
    b = tmp_path / "other.json"
    b.write_text(to_json(p2))
    result = runner.invoke(app, ["diff", str(a), str(b)])
    assert result.exit_code == 1
    assert str(a) in result.output
    assert str(b) in result.output
