"""Tests for the expanded IEC §2.5.2 standard-function registry.

The previous slice's registry covered numerical / bit-string /
logical / selection / comparison families plus a handful of named
conversions and a small time/date subset.  This expansion fills
out:

  - §2.5.2.1 type conversions: every ``<SRC>_TO_<DST>`` pair for
    the 21 IEC elementary types (420 conversions), plus BCD and
    TRUNC families.
  - §2.5.2.10 time/date arithmetic: the full table-30 set.

These tests verify the membership shape, the predicate, and the
two new builder helpers.
"""
import pytest

from universal_machinery.builders import (
    convert, trunc, std_func,
)
from universal_machinery.il import (
    STD_FUNCTION_NAMES, StdFunc, TagRef, iec_convertible_types,
    is_iec_std_function,
)


# -----------------------------------------------------------------------------
# Registry shape
# -----------------------------------------------------------------------------


def test_registry_size_grew_past_legacy_floor():
    """A floor on the size so a regression that drops a whole
    family is caught.

    Current breakdown (511 names total):
      - 420 ``<SRC>_TO_<DST>`` type-conversion pairs
      - 12 BCD conversion pairs
      - 17 TRUNC family (generic + 16 typed variants)
      - 11 numerical functions
      - 7 arithmetic functions
      - 4 bit-string functions
      - 4 logical functions
      - 5 selection functions
      - 6 comparison functions
      - 9 string functions
      - 17 time/date functions

    The 500 floor catches a regression that removes any of the
    smaller families wholesale; the 400 floor was the original
    "did we drop back to the legacy ~50-name set" sanity check."""
    assert len(STD_FUNCTION_NAMES) >= 500


def test_iec_convertible_types_returns_tuple():
    types = iec_convertible_types()
    assert isinstance(types, tuple)
    # Standard IEC elementary types: BOOL, BYTE/WORD/DWORD/LWORD,
    # SINT/INT/DINT/LINT, USINT/UINT/UDINT/ULINT, REAL/LREAL,
    # TIME/DATE/TOD/DT, STRING/WSTRING = 21
    assert len(types) == 21
    assert "BOOL" in types
    assert "LREAL" in types
    assert "WSTRING" in types


# -----------------------------------------------------------------------------
# §2.5.2.1 -- type conversions
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("src,dst", [
    ("BOOL", "INT"),
    ("INT", "REAL"),
    ("REAL", "INT"),
    ("LREAL", "REAL"),
    ("WORD", "DWORD"),
    ("DINT", "STRING"),
    ("STRING", "WSTRING"),
    ("DATE", "DT"),
    ("BOOL", "WSTRING"),     # unusual but allowed per IEC
    ("ULINT", "LREAL"),
])
def test_type_conversion_pair_registered(src, dst):
    name = f"{src}_TO_{dst}"
    assert is_iec_std_function(name), f"{name!r} missing from registry"


def test_same_type_pair_not_emitted():
    """``X_TO_X`` is meaningless and shouldn't appear -- IEC
    conversions are between distinct types."""
    assert "INT_TO_INT" not in STD_FUNCTION_NAMES
    assert "REAL_TO_REAL" not in STD_FUNCTION_NAMES
    assert "BOOL_TO_BOOL" not in STD_FUNCTION_NAMES


def test_all_pairs_count():
    """21 types × 20 distinct destinations = 420 conversion names."""
    conversions = [n for n in STD_FUNCTION_NAMES
                   if "_TO_" in n
                   and not n.startswith("BCD_TO_")
                   and not n.endswith("_TO_BCD")
                   and not n.startswith("DT_TO_")]
    # Filter out time/date extraction (DT_TO_DATE, DT_TO_TOD, DT_TO_TIME)
    # and BCD pairs which are tracked separately.
    assert len(conversions) >= 400  # leave some slack for edge cases


# -----------------------------------------------------------------------------
# §2.5.2.1 -- TRUNC + BCD families
# -----------------------------------------------------------------------------


def test_trunc_generic_and_typed_variants():
    assert "TRUNC" in STD_FUNCTION_NAMES
    assert "REAL_TRUNC_INT" in STD_FUNCTION_NAMES
    assert "REAL_TRUNC_DINT" in STD_FUNCTION_NAMES
    assert "LREAL_TRUNC_INT" in STD_FUNCTION_NAMES
    assert "LREAL_TRUNC_LINT" in STD_FUNCTION_NAMES


def test_bcd_conversions_registered():
    for pair in ("BCD_TO_INT", "INT_TO_BCD",
                 "BCD_TO_DINT", "DINT_TO_BCD",
                 "BCD_TO_USINT", "USINT_TO_BCD"):
        assert pair in STD_FUNCTION_NAMES, pair


# -----------------------------------------------------------------------------
# §2.5.2.10 -- time / date arithmetic
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "ADD_TIME", "SUB_TIME",
    "MULTIME", "DIVTIME",
    "MUL_TIME", "DIV_TIME",
    "ADD_TOD_TIME", "SUB_TOD_TIME",
    "ADD_DT_TIME", "SUB_DT_TIME",
    "SUB_DATE_DATE", "SUB_TOD_TOD", "SUB_DT_DT",
    "CONCAT_DATE_TOD",
    "DT_TO_DATE", "DT_TO_TOD", "DT_TO_TIME",
])
def test_time_date_function_registered(name):
    assert is_iec_std_function(name), f"{name!r} missing from registry"


# -----------------------------------------------------------------------------
# §2.5.2.5 -- arithmetic functions (function-call form of +, -, *, /,
# MOD, **, plus MOVE)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("name",
                            ["ADD", "SUB", "MUL", "DIV", "MOD",
                             "EXPT", "MOVE"])
def test_arithmetic_function_registered(name):
    """IEC §2.5.2.5 table 24 -- the function-call form of the
    arithmetic operators (plus ``MOVE`` from §2.5.2.1).  The IL
    normally renders these via ``BinaryMath`` / ``Move`` ops (which
    produce the infix form), so this registry entry exists for
    recognition of the function form when parsing third-party ST.
    matiec accepts both forms; the round-trip test pins the
    function-call path."""
    assert is_iec_std_function(name), f"{name!r} missing from registry"


# -----------------------------------------------------------------------------
# §2.5.2.10 -- comparison functions (function-call form of >, >=, =, <=, <, <>)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["GT", "GE", "EQ", "LE", "LT", "NE"])
def test_comparison_function_registered(name):
    """IEC §2.5.2.10 table 33 -- the function-call form of the
    comparison operators.  matiec compiles ``r := GT(a, b);``
    cleanly, so the registry should recognise these names even
    though the IL normally renders comparisons via ``Compare``
    op (which produces the infix form)."""
    assert is_iec_std_function(name), f"{name!r} missing from registry"


# -----------------------------------------------------------------------------
# Predicate
# -----------------------------------------------------------------------------


def test_is_iec_std_function_accepts_iec_names():
    assert is_iec_std_function("ABS")
    assert is_iec_std_function("INT_TO_REAL")
    assert is_iec_std_function("ADD_TIME")


def test_is_iec_std_function_rejects_vendor_names():
    assert not is_iec_std_function("MY_VENDOR_OP")
    assert not is_iec_std_function("")
    assert not is_iec_std_function("int_to_real")  # case-sensitive


# -----------------------------------------------------------------------------
# Builder helpers
# -----------------------------------------------------------------------------


def test_convert_builder_produces_named_std_func():
    op = convert("INT", "REAL", "count", "count_real")
    assert isinstance(op, StdFunc)
    assert op.name == "INT_TO_REAL"
    assert op.inputs == (TagRef("count"),)
    assert op.output == TagRef("count_real")


def test_convert_builder_uppercases_type_names():
    """Common typo: lowercase type names.  The builder upper-cases
    so the resulting name is registry-valid."""
    op = convert("int", "real", "count", "count_real")
    assert op.name == "INT_TO_REAL"
    assert is_iec_std_function(op.name)


def test_convert_builder_accepts_unusual_pairs():
    """The builder doesn't gatekeep -- pairs outside the registry
    are emitted as vendor-extension names if the user explicitly
    asks."""
    op = convert("CUSTOM", "ALSO_CUSTOM", "x", "y")
    assert op.name == "CUSTOM_TO_ALSO_CUSTOM"
    assert not is_iec_std_function(op.name)


def test_trunc_builder_default_emits_generic_TRUNC():
    op = trunc("rate", "rate_int")
    assert op.name == "TRUNC"


def test_trunc_builder_typed_variant():
    op = trunc("rate", "rate_int", dst_type="INT")
    assert op.name == "REAL_TRUNC_INT"


def test_trunc_builder_lreal_source():
    op = trunc("rate", "rate_lint", dst_type="LINT", src_type="LREAL")
    assert op.name == "LREAL_TRUNC_LINT"
    assert is_iec_std_function(op.name)
