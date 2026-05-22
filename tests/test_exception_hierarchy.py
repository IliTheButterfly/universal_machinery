"""Pin the structured-exception hierarchy.

``UniversalMachineryError`` is the catch-all base for any
library-originated failure.  These tests guard against:

  - A new concrete exception being added without inheriting from
    the base (which would force CLI/GUI consumers to chase each
    new type individually).
  - The two ``LoweringError`` re-export paths drifting back into
    separate classes (the bug that motivated PR #74).
  - ``RoundTripError`` not being importable from the public API.
"""
from __future__ import annotations


def test_universal_machinery_error_is_top_level_importable():
    from universal_machinery import UniversalMachineryError
    assert issubclass(UniversalMachineryError, Exception)


def test_lowering_error_is_top_level_importable():
    from universal_machinery import LoweringError, UniversalMachineryError
    assert issubclass(LoweringError, UniversalMachineryError)


def test_round_trip_error_is_top_level_importable():
    from universal_machinery import RoundTripError, UniversalMachineryError
    assert issubclass(RoundTripError, UniversalMachineryError)


def test_concrete_exceptions_inherit_from_base():
    """Every concrete library exception must inherit from
    ``UniversalMachineryError`` so callers can catch them all with
    one ``except`` clause.  When adding a new exception class,
    inherit from the base or this test catches the omission."""
    from universal_machinery import UniversalMachineryError
    from universal_machinery.backends import UnsupportedOpError
    from universal_machinery.emitters.plcopen_xml import XMLSchemaError
    from universal_machinery.parsers.plcopen_xml import PlcopenParseError
    from universal_machinery.parsers.st_text import StParseError
    from universal_machinery.serialisation import SerialisationError
    for cls in (
        UnsupportedOpError,
        XMLSchemaError,
        PlcopenParseError,
        StParseError,
        SerialisationError,
    ):
        assert issubclass(cls, UniversalMachineryError), (
            f"{cls.__name__} must inherit from UniversalMachineryError"
        )


def test_lowering_error_is_unified_across_both_passes():
    """``lowering/click_calling.py`` and ``lowering/fbd_to_st.py``
    each used to define their own ``LoweringError`` class.  Catching
    one path's name did NOT catch the other's failures.  PR #74
    moved the canonical class to ``universal_machinery.exceptions``
    and made both modules re-export it.

    This test pins that the two import paths refer to the SAME
    class so ``except LoweringError`` is reliable regardless of
    which module the name came from."""
    from universal_machinery.exceptions import LoweringError as base
    from universal_machinery.lowering.click_calling import (
        LoweringError as click_error,
    )
    from universal_machinery.lowering.fbd_to_st import (
        LoweringError as fbd_error,
    )
    assert click_error is base
    assert fbd_error is base


def test_lowering_error_is_catchable_via_base():
    """A consumer that catches ``UniversalMachineryError`` should
    catch every concrete library exception, including the lowering
    failures from both passes."""
    from universal_machinery import UniversalMachineryError
    from universal_machinery.lowering.click_calling import LoweringError
    raised = LoweringError("synthetic")
    assert isinstance(raised, UniversalMachineryError)


def test_round_trip_error_carries_message():
    from universal_machinery import RoundTripError
    err = RoundTripError("ckp -> il -> ckp differs at byte 42")
    assert "byte 42" in str(err)
