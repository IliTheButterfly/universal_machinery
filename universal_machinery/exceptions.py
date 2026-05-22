"""Structured exception hierarchy for ``universal_machinery``.

A single base class -- ``UniversalMachineryError`` -- lets CLI and
GUI consumers catch any library-originated failure in one place
without enumerating every concrete exception type::

    try:
        program = parse_plcopen_xml(xml)
        emit_program(program)
    except UniversalMachineryError as exc:
        # surface to the user, exit gracefully, etc.
        ...

Concrete exceptions defined elsewhere in the library inherit from
this base.  This module also owns the few exception types that
don't have a natural module home -- notably ``LoweringError``
(formerly duplicated in ``lowering/click_calling.py`` and
``lowering/fbd_to_st.py``) and ``RoundTripError`` (raised when
IL -> format -> IL re-parses to a different Program).

Validation diagnostics are a separate concern: ``ValidationError``
in ``universal_machinery.validation`` is a dataclass describing
*one validation issue*, not an exception.  ``validate()`` returns
``list[ValidationError]`` rather than raising, so callers can
report every issue at once.  See that module for details.
"""

from __future__ import annotations


class UniversalMachineryError(Exception):
    """Base class for every exception raised by ``universal_machinery``.

    Concrete subclasses live in their respective modules:
      - ``SerialisationError`` in ``universal_machinery.serialisation``
      - ``PlcopenParseError`` in ``universal_machinery.parsers.plcopen_xml``
      - ``StParseError`` in ``universal_machinery.parsers.st_text``
      - ``XMLSchemaError`` in ``universal_machinery.emitters.plcopen_xml``
      - ``UnsupportedOpError`` in ``universal_machinery.backends``
      - ``LoweringError`` (this module) -- shared by every lowering pass

    Inheriting from this base is strictly additive: existing
    ``except SerialisationError`` / etc. blocks keep working, but
    new code can also write ``except UniversalMachineryError``
    when it doesn't care which concrete failure happened.
    """


class LoweringError(UniversalMachineryError):
    """Raised when a lowering pass can't translate the IL to its
    target representation.

    Single source of truth for both the CLICK calling-convention
    lowering (``lowering/click_calling.py``) and the FBD -> ST
    lowering (``lowering/fbd_to_st.py``).  Both modules used to
    define their own ``LoweringError`` class, so ``except
    LoweringError`` only caught one of them depending on which
    module the name came from.  Importing here unifies them.

    Causes vary by pass:
      - CLICK: unknown call target, unknown formal-parameter name,
        instance binding on a non-FB target, return_to on an
        output-less POU, malformed reserved-region base address.
      - FBD -> ST: cycles in the wire graph, references to
        undeclared elements (validation should catch most of
        these before lowering runs).

    Surfaces as a compile-time diagnostic; never silently dropped.
    """


class RoundTripError(UniversalMachineryError):
    """Raised when an IL -> format -> IL round-trip lost information.

    Used by tooling that asserts loss-free round-trip as a
    correctness contract (the ``um diff`` workflow, future
    cert-grade audit harnesses).  The library itself doesn't
    raise this today -- it's the contract for callers that want
    to check the property.

    Carry the diff or summary in the exception message so the
    CLI / GUI can present it without the caller re-running the
    round-trip.
    """
