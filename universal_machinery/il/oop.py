"""IEC 61131-3 3rd edition OOP additions: METHOD, INTERFACE,
ABSTRACT, EXTENDS, IMPLEMENTS, and access specifiers.

These extend the FUNCTION_BLOCK model to support encapsulation,
inheritance, and interface-style polymorphism added in the 2013
edition of the standard.  Modeled here as separate dataclasses
keyed by name so the existing ``Subroutine`` shape stays
backwards-compatible (FBs that don't use OOP features have
``methods=[]`` / ``extends=None`` / ``implements=[]``).

PLCopen XML mapping caveat
--------------------------

The bundled TC6 v2.01 XSD predates the IEC 3rd edition and has
no native elements for ``<method>`` / ``<interface>``.  Programs
that use these constructs get full ST emission (every PLCopen-
conformant ST compiler accepts the IEC 3rd-edition syntax) but
their PLCopen XML emission is incomplete -- methods and
interface declarations don't appear in the ``<types>`` block.
A v2.02+ schema upgrade is queued as a follow-up; until then,
the ST emitter is the conformant-output path for OOP IL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .ast import Rung, Var
    from .st import Statement
    from .types import DataType


class AccessSpec(Enum):
    """IEC 61131-3 3rd edition access specifiers for methods.

    PUBLIC      visible to anyone with a reference to the instance
    PRIVATE     visible only within the declaring FUNCTION_BLOCK
    PROTECTED   visible within the FB and its descendants (EXTENDS chain)
    INTERNAL    visible within the same configuration / resource scope

    PUBLIC is the default and matches the pre-3rd-edition semantics
    where every method was implicitly public.
    """
    PUBLIC    = "PUBLIC"
    PRIVATE   = "PRIVATE"
    PROTECTED = "PROTECTED"
    INTERNAL  = "INTERNAL"


@dataclass
class Method:
    """A method declared inside a FUNCTION_BLOCK or an INTERFACE.

    Structurally similar to a FUNCTION (typed parameter interface +
    optional return value + body) but with three differences:

      1. Lives inside an FB or Interface, not at program scope.
      2. Has access to its FB's state (instance vars) without
         needing them in the parameter list.
      3. May be ABSTRACT (no body; subclasses must OVERRIDE) or
         OVERRIDE (provides an implementation for a parent's
         abstract or concrete method).

    Inside an ``Interface``, every ``Method`` should have
    ``abstract=True`` and an empty ``rungs`` list -- the interface
    declares only signatures.  The validator enforces this.

    Inside a ``FUNCTION_BLOCK`` (``Subroutine.methods``), methods
    may be concrete (``abstract=False``, body in ``rungs``) or
    abstract (in which case the enclosing FB must also be marked
    ``abstract=True``).
    """
    name: str
    rungs: list["Rung"] = field(default_factory=list)
    inputs:     list["Var"] = field(default_factory=list)
    outputs:    list["Var"] = field(default_factory=list)
    in_outs:    list["Var"] = field(default_factory=list)
    local_vars: list["Var"] = field(default_factory=list)
    return_type: Optional["DataType"] = None
    access: AccessSpec = AccessSpec.PUBLIC
    abstract: bool = False
    override: bool = False
    comment: str = ""
    st_body: Optional[list["Statement"]] = None

    def is_signature_only(self) -> bool:
        """True iff this method has no body (abstract / interface decl)."""
        if self.abstract:
            return True
        return not self.rungs and not self.st_body


@dataclass
class Interface:
    """IEC 61131-3 3rd edition INTERFACE.

    An abstract contract of method signatures that
    ``FUNCTION_BLOCK``s declare they ``IMPLEMENT``.  Used for
    polymorphism / dependency inversion -- callers can hold a
    reference typed as an interface and dispatch to whatever
    concrete FB implements it.

    Every method in an Interface must be abstract (``abstract=True``);
    the validator enforces this.  Concrete bodies live in the
    implementing FBs.

    Multiple inheritance for interfaces is allowed by IEC and by
    our model (``Subroutine.implements`` is a list); single
    inheritance for FBs (``Subroutine.extends`` is a single name).
    """
    name: str
    methods: list[Method] = field(default_factory=list)
    comment: str = ""

    def find_method(self, name: str) -> Optional[Method]:
        for m in self.methods:
            if m.name == name:
                return m
        return None
