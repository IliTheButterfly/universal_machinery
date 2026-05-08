"""Vendor-neutral PLC program AST.

The structure mirrors IEC 61131-3 Part 3 (Programmable Languages):

  Program
    └── Subroutine[]                 (a "POU" -- program organization unit)
          └── Rung[]                  (a horizontal logic row in LD)
                └── Op[]              (instructions; see il/ops.py)

A ``Tag`` is a named symbolic reference to a memory address; an
``Address`` is the raw vendor-style location (e.g., "X001", "Y001",
"DS20").  Tags hold display nicknames, data types, and initial values;
addresses are what the PLC actually reads/writes.

Backends are responsible for mapping IL data types onto whatever the
target vendor uses (CLICK uses 0x6065 for discrete contacts, 0x6074
for 16-bit register sources, etc.; OpenPLC uses IEC types directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# -----------------------------------------------------------------------------
# Memory addresses and tags
# -----------------------------------------------------------------------------


class TagType(Enum):
    """Data types per IEC 61131-3 Part 3 §6.4.

    Backends translate to vendor-native type identifiers.  CLICK's
    DISCRETE maps to BOOL; CLICK's SIGNED_INT_16 maps to INT, etc.
    """

    BOOL       = "BOOL"          # 1-bit discrete
    BYTE       = "BYTE"          # 8-bit, treated as bit string
    WORD       = "WORD"          # 16-bit bit string
    DWORD      = "DWORD"         # 32-bit bit string
    SINT       = "SINT"          # signed 8-bit
    INT        = "INT"           # signed 16-bit
    DINT       = "DINT"          # signed 32-bit
    LINT       = "LINT"          # signed 64-bit
    USINT      = "USINT"         # unsigned 8-bit
    UINT       = "UINT"          # unsigned 16-bit
    UDINT      = "UDINT"         # unsigned 32-bit
    ULINT      = "ULINT"         # unsigned 64-bit
    REAL       = "REAL"          # 32-bit float
    LREAL      = "LREAL"         # 64-bit float
    TIME       = "TIME"          # time duration
    STRING     = "STRING"        # variable-length string


@dataclass(frozen=True)
class Address:
    """A vendor-style memory address (e.g. 'X001', 'Y005', 'DS20', 'C100').

    The string is kept verbatim; backends parse and validate against
    their address-space conventions on lower.
    """

    raw: str

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class Tag:
    """A named symbolic reference to a memory address with optional metadata.

    Mirrors what CLICK calls a "nickname entry" and what IEC calls a
    declared variable.
    """

    address: Address
    nickname: str = ""             # human-readable name, may be empty
    data_type: TagType = TagType.BOOL
    initial_value: str = ""        # textual default (e.g. "0", "1.5")
    comment: str = ""              # free-form documentation


# -----------------------------------------------------------------------------
# Program organization
# -----------------------------------------------------------------------------


@dataclass
class Rung:
    """One horizontal logic row in Ladder Diagram.

    The ``ops`` list is read left-to-right: contacts in series form an
    AND; multiple contacts in parallel form an OR (represented via the
    ``ParallelGroup`` op below).  The right-most op is the rung's output
    -- typically a coil, function-block, or jump.
    """

    ops: list[object]              # list[Op]; using object to break import cycle
    comment: str = ""

    def __len__(self) -> int:
        return len(self.ops)


@dataclass
class Subroutine:
    """A Program Organization Unit (POU): a named, callable ladder routine.

    Per IEC 61131-3 there are three POU kinds (PROGRAM, FUNCTION,
    FUNCTION_BLOCK).  We collapse them into one until a backend
    actually needs to distinguish.

    The ``main`` flag marks the entry point.  Most projects have
    exactly one main routine; subordinate ones are reached via Call ops.
    """

    name: str
    rungs: list[Rung] = field(default_factory=list)
    main: bool = False
    comment: str = ""

    def append(self, rung: Rung) -> None:
        self.rungs.append(rung)


@dataclass
class Program:
    """The full PLC program: a collection of subroutines plus a tag table.

    ``tags`` is the symbol table: every memory address referenced by any
    op should have a corresponding ``Tag`` here, even if the nickname is
    empty.  Backends use this table to emit vendor symbol files (CLICK's
    SC-NICK section, OpenPLC's variable declarations, etc.).
    """

    subroutines: list[Subroutine] = field(default_factory=list)
    tags: dict[Address, Tag] = field(default_factory=dict)

    # Optional metadata that some backends consume
    cpu_model: str = ""            # e.g. "C2-01CPU" for CLICK
    project_name: str = ""
    comment: str = ""

    # ----- Lookups -----

    def find_subroutine(self, name: str) -> Optional[Subroutine]:
        for s in self.subroutines:
            if s.name == name:
                return s
        return None

    def main_subroutine(self) -> Optional[Subroutine]:
        for s in self.subroutines:
            if s.main:
                return s
        return None

    def referenced_addresses(self) -> set[Address]:
        """Walk every rung and collect addresses used by any op.

        Backends use this to ensure the symbol table is complete before
        emitting vendor symbol files.
        """
        from . import ops as _ops
        out: set[Address] = set()
        for sub in self.subroutines:
            for rung in sub.rungs:
                for op in rung.ops:
                    out.update(_ops.addresses_of(op))
        return out
