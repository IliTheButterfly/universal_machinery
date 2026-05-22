"""universal_machinery -- open-source PLC programming toolkit.

The big idea: a single vendor-neutral intermediate language (`il`) plus
a registry of vendor backends that read/write each PLC manufacturer's
proprietary file format.  Write your control logic once; deploy to any
supported PLC.

  - Read CKP project files from CLICK PLC (AutomationDirect) ──┐
  - Read PLCopen XML / Structured Text from OpenPLC  ─────────┤
                                                              │
                                                              ▼
                                              universal_machinery.il
                                                  (vendor-neutral
                                                   ladder-logic AST)
                                                              │
                                                              ▼
  - Write CKP for CLICK ────────────────────────────────────┐ │
  - Write PLCopen XML / ST for OpenPLC  ──────────────────┐ │ │
                                                          │ │ │
                                                          ▼ ▼ ▼
                                                     deployable program

Status: ALPHA.  CLICK read+write works end-to-end via the click_plc
backend.  IL design is complete enough to round-trip CLICK programs.
OpenPLC backend dispatches to the parent's IEC ST / PLCopen XML
emitters and is registered via ``@register("openplc")``.

See ``docs/ARCHITECTURE.md`` for the design and ``backends/<vendor>/``
for each backend's status.
"""
from . import il
from .backends import Backend
from .exceptions import (
    LoweringError,
    RoundTripError,
    UniversalMachineryError,
)

#: The documented Stable / Backend-author surface at the top level.
#: Submodules (``builders`` / ``emitters`` / ``parsers`` / ``validation`` /
#: ``serialisation`` / ``cli`` / ``lowering``) are reachable via
#: ``from universal_machinery import <name>`` without an explicit eager
#: import here -- listing them in ``__all__`` keeps the contract
#: explicit without paying their import-time cost on every
#: ``import universal_machinery``.  ``docs/API_STABILITY.md`` is the
#: source of truth for what each entry promises.
__all__ = [
    # Eager subpackage (cheap, no third-party deps)
    "il",
    # Lazy subpackages -- reachable via ``from universal_machinery
    # import X``; not eagerly imported (typer/rich/etc. would slow
    # the parent's start-up needlessly when callers only need the IL).
    "builders",
    "emitters",
    "parsers",
    "validation",
    "serialisation",
    "exceptions",
    "backends",
    "cli",
    "lowering",
    # Backend ABC + exception hierarchy at the top level so consumers
    # don't need to remember sub-paths for the catch-all names.
    "Backend",
    "UniversalMachineryError",
    "LoweringError",
    "RoundTripError",
]
__version__ = "0.1.0"
