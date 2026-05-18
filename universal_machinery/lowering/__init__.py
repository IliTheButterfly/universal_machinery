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
"""
from . import click_calling

__all__ = ["click_calling"]
