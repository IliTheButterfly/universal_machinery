"""Smoke tests for the vendor-neutral IL."""

from universal_machinery.il import Address, Program, Rung, Subroutine, Tag, TagType
from universal_machinery.il.ops import (
    Call, ContactNC, ContactNO, End, Move, OutCoil, Return, addresses_of,
)


def test_program_collects_addresses():
    prog = Program(
        subroutines=[
            Subroutine(
                name="Main",
                main=True,
                rungs=[
                    Rung([Call(target="Sub1")]),
                    Rung([End()]),
                ],
            ),
            Subroutine(
                name="Sub1",
                rungs=[
                    Rung([
                        ContactNO(Address("X001")),
                        ContactNC(Address("X002")),
                        OutCoil(Address("Y001")),
                    ]),
                    Rung([Move(src=Address("DS20"), dst=Address("DS21"))]),
                    Rung([Return()]),
                ],
            ),
        ]
    )
    refs = prog.referenced_addresses()
    assert {a.raw for a in refs} == {"X001", "X002", "Y001", "DS20", "DS21"}


def test_main_subroutine_lookup():
    prog = Program(
        subroutines=[
            Subroutine(name="Sub1"),
            Subroutine(name="Main", main=True),
        ]
    )
    main = prog.main_subroutine()
    assert main is not None and main.name == "Main"
    assert prog.find_subroutine("Sub1") is not None
    assert prog.find_subroutine("nope") is None


def test_addresses_of_handles_compounds():
    from universal_machinery.il.ops import Compare, ParallelGroup
    op = ParallelGroup(branches=(
        (ContactNO(Address("X001")),),
        (ContactNC(Address("X002")), Compare(op=">", lhs=Address("DS10"), rhs="100")),
    ))
    addrs = {a.raw for a in addresses_of(op)}
    assert addrs == {"X001", "X002", "DS10"}
