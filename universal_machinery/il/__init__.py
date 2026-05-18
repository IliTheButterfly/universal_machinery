"""universal_machinery.il -- vendor-neutral intermediate language for PLC programs.

This package defines an AST that captures Ladder Diagram (LD) and a small
subset of Structured Text (ST) per IEC 61131-3 Part 3.  Vendor backends
in `universal_machinery.backends.<vendor>` lower IL down to vendor file
formats (CKP, PLCopen XML, Structured Text, ...) and parse those formats
back into IL.

Design goals:

  - Cover what real PLC programs need: contacts, coils, timers, counters,
    compare ops, math ops, function blocks, calls, jumps.  Anything an
    AutomationDirect CLICK or Allen-Bradley MicroLogix or OpenPLC project
    typically uses.
  - Be vendor-neutral: no CKP-specific bytes leak into the IL.
  - Round-trip: `decode -> IL -> encode` should be lossless for the same
    backend, lossy-but-meaningful across backends (some constructs may
    not exist on all targets and trigger an explicit ``Unsupported``).
  - Static-typeable: every AST node is a frozen dataclass.

Read the high-level ``Program``, the ``Subroutine`` collection of named
ladder routines, the ``Rung`` linear logic row, and the union ``Op``
that captures every supported instruction.

Example::

    from universal_machinery.il import Program, Subroutine, Rung, Address
    from universal_machinery.il.ops import ContactNO, OutCoil, Call, End

    prog = Program(
        subroutines=[
            Subroutine(
                name="Main",
                rungs=[
                    Rung([Call(target="Sub1")]),
                    Rung([End()]),
                ],
            ),
            Subroutine(
                name="Sub1",
                rungs=[
                    Rung([ContactNO(Address("X001")), OutCoil(Address("Y001"))]),
                ],
            ),
        ],
    )

    # Compile to a vendor target
    from universal_machinery.backends.click import ClickBackend  # via submodule
    ClickBackend().write(prog, "out.ckp")
"""
from .ast import (
    Address,
    DataBlock,
    PouKind,
    Program,
    Rung,
    Subroutine,
    Tag,
    TagRef,
    TagType,
    Var,
    VarDirection,
)
from . import ops, sfc
from .ops import Loc, Value, VendorOp
from .sfc import Action, SfcNetwork, Step, Transition

__all__ = [
    "Action",
    "Address",
    "DataBlock",
    "Loc",
    "PouKind",
    "Program",
    "Rung",
    "SfcNetwork",
    "Step",
    "Subroutine",
    "Tag",
    "TagRef",
    "TagType",
    "Transition",
    "Value",
    "Var",
    "VarDirection",
    "VendorOp",
    "ops",
    "sfc",
]

__version__ = "0.3.0"
