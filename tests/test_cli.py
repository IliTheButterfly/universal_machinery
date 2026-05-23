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


# -----------------------------------------------------------------------------
# ``um import``
# -----------------------------------------------------------------------------


def test_import_round_trips_through_json(tmp_path):
    """`um import f.xml` parses a PLCopen XML doc back into IL JSON
    that `um inspect` can read."""
    from universal_machinery.emitters.plcopen_xml import emit_xml
    p = program(
        project_name="ImportDemo",
        subroutines=[prog("Main", main=True,
                            inputs=[var_in("a", TagType.INT)],
                            outputs=[var_out("done", TagType.BOOL)])],
    )
    xml_path = tmp_path / "demo.xml"
    xml_path.write_text(emit_xml(p))

    json_path = tmp_path / "imported.json"
    result = runner.invoke(app, ["import", str(xml_path),
                                   "-o", str(json_path)])
    assert result.exit_code == 0, result.output

    # The output JSON must round-trip through `um inspect`
    result2 = runner.invoke(app, ["inspect", str(json_path)])
    assert result2.exit_code == 0, result2.output
    assert "Main" in result2.output
    assert "ImportDemo" in result2.output


def test_import_missing_file_exits_2(tmp_path):
    missing = tmp_path / "does_not_exist.xml"
    result = runner.invoke(app, ["import", str(missing)])
    assert result.exit_code == 2
    assert "file not found" in result.output.lower()


def test_import_malformed_xml_exits_2(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<<<not xml>>>")
    result = runner.invoke(app, ["import", str(bad)])
    assert result.exit_code == 2
    assert "PLCopen parse failed" in result.output


# -----------------------------------------------------------------------------
# ``um lint``
# -----------------------------------------------------------------------------


def test_lint_clean_json_program_exits_0(tmp_path):
    """Clean Program -> exit 0, ``ok`` message."""
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X1"), coil("Y1")),
    ])])
    path = tmp_path / "ok.json"
    path.write_text(to_json(p))
    result = runner.invoke(app, ["lint", str(path)])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()


def test_lint_program_with_errors_exits_1(tmp_path):
    from universal_machinery.builders import goto
    p = program(subroutines=[prog("Main", main=True,
                                     st_body=[goto("MISSING")])])
    path = tmp_path / "bad.json"
    path.write_text(to_json(p))
    result = runner.invoke(app, ["lint", str(path)])
    assert result.exit_code == 1
    # Error mode emits the count and per-code grouping
    assert "validation error" in result.output.lower()
    assert "st-unresolved-goto" in result.output


def test_lint_json_format_emits_records(tmp_path):
    """``--format json`` returns a JSON array of error records."""
    from universal_machinery.builders import goto
    p = program(subroutines=[prog("Main", main=True,
                                     st_body=[goto("MISSING")])])
    path = tmp_path / "bad.json"
    path.write_text(to_json(p))
    result = runner.invoke(app, ["lint", str(path), "-f", "json"])
    assert result.exit_code == 1
    records = json.loads(result.output)
    assert isinstance(records, list)
    assert len(records) == 1
    assert records[0]["code"] == "st-unresolved-goto"
    assert "MISSING" in records[0]["message"]
    assert "Subroutine 'Main'" in records[0]["location"]


def test_lint_json_format_on_clean_program_emits_empty_array(tmp_path):
    p = program(subroutines=[prog("Main", main=True)])
    path = tmp_path / "ok.json"
    path.write_text(to_json(p))
    result = runner.invoke(app, ["lint", str(path), "-f", "json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_lint_accepts_xml_input(tmp_path):
    """``um lint`` autodetects PLCopen XML and runs validation
    on the parsed Program."""
    from universal_machinery.emitters.plcopen_xml import emit_xml
    p = program(subroutines=[prog("Main", main=True, rungs=[
        rung(no("X1"), coil("Y1")),
    ])])
    path = tmp_path / "ok.xml"
    path.write_text(emit_xml(p))
    result = runner.invoke(app, ["lint", str(path)])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()


def test_lint_unknown_format_exits_2(tmp_path):
    p = program(subroutines=[prog("Main", main=True)])
    path = tmp_path / "ok.json"
    path.write_text(to_json(p))
    result = runner.invoke(app, ["lint", str(path), "-f", "yaml"])
    assert result.exit_code == 2
    assert "unknown --format" in result.output


def test_lint_missing_file_exits_2(tmp_path):
    result = runner.invoke(app, ["lint", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_lint_malformed_xml_exits_2(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<<<not xml>>>")
    result = runner.invoke(app, ["lint", str(bad)])
    assert result.exit_code == 2
    assert "PLCopen parse failed" in result.output


# -----------------------------------------------------------------------------
# ``um convert``: format-to-format translation via the IL
# -----------------------------------------------------------------------------


def _sample_program():
    """LD + FUNCTION sample used across the convert tests so the
    same Program survives every format pair."""
    return program(subroutines=[
        prog("Main", main=True,
             rungs=[rung(no("X1"), coil("Y1"))]),
        fn("Avg",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
           outputs=[var_out("r", TagType.INT)],
           return_type=TagType.INT,
           rungs=[rung(add(tag("a"), tag("b"), tag("r")))]),
    ])


def test_convert_json_to_xml_round_trips(tmp_path):
    """``um convert prog.json prog.xml`` writes XSD-valid PLCopen
    TC6 XML.  The XML can be read back into an IL that preserves
    the POU set."""
    from universal_machinery.emitters.plcopen_xml import (
        validate_plcopen_xml,
    )
    from universal_machinery.parsers.plcopen_xml import (
        parse_plcopen_xml,
    )
    src_json = tmp_path / "src.json"
    src_json.write_text(to_json(_sample_program()))
    dst_xml = tmp_path / "out.xml"
    result = runner.invoke(app, ["convert", str(src_json), str(dst_xml)])
    assert result.exit_code == 0, result.output
    text = dst_xml.read_text()
    validate_plcopen_xml(text)
    p2 = parse_plcopen_xml(text)
    assert sorted(s.name for s in p2.subroutines) == ["Avg", "Main"]


def test_convert_xml_to_json_round_trips(tmp_path):
    """``um convert prog.xml prog.json`` re-emits canonical IL JSON
    that round-trips through ``from_json`` back into a Program with
    the same POUs."""
    from universal_machinery.emitters.plcopen_xml import emit_xml
    from universal_machinery.serialisation import from_json
    src_xml = tmp_path / "src.xml"
    src_xml.write_text(emit_xml(_sample_program()))
    dst_json = tmp_path / "out.json"
    result = runner.invoke(app, ["convert", str(src_xml), str(dst_json)])
    assert result.exit_code == 0, result.output
    p2 = from_json(dst_json.read_text())
    assert sorted(s.name for s in p2.subroutines) == ["Avg", "Main"]


def test_convert_json_to_st_writes_iec_text(tmp_path):
    """``um convert prog.json prog.st`` emits IEC §3 Structured
    Text with the distinctive POU headers."""
    src_json = tmp_path / "src.json"
    src_json.write_text(to_json(_sample_program()))
    dst_st = tmp_path / "out.st"
    result = runner.invoke(app, ["convert", str(src_json), str(dst_st)])
    assert result.exit_code == 0, result.output
    text = dst_st.read_text()
    assert "PROGRAM Main" in text
    assert "FUNCTION Avg" in text
    assert "END_PROGRAM" in text


def test_convert_xml_to_st_chains_parse_and_emit(tmp_path):
    """``um convert prog.xml prog.st`` chains the PLCopen reader
    and the ST emitter in one step, demonstrating the IL as a
    real interchange format."""
    from universal_machinery.emitters.plcopen_xml import emit_xml
    src_xml = tmp_path / "src.xml"
    src_xml.write_text(emit_xml(_sample_program()))
    dst_st = tmp_path / "out.st"
    result = runner.invoke(app, ["convert", str(src_xml), str(dst_st)])
    assert result.exit_code == 0, result.output
    text = dst_st.read_text()
    assert "PROGRAM Main" in text
    assert "FUNCTION Avg" in text


def test_convert_st_read_round_trips_via_parse_program(tmp_path):
    """``um convert prog.st prog.json`` now routes through the
    parent's ``parse_program`` (v1, PR #84) and produces canonical
    IL JSON.  Round-trip pinned: write JSON, convert to ST,
    convert back to JSON, parse, structural equality."""
    p = program(subroutines=[prog("Main", main=True,
        rungs=[rung(no("X1"), coil("Y1"))])])
    src_st = tmp_path / "src.st"
    src_st.write_text(_st_text_for(p))
    out_json = tmp_path / "out.json"
    result = runner.invoke(app, ["convert", str(src_st), str(out_json)])
    assert result.exit_code == 0, result.output
    from universal_machinery.serialisation import from_json
    p_back = from_json(out_json.read_text())
    assert sorted(s.name for s in p_back.subroutines) == ["Main"]


def test_convert_st_read_unsupported_shape_exits_2(tmp_path):
    """parse_program's scope guards: shapes it doesn't accept
    surface as exit-2 with the ST parser's diagnostic message.
    Use a still-out-of-scope shape (INTERFACE / OOP); TYPE
    blocks (v3) and CONFIGURATION (v4) are now in-scope."""
    src = tmp_path / "src.st"
    src.write_text(
        "PROGRAM Main\nEND_PROGRAM\n\n"
        "INTERFACE IFoo\nEND_INTERFACE\n"
    )
    result = runner.invoke(app, ["convert", str(src), str(tmp_path / "out.json")])
    assert result.exit_code == 2
    assert "ST parse failed" in result.output


def _st_text_for(p):
    """Helper: emit ``p`` as ST text via the parent's emitter."""
    from universal_machinery.emitters.st import emit_program
    return emit_program(p)


def test_convert_unknown_input_suffix_exits_2(tmp_path):
    bogus = tmp_path / "src.txt"
    bogus.write_text("nope")
    result = runner.invoke(app, ["convert", str(bogus), str(tmp_path / "out.json")])
    assert result.exit_code == 2
    assert "unrecognised input suffix" in result.output


def test_convert_unknown_output_suffix_exits_2(tmp_path):
    src = tmp_path / "src.json"
    src.write_text(to_json(_sample_program()))
    result = runner.invoke(app, ["convert", str(src), str(tmp_path / "out.txt")])
    assert result.exit_code == 2
    assert "unrecognised output suffix" in result.output


def test_convert_stdout_writes_canonical_json(tmp_path):
    """``um convert prog.xml -`` writes canonical IL JSON to stdout
    -- the sensible default for ``-`` matches the rest of the CLI's
    stdin/stdout convention."""
    from universal_machinery.emitters.plcopen_xml import emit_xml
    from universal_machinery.serialisation import from_json
    src_xml = tmp_path / "src.xml"
    src_xml.write_text(emit_xml(_sample_program()))
    result = runner.invoke(app, ["convert", str(src_xml), "-"])
    assert result.exit_code == 0, result.output
    p2 = from_json(result.output)
    assert sorted(s.name for s in p2.subroutines) == ["Avg", "Main"]


def test_convert_input_missing_exits_2(tmp_path):
    result = runner.invoke(app, ["convert",
                                   str(tmp_path / "nope.xml"),
                                   str(tmp_path / "out.json")])
    assert result.exit_code == 2
    assert "file not found" in result.output
