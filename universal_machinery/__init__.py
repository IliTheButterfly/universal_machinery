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

__all__ = [
    "il",
    "Backend",
    "UniversalMachineryError",
    "LoweringError",
    "RoundTripError",
]
__version__ = "0.1.0"
