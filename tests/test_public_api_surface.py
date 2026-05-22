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
