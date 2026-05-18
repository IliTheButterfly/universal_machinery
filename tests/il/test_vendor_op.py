"""Tests for the VendorOp extension protocol.

Exercises:
  - VendorOp construction (flat + subclassed forms)
  - addresses_of() picks up VendorOp.addresses
  - VendorOp inside a rung contributes to Program.referenced_addresses
  - Frozen / hashable semantics
"""
from dataclasses import dataclass

from universal_machinery.il import (
    Address, Program, Rung, Subroutine, VendorOp,
)
from universal_machinery.il.ops import addresses_of, ContactNO


def test_vendor_op_basic_construction():
    op = VendorOp(vendor="click", name="DRUM",
                  operands=(10, "step1"),
                  attributes=(("preset", 1000),),
                  addresses=(Address("DS50"), Address("DS51")),
                  comment="six-step recipe")
    assert op.vendor == "click"
    assert op.name == "DRUM"
    assert op.addresses == (Address("DS50"), Address("DS51"))
    assert op.attributes == (("preset", 1000),)


def test_vendor_op_addresses_collected():
    op = VendorOp(vendor="siemens", name="SCL_S_LOOP",
                  addresses=(Address("DB7"), Address("DB7.DBW0")))
    assert {a.raw for a in addresses_of(op)} == {"DB7", "DB7.DBW0"}


def test_vendor_op_without_addresses_emits_nothing():
    """A VendorOp with no declared addresses contributes none -- the
    backend that authored it knows what it touches; the generic walker
    cannot introspect arbitrary operands."""
    op = VendorOp(vendor="click", name="DRUM",
                  operands=("opaque", 42))
    assert addresses_of(op) == set()


def test_vendor_op_in_rung_contributes_to_program_addresses():
    prog = Program(subroutines=[
        Subroutine(name="Main", main=True, rungs=[
            Rung([
                ContactNO(Address("X001")),
                VendorOp(vendor="click", name="DRUM",
                         addresses=(Address("DS50"), Address("DS51"))),
            ]),
        ]),
    ])
    addrs = {a.raw for a in prog.referenced_addresses()}
    assert addrs == {"X001", "DS50", "DS51"}


def test_vendor_op_is_frozen():
    """VendorOp inherits frozen=True so AST equality + hashing work."""
    import dataclasses
    op = VendorOp(vendor="click", name="DRUM")
    try:
        op.name = "OTHER"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("VendorOp must be frozen")


def test_vendor_op_hashable_and_equal():
    a = VendorOp(vendor="click", name="DRUM",
                 addresses=(Address("DS50"),))
    b = VendorOp(vendor="click", name="DRUM",
                 addresses=(Address("DS50"),))
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}


def test_vendor_op_subclass_still_works_as_op():
    """Backends may subclass VendorOp for stronger typing; instances
    still satisfy isinstance(op, VendorOp) and addresses_of() picks
    them up uniformly."""
    @dataclass(frozen=True)
    class ClickDrum(VendorOp):
        vendor: str = "click"
        name:   str = "DRUM"
        preset: int = 0

    op = ClickDrum(addresses=(Address("DS50"),), preset=1000)
    assert isinstance(op, VendorOp)
    assert op.preset == 1000
    assert {a.raw for a in addresses_of(op)} == {"DS50"}
