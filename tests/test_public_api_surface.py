"""Pin the documented public API surface.

``docs/API_STABILITY.md`` is the contract for what callers can rely
on across minor versions.  These tests guard against accidental
breakage:

  - Every name listed under "Stable" in the doc must actually
    import.  If we rename or move one without updating the doc
    the test fails first.
  - Every name in the top-level ``__all__`` must be importable.
    Keeps ``from universal_machinery import *`` honest.
  - The exception hierarchy + Backend ABC remain reachable at the
    top level (one ``from universal_machinery import X``,
    not a nested ``from universal_machinery.backends.base
    import X`` dance).
"""
from __future__ import annotations

import importlib


# Names that ``docs/API_STABILITY.md`` advertises as the **Stable**
# top-level surface -- subpackages reachable via ``from
# universal_machinery import <name>``.  Adding a name here without
# updating the doc is fine (the test will pass); removing a name
# from the package without updating both this list and the doc is
# the regression we want to catch.
_DOCUMENTED_STABLE_SUBPACKAGES = (
    "il",
    "builders",
    "emitters",
    "parsers",
    "validation",
    "serialisation",
    "exceptions",
    "backends",
    "cli",
    "lowering",
)

# Names exported directly from the top-level package (not subpackages).
# These are the catch-all classes a CLI/GUI/script consumer wants in
# one import without remembering sub-paths.
_DOCUMENTED_TOPLEVEL_NAMES = (
    "Backend",
    "UniversalMachineryError",
    "LoweringError",
    "RoundTripError",
)


def test_documented_stable_subpackages_are_importable():
    """Every subpackage listed in ``docs/API_STABILITY.md`` as
    Stable must resolve via ``from universal_machinery import X``.
    Renaming or moving any of them is a breaking change requiring
    a deprecation cycle."""
    for name in _DOCUMENTED_STABLE_SUBPACKAGES:
        mod = importlib.import_module(f"universal_machinery.{name}")
        assert mod is not None, (
            f"universal_machinery.{name} disappeared.  This is a "
            f"breaking API change -- update docs/API_STABILITY.md "
            f"with a deprecation notice before removing"
        )


def test_documented_toplevel_names_are_importable():
    """Names ``docs/API_STABILITY.md`` calls out as directly
    importable from ``universal_machinery`` must be attributes of
    the package after import."""
    import universal_machinery as um
    for name in _DOCUMENTED_TOPLEVEL_NAMES:
        assert hasattr(um, name), (
            f"universal_machinery.{name} not exported at the top "
            f"level (docs/API_STABILITY.md says it should be)"
        )


def test_all_includes_every_documented_name():
    """The package's ``__all__`` must list every documented name
    (subpackages + top-level catch-alls).  Keeps
    ``from universal_machinery import *`` consistent with the doc."""
    import universal_machinery as um
    documented = set(_DOCUMENTED_STABLE_SUBPACKAGES + _DOCUMENTED_TOPLEVEL_NAMES)
    missing = documented - set(um.__all__)
    assert not missing, (
        f"docs/API_STABILITY.md claims these names are public but "
        f"they're not in universal_machinery.__all__: "
        f"{sorted(missing)}"
    )


def test_il_subpackage_publishes_stable_node_types():
    """``il`` is the IL AST package; ``docs/API_STABILITY.md`` calls
    out every IL node type as Stable.  Pin a representative sample
    -- if any of these go missing or get renamed without a
    deprecation cycle, downstream programs break."""
    from universal_machinery import il
    for name in (
        # Core
        "Program", "Subroutine", "Rung", "Var", "TagType", "VarDirection",
        # LD ops (via il.ops re-exported through il)
        "SR", "RS", "RTrig", "FTrig",
        # SFC
        "SfcNetwork", "Step", "Transition", "Action",
        # ST
        "Assignment", "IfStatement", "ForStatement", "CaseStatement",
        # User-defined types
        "StructType", "ArrayType", "EnumType", "AliasType",
        "SubrangeType", "NamedType",
        # Configuration / Resource / Task
        "Configuration", "Resource", "TaskSpec", "PouInstance",
        "AccessVar", "ConfigVar",
        # OOP (Experimental per doc, but still in __all__)
        "Method", "Interface", "AccessSpec",
    ):
        assert hasattr(il, name), (
            f"il.{name} missing -- breaks the documented Stable IL "
            f"surface"
        )


def test_exception_hierarchy_consistency():
    """The exception hierarchy listed in
    ``docs/API_STABILITY.md`` must remain consistent: every
    concrete exception inherits from ``UniversalMachineryError``.
    Cross-references the existing
    ``tests/test_exception_hierarchy.py`` but stays in the API
    audit test file so the API surface contract is self-contained."""
    from universal_machinery import (
        LoweringError, RoundTripError, UniversalMachineryError,
    )
    from universal_machinery.backends import UnsupportedOpError
    from universal_machinery.emitters.plcopen_xml import XMLSchemaError
    from universal_machinery.parsers.plcopen_xml import PlcopenParseError
    from universal_machinery.parsers.st_text import StParseError
    from universal_machinery.serialisation import SerialisationError
    for exc in (
        LoweringError, RoundTripError, UnsupportedOpError,
        XMLSchemaError, PlcopenParseError, StParseError,
        SerialisationError,
    ):
        assert issubclass(exc, UniversalMachineryError), (
            f"{exc.__name__} must inherit from UniversalMachineryError "
            f"(docs/API_STABILITY.md commits to this contract)"
        )


# -----------------------------------------------------------------------------
# Backend ABC contract
# -----------------------------------------------------------------------------


def test_backend_abc_methods_are_abstract():
    """``Backend.read`` and ``Backend.write`` are abstract -- you
    can't instantiate ``Backend`` directly; concrete subclasses must
    override both.  Pinned because the ABC contract is part of the
    documented Backend-author surface in ``docs/API_STABILITY.md``."""
    import inspect
    from universal_machinery.backends import Backend
    assert inspect.isabstract(Backend)
    abstract_names = set(Backend.__abstractmethods__)
    assert "read" in abstract_names
    assert "write" in abstract_names


def test_backend_supports_method_is_concrete():
    """``Backend.supports(capability)`` is a concrete helper -- it
    doesn't need to be overridden.  Pinned because backends rely
    on the base implementation."""
    import inspect
    from universal_machinery.backends import Backend
    assert "supports" not in Backend.__abstractmethods__
    sig = inspect.signature(Backend.supports)
    assert "capability" in sig.parameters


def test_register_decorator_assigns_name_and_registers():
    """``@register("foo")`` must: (1) set the class's ``name``
    attribute, (2) make ``get_backend("foo")`` resolve to it.
    Pinned because PR #74's structured-errors and PRs #56/#75/#78's
    backend submodules all depend on this contract."""
    from universal_machinery.backends import (
        Backend, get_backend, register, registered_names,
    )

    @register("__pin_test_backend__")
    class _PinTestBackend(Backend):
        capabilities = frozenset({"ld"})
        def read(self, path): return None
        def write(self, program, path): return None

    try:
        assert _PinTestBackend.name == "__pin_test_backend__"
        assert "__pin_test_backend__" in registered_names()
        assert isinstance(get_backend("__pin_test_backend__"),
                            _PinTestBackend)
    finally:
        # Clean up the registry so the test doesn't leak state.
        from universal_machinery.backends.base import _REGISTRY
        _REGISTRY.pop("__pin_test_backend__", None)


# -----------------------------------------------------------------------------
# Builder DSL headline functions
# -----------------------------------------------------------------------------


def test_builder_dsl_headline_functions_are_callable():
    """``docs/API_STABILITY.md`` calls out ``builders`` as Stable.
    Pin that the headline functions exist + are callable with the
    minimal-required arguments -- rename / signature changes
    surface here.

    Not exhaustive (the full builder DSL is ~50 functions); these
    are the ones every test file + the openplc / rusty / click
    submodules import."""
    from universal_machinery import builders
    headline = (
        # POU constructors
        "program", "prog", "fn", "fb", "method", "abstract_method",
        "interface",
        # LD primitives
        "rung", "no", "nc", "coil", "set_", "reset_",
        # Stateful FBs
        "ton", "tof", "tp", "ctu", "ctd", "ctud",
        "r_trig", "f_trig", "sr", "rs",
        # Math / Move / Compare
        "add", "sub", "mul", "div", "mod", "move",
        "eq", "ne", "lt", "le", "gt", "ge",
        # Stdlib helpers
        "abs_", "sel",
        # Variables
        "var", "var_in", "var_out", "var_inout", "tag", "tag_decl",
        # Control flow
        "jump", "label_", "ret",
        # ST statement builders
        "assign", "if_", "case_", "case_clause", "for_", "while_",
        "repeat_", "fcall_expr", "call_stmt",
    )
    for name in headline:
        assert hasattr(builders, name), (
            f"builders.{name} missing -- breaks the documented "
            f"Stable builder DSL surface"
        )
        assert callable(getattr(builders, name)), (
            f"builders.{name} must be callable"
        )


def test_builders_produce_expected_types():
    """The builders' return types are part of the contract -- a
    program emitter that switches from returning ``Program`` to
    returning ``dict`` would silently break every downstream
    caller without this pin."""
    from universal_machinery.builders import (
        program, prog, rung, no, coil, ton,
    )
    from universal_machinery.il import Program, Subroutine, TagType
    from universal_machinery.il.ops import ContactNO, OutCoil, TON
    from universal_machinery.il.ast import Rung
    p = program(subroutines=[
        prog("Main", main=True, rungs=[rung(no("x"), coil("y"))])
    ])
    assert isinstance(p, Program)
    assert isinstance(p.subroutines[0], Subroutine)
    assert isinstance(p.subroutines[0].rungs[0], Rung)
    assert isinstance(p.subroutines[0].rungs[0].ops[0], ContactNO)
    assert isinstance(p.subroutines[0].rungs[0].ops[1], OutCoil)
    # TON builder returns a TON op
    t = ton("t1", 100, done_bit="done")
    assert isinstance(t, TON)


# -----------------------------------------------------------------------------
# Emitter / parser signatures
# -----------------------------------------------------------------------------


def test_emit_program_returns_str():
    """``emitters.st.emit_program(program) -> str``.  Return type
    is part of the contract."""
    from universal_machinery.builders import prog, program
    from universal_machinery.emitters.st import emit_program
    text = emit_program(program(subroutines=[prog("Main", main=True)]))
    assert isinstance(text, str)
    assert "PROGRAM Main" in text


def test_emit_xml_returns_str_and_accepts_time_now_kwarg():
    """``emitters.plcopen_xml.emit_xml(program, time_now=None) ->
    str``.  Pinning the ``time_now`` kwarg explicitly because
    callers (tests, the CLI's ``um emit``) use it to make
    deterministic output."""
    import inspect
    from datetime import datetime, timezone
    from universal_machinery.builders import prog, program
    from universal_machinery.emitters.plcopen_xml import emit_xml
    sig = inspect.signature(emit_xml)
    assert "time_now" in sig.parameters
    p = program(subroutines=[prog("Main", main=True)])
    xml = emit_xml(p, time_now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert isinstance(xml, str)
    assert xml.startswith("<")


def test_validate_plcopen_xml_raises_xml_schema_error_on_invalid():
    """``validate_plcopen_xml(xml)`` raises ``XMLSchemaError`` on
    schema violations; returns ``None`` on success.  Both halves
    of the contract are documented in
    ``docs/API_STABILITY.md``."""
    import pytest
    from universal_machinery.emitters.plcopen_xml import (
        XMLSchemaError, validate_plcopen_xml,
    )
    with pytest.raises(XMLSchemaError):
        validate_plcopen_xml("<not_plcopen/>")


def test_parse_plcopen_xml_returns_program():
    """``parsers.plcopen_xml.parse_plcopen_xml(xml) -> Program``.
    Round-trip with ``emit_xml`` pinned -- if either side drifts,
    this catches it."""
    from universal_machinery.builders import prog, program
    from universal_machinery.emitters.plcopen_xml import emit_xml
    from universal_machinery.il import Program
    from universal_machinery.parsers.plcopen_xml import parse_plcopen_xml
    p = program(subroutines=[prog("Main", main=True)])
    parsed = parse_plcopen_xml(emit_xml(p))
    assert isinstance(parsed, Program)
    assert sorted(s.name for s in parsed.subroutines) == ["Main"]


# -----------------------------------------------------------------------------
# Validation / serialisation contracts
# -----------------------------------------------------------------------------


def test_validate_returns_list_not_raises():
    """``validation.validate(program) -> list[ValidationError]``.
    Documented contract: ``validate()`` returns a list of issues
    (potentially empty), not raises -- so callers see *every*
    issue in one pass.  Pinning to guard against a refactor that
    converts to exception-raising."""
    from universal_machinery.builders import prog, program
    from universal_machinery.validation import validate, ValidationError
    errors = validate(program(subroutines=[prog("Main", main=True)]))
    assert isinstance(errors, list)
    for e in errors:
        assert isinstance(e, ValidationError)
        assert hasattr(e, "code")
        assert hasattr(e, "message")
        assert hasattr(e, "location")


def test_validation_error_is_not_an_exception():
    """``ValidationError`` is a *dataclass*, not an ``Exception``
    subclass.  Documented in ``docs/API_STABILITY.md`` to
    distinguish it from the structured-exception hierarchy
    (PR #74 / ``UniversalMachineryError``)."""
    from universal_machinery.validation import ValidationError
    assert not issubclass(ValidationError, Exception)


def test_serialisation_round_trip_preserves_program():
    """``from_json(to_json(p))`` is equal to ``p`` for any
    Program.  Foundational contract: the canonical IL JSON is
    lossless.  Used by ``um diff`` + ``um convert`` + every test
    that checks IL equivalence."""
    from universal_machinery.builders import (
        prog, program, rung, no, coil,
    )
    from universal_machinery.serialisation import from_json, to_json
    p = program(subroutines=[
        prog("Main", main=True, rungs=[rung(no("x"), coil("y"))]),
    ])
    p2 = from_json(to_json(p))
    assert p2 == p, "Canonical-JSON round-trip must be lossless"


# -----------------------------------------------------------------------------
# CLI module contract
# -----------------------------------------------------------------------------


def test_cli_module_exposes_typer_app():
    """``universal_machinery.cli`` exposes ``app`` (a ``typer.
    Typer``) and ``main`` (the console-script entry point).  The
    pyproject's ``[project.scripts]`` table binds ``um`` to
    ``universal_machinery.cli:main`` -- this pin catches an
    accidental rename."""
    import typer
    from universal_machinery import cli
    assert hasattr(cli, "app")
    assert isinstance(cli.app, typer.Typer)
    assert hasattr(cli, "main")
    assert callable(cli.main)


# -----------------------------------------------------------------------------
# Versioning
# -----------------------------------------------------------------------------


def test_package_versions_are_strings():
    """``universal_machinery.__version__`` and ``il.__version__``
    are documented in ``docs/API_STABILITY.md`` § "Version policy".
    Both must be strings (not e.g. tuples) so packaging tooling
    handles them correctly."""
    import universal_machinery
    from universal_machinery import il
    assert isinstance(universal_machinery.__version__, str)
    assert isinstance(il.__version__, str)
    # Both follow ``major.minor.patch`` convention; loose check
    # (don't pin specific values -- they bump independently).
    assert universal_machinery.__version__.count(".") >= 1
    assert il.__version__.count(".") >= 1
