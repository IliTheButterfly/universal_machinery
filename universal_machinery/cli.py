"""``um`` -- command-line interface for universal_machinery.

A thin Typer wrapper over the library's existing entry points:

  ``um inspect <file>``   structural summary of an IL Program
  ``um validate <file>``  run the structural validator; exit non-zero on errors
  ``um emit <file> -f st|xml``  emit Structured Text or PLCopen TC6 XML
  ``um diff <a> <b>``     line-diff two IL programs (JSON form)
  ``um import <file.xml>``  parse a PLCopen TC6 XML doc into IL JSON

The CLI takes IL programs in their JSON form (the
``universal_machinery.serialisation`` format).  Vendor-format
readers / writers (CKP, PLCopen XML import) will be wired in as
follow-on slices via a ``backends`` plug-in registry; today the
JSON form is the canonical interchange.

Design rules

  - Every command is a one-call wrapper over a library function.
    No business logic in this module -- the CLI is a UX layer.
  - ``-`` as a filename means stdin / stdout where appropriate.
  - Errors exit with a non-zero status; ``rich``-formatted diagnostic
    text goes to stderr.
"""
from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .il import Program
from .serialisation import (
    SerialisationError, from_json, to_dict, to_json,
)
from .validation import validate


app = typer.Typer(
    name="um",
    help="universal_machinery CLI -- inspect, validate, emit, and diff IL.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

err = Console(stderr=True)
out = Console()


# -----------------------------------------------------------------------------
# Shared helpers (still no business logic -- just I/O glue)
# -----------------------------------------------------------------------------


def _read_program(path: Path) -> Program:
    """Load a Program from a JSON file or stdin (``-``)."""
    try:
        if str(path) == "-":
            text = sys.stdin.read()
        else:
            text = path.read_text()
    except OSError as e:
        err.print(f"[red]error[/red]: can't read {path}: {e}")
        raise typer.Exit(code=2)
    try:
        prog = from_json(text)
    except (SerialisationError, json.JSONDecodeError) as e:
        err.print(f"[red]error[/red]: {path}: {e}")
        raise typer.Exit(code=2)
    if not isinstance(prog, Program):
        err.print(f"[red]error[/red]: {path}: top-level value is not a Program")
        raise typer.Exit(code=2)
    return prog


def _write_output(text: str, dest: Optional[Path]) -> None:
    """Write ``text`` to ``dest`` or stdout (``None`` or ``Path("-")``)."""
    if dest is None or str(dest) == "-":
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    else:
        dest.write_text(text)


# -----------------------------------------------------------------------------
# ``um inspect``
# -----------------------------------------------------------------------------


@app.command()
def inspect(
    file: Path = typer.Argument(..., help="Path to a Program JSON file (or '-' for stdin)."),
) -> None:
    """Print a structural summary of an IL Program.

    Shows the project metadata, POU + tag + user-type + configuration
    counts, and a per-POU breakdown of kind / parameter counts / rung
    count.  Useful as a quick sanity check on a JSON IL artefact.
    """
    prog = _read_program(file)

    out.print(f"[bold]Program[/bold]: {prog.project_name or '(unnamed)'}")
    if prog.cpu_model:
        out.print(f"  CPU model:      {prog.cpu_model}")
    if prog.comment:
        out.print(f"  Comment:        {prog.comment}")

    out.print(
        f"  Subroutines:    {len(prog.subroutines)}    "
        f"Tags: {len(prog.tags)}    "
        f"UDTs: {len(prog.user_types)}    "
        f"DataBlocks: {len(prog.data_blocks)}    "
        f"Configurations: {len(prog.configurations)}"
    )

    if prog.subroutines:
        table = Table(title="POUs", show_header=True, header_style="bold")
        table.add_column("Name")
        table.add_column("Kind")
        table.add_column("Main", justify="center")
        table.add_column("Inputs", justify="right")
        table.add_column("Outputs", justify="right")
        table.add_column("Locals", justify="right")
        table.add_column("Rungs", justify="right")
        for sub in prog.subroutines:
            table.add_row(
                sub.name,
                sub.kind.value,
                "★" if sub.main else "",
                str(len(sub.inputs)),
                str(len(sub.outputs)),
                str(len(sub.local_vars)),
                str(len(sub.rungs)) + (" (sfc)" if sub.sfc else ""),
            )
        out.print(table)


# -----------------------------------------------------------------------------
# ``um validate``
# -----------------------------------------------------------------------------


@app.command()
def validate_(
    file: Path = typer.Argument(..., help="Path to a Program JSON file (or '-' for stdin)."),
) -> None:
    """Run the structural validator on an IL Program.

    Exits with code 0 if the Program is structurally sound; exits
    with code 1 and prints each ``ValidationError`` if not.
    Designed for CI / pre-commit use.
    """
    prog = _read_program(file)
    errors = validate(prog)
    if not errors:
        out.print("[green]ok[/green]: Program is structurally valid.")
        raise typer.Exit(code=0)
    err.print(f"[red]{len(errors)} validation error(s):[/red]")
    for e in errors:
        loc = f"  [dim]{e.location}[/dim]" if e.location else ""
        err.print(f"  [yellow]{e.code}[/yellow]: {e.message}{loc}")
    raise typer.Exit(code=1)


# Typer auto-derives the CLI name from the function name; rename
# ``validate_`` (Python-keyword workaround) back to ``validate`` on
# the command line.
app.registered_commands[-1].name = "validate"


# -----------------------------------------------------------------------------
# ``um emit``
# -----------------------------------------------------------------------------


class EmitFormat(str, Enum):
    st = "st"
    xml = "xml"
    json = "json"


@app.command()
def emit(
    file: Path = typer.Argument(..., help="Path to a Program JSON file (or '-' for stdin)."),
    fmt: EmitFormat = typer.Option(
        EmitFormat.st, "--format", "-f",
        help="Output format: ``st`` (Structured Text), ``xml`` "
             "(PLCopen TC6), or ``json`` (round-trip canonical form).",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Output file path; omit or use ``-`` to write to stdout.",
    ),
) -> None:
    """Emit a Program as Structured Text, PLCopen TC6 XML, or canonical JSON.

    Reads the input as JSON (the canonical IL format), then runs the
    matching emitter.  Useful for round-tripping a JSON artefact into
    a vendor-consumable form during build / CI.
    """
    prog = _read_program(file)
    if fmt is EmitFormat.st:
        from .emitters.st import emit_program
        text = emit_program(prog)
    elif fmt is EmitFormat.xml:
        from .emitters.plcopen_xml import emit_xml
        text = emit_xml(prog)
    else:  # json
        text = to_json(prog, sort_keys=True)
    _write_output(text, output)


# -----------------------------------------------------------------------------
# ``um diff``
# -----------------------------------------------------------------------------


@app.command()
def diff(
    a: Path = typer.Argument(..., help="First Program JSON file."),
    b: Path = typer.Argument(..., help="Second Program JSON file."),
) -> None:
    """Line-diff two IL Programs in canonical JSON form.

    Reads both files, re-emits each as deterministic-key-order JSON
    (so timestamps / dict-ordering noise doesn't show up), then
    prints a unified diff.  Exits 0 if identical; 1 if different.
    Useful for code-review of IL changes since vendor binary
    formats don't diff cleanly.
    """
    import difflib

    pa = _read_program(a)
    pb = _read_program(b)
    sa = to_json(pa, sort_keys=True).splitlines(keepends=True)
    sb = to_json(pb, sort_keys=True).splitlines(keepends=True)
    if sa == sb:
        out.print("[green]ok[/green]: Programs are identical (canonical form).")
        raise typer.Exit(code=0)

    diff_lines = list(difflib.unified_diff(
        sa, sb, fromfile=str(a), tofile=str(b), lineterm="",
    ))
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            out.print(f"[green]{line}[/green]", end="")
        elif line.startswith("-") and not line.startswith("---"):
            out.print(f"[red]{line}[/red]", end="")
        elif line.startswith("@@"):
            out.print(f"[cyan]{line}[/cyan]", end="")
        else:
            out.print(line, end="")
    raise typer.Exit(code=1)


# -----------------------------------------------------------------------------
# `um import`: PLCopen XML -> IL JSON
# -----------------------------------------------------------------------------


@app.command(name="import")
def import_(
    file: Path = typer.Argument(..., help="PLCopen TC6 XML file to parse"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Write to file (default: stdout).  Use '-' for stdout.",
    ),
) -> None:
    """Parse a PLCopen TC6 XML document into IL JSON.

    Closes the round-trip loop: ``um emit f.json -f xml`` produces a
    document this command can read back into the equivalent IL.
    Useful for importing programs authored in PLCopen-conformant
    tools (matiec, Beremiz, OpenPLC editor) into the IL for
    cross-vendor migration.
    """
    from .parsers.plcopen_xml import (
        PlcopenParseError, parse_plcopen_xml_file,
    )

    try:
        prog = parse_plcopen_xml_file(file)
    except FileNotFoundError:
        err.print(f"[red]error[/red]: file not found: {file}")
        raise typer.Exit(code=2)
    except PlcopenParseError as exc:
        err.print(f"[red]error[/red]: PLCopen parse failed: {exc}")
        raise typer.Exit(code=2)

    _write_output(to_json(prog, indent=2), output)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------


def main() -> None:
    """Console-script entry point (see pyproject ``[project.scripts]``)."""
    app()


if __name__ == "__main__":
    main()
