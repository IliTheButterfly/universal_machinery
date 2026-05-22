"""Backend ABC for vendor PLC file-format adapters.

A backend converts between a vendor's proprietary on-disk representation
(e.g., CLICK's .ckp, OpenPLC's PLCopen XML) and the vendor-neutral
``universal_machinery.il.Program`` AST.

Implementing a new backend
==========================

Create a Python package (typically as a git submodule under
``backends/<vendor>/``) that exposes a class subclassing ``Backend``::

    from universal_machinery.backends import Backend, register
    from universal_machinery.il import Program

    @register("myvendor")
    class MyVendorBackend(Backend):
        name = "myvendor"
        capabilities = frozenset({"ld", "st"})

        def read(self, path: str) -> Program:
            ...

        def write(self, program: Program, path: str) -> None:
            ...

Once the package is imported, ``get_backend("myvendor")`` returns it.

Capabilities
------------

A backend declares which IL features it can faithfully round-trip.  The
caller can check ``b.supports(op)`` before lowering to avoid surprises.
The standard capability strings are:

  ``"ld"``                Ladder Diagram (any LD op)
  ``"st"``                Structured Text output
  ``"timers"``            TON/TOF/TP
  ``"counters"``          CTU/CTD/CTUD
  ``"compare"``           Eq/Ne/Lt/Le/Gt/Ge
  ``"math"``              Move/BinaryMath
  ``"call"``              Bare subroutine calls (no params)
  ``"functions"``         FUNCTION POUs with VAR_INPUT/OUTPUT + return
  ``"function_blocks"``   FUNCTION_BLOCK POUs with per-instance state
  ``"data_blocks"``       Standalone DataBlock declarations
  ``"sfc"``               Sequential Function Chart (grafcet) bodies
  ``"nested_calls"``      Calls inside a callee body (CLICK lacks this)
  ``"jump"``              Intra-rung Jump/Label
  ``"parallel"``          ParallelGroup (OR branches in LD)

Backends not yet declaring a capability should raise
``UnsupportedOpError`` rather than silently emit broken output.
"""
from __future__ import annotations

import abc
from typing import Callable, ClassVar, TypeVar

from universal_machinery.exceptions import UniversalMachineryError
from universal_machinery.il import Program


class UnsupportedOpError(UniversalMachineryError):
    """Raised by a backend when it cannot lower a given IL op."""


class Backend(abc.ABC):
    """Read/write PLC programs for a specific vendor or runtime."""

    #: Short identifier (e.g. "click", "openplc").  Must be unique.
    name: ClassVar[str] = ""

    #: Set of capability strings.  See module docstring for the standard set.
    capabilities: ClassVar[frozenset[str]] = frozenset()

    @abc.abstractmethod
    def read(self, path: str) -> Program:
        """Parse the file at *path* and return a Program."""

    @abc.abstractmethod
    def write(self, program: Program, path: str) -> None:
        """Lower a Program and write it to *path*."""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------

_T = TypeVar("_T", bound=type[Backend])
_REGISTRY: dict[str, type[Backend]] = {}


def register(name: str) -> Callable[[_T], _T]:
    """Class decorator to register a backend under a given name.

    Usage::

        @register("click")
        class ClickBackend(Backend):
            ...
    """

    def deco(cls: _T) -> _T:
        if name in _REGISTRY:
            raise ValueError(f"backend {name!r} already registered as {_REGISTRY[name]!r}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return deco


def get_backend(name: str) -> Backend:
    """Instantiate the backend registered under *name*."""
    try:
        cls = _REGISTRY[name]
    except KeyError as e:
        raise KeyError(
            f"no backend registered for {name!r}; "
            f"known: {sorted(_REGISTRY)}.  Did you import the backend package?"
        ) from e
    return cls()


def registered_names() -> list[str]:
    return sorted(_REGISTRY)
