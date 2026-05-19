"""Vendor-specific lowering passes operating on the IL.

Each module lowers a vendor-neutral ``il.Program`` into a form a
specific backend can emit directly.  Passes are pure functions:
``Program -> Program`` plus side-information (slot maps, allocations).

Modules
-------

``click_calling``
    First two passes of the CLICK calling convention from
    ``docs/click_calling_convention.md``: slot allocator + caller-side
    marshalling.  Sufficient to rewrite parameterized Calls into
    Move/Call/Move sequences for FUNCTIONs, FUNCTION_BLOCKs, and
    parameterized PROGRAMs.  The scheduler trampoline and the
    inside-subroutine-call rewriter are still TODO.

``fbd_to_st``
    Topological-sort-based lowering of a ``FbdNetwork`` to an
    equivalent list of ST ``Statement``s + temp ``Var``
    declarations.  Used as a fallback by backends that don't
    speak FBD natively, and by the ST emitter to render FBD
    bodies as real ST text instead of a marker comment.
"""
from . import click_calling, fbd_to_st
from .fbd_to_st import LoweringError, LoweringResult, lower_fbd_to_st

__all__ = [
    "LoweringError", "LoweringResult", "click_calling",
    "fbd_to_st", "lower_fbd_to_st",
]
