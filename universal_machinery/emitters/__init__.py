"""Emitters: serialise the IL into external text / binary formats.

Lowering passes (``universal_machinery.lowering``) operate IL -> IL.
Emitters operate IL -> bytes/text destined for a target tool.

Modules
-------

``st``
    IEC 61131-3 §3 Structured Text.  Stateless, no instance management
    -- the text is what a vendor compiler would consume after its own
    parse pass.  Used standalone for code inspection and as the body
    payload for the PLCopen TC6 XML emitter.

``plcopen_xml``
    IEC 61131-3 / PLCopen TC6 XML interchange format.  Wraps the ST
    emitter's output inside the TC6 schema -- the deliverable for any
    PLCopen conformance claim.
"""
from . import plcopen_xml, st

__all__ = ["plcopen_xml", "st"]
