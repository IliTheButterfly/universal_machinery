"""Integration tests for the ``rusty_backend`` submodule.

The submodule at ``backends/rusty/`` wires the parent project's
ST emitter into the ``Backend`` ABC, registers itself as
``"rusty"``, and adds an IEC 3rd-edition-OOP capability set
that matiec rejects.  This module exercises the API-shape
integration end-to-end so a parent emitter regression that
breaks the backend surfaces here, not just in the submodule's
own CI.

Subprocess validation against the actual ``plc`` binary lives
in the submodule's ``tests/test_smoke.py``; this file stays
inside the IL surface so it can run on every parent CI invocation
without needing rusty installed.

Skip behaviour
--------------

If ``rusty_backend`` isn't installed (the package isn't pulled
in by the parent's dev extras yet), the whole module skips with
an install hint.

Install hint::

    pip install -e ./backends/rusty
"""
from __future__ import annotations

import pytest

rusty_backend = pytest.importorskip(
    "rusty_backend",
    reason=(
        "rusty_backend not installed; install with "
        "``pip install -e ./backends/rusty`` to run this module"
    ),
)


def _representative_program():
    """Same shape as the openplc integration's representative
    program -- LD + TON + FUNCTION -- so the two backends share
    a comparable test corpus."""
    from universal_machinery.builders import (
        assign, coil, fn, no, prog, program, rung, ton, var, var_in,
    )
    from universal_machinery.il import NamedType, TagType
    from universal_machinery.il.ast import Var, VarDirection
    return program(subroutines=[
        fn("Doubled",
           return_type=TagType.INT,
           inputs=[var_in("x", TagType.INT)],
           st_body=[assign("Doubled", "x")]),
        prog("Main", main=True,
             local_vars=[
                 var("trigger", TagType.BOOL),
                 var("done", TagType.BOOL),
                 Var(name="t1", data_type=NamedType("TON"),
                     direction=VarDirection.LOCAL),
             ],
             rungs=[
                 rung(no("trigger"),
                       ton("t1", 1000, done_bit="done")),
                 rung(no("done"), coil("done")),
             ]),
    ])


def test_rusty_backend_registered_via_side_effect_import():
    """Importing the package triggers ``@register('rusty')``, so
    ``get_backend('rusty')`` resolves it without a separate
    registration step."""
    from universal_machinery.backends import get_backend, registered_names
    assert "rusty" in registered_names()
    backend = get_backend("rusty")
    assert backend.name == "rusty"


def test_rusty_backend_advertises_oop_capabilities():
    """The headline difference vs openplc / matiec: rusty advertises
    the IEC 3rd-edition OOP capabilities (``methods`` / ``interfaces``
    / ``extends`` / ``implements`` / ``abstract``).  Consumers
    checking ``backend.supports('methods')`` should get ``True``
    from rusty and ``False`` from openplc."""
    from openplc_backend import OpenPlcBackend
    from rusty_backend import RustyBackend
    rusty = RustyBackend()
    openplc = OpenPlcBackend()
    for cap in ("methods", "interfaces", "extends",
                "implements", "abstract"):
        assert rusty.supports(cap), (
            f"rusty must advertise {cap!r} (it's the whole point of "
            f"the backend vs openplc/matiec)"
        )
        assert not openplc.supports(cap), (
            f"openplc must NOT advertise {cap!r} -- matiec rejects "
            f"this construct at the parser level"
        )


def test_rusty_backend_write_st_emits_iec_3_text(tmp_path):
    """``rusty.write(program, '*.st')`` writes the same IEC §3 ST
    the parent's emitter produces.  The submodule's own tests
    validate that this ST compiles through ``plc``; here we just
    pin that the dispatch produces non-empty ST with the expected
    POU headers."""
    from rusty_backend import RustyBackend
    out = tmp_path / "prog.st"
    RustyBackend().write(_representative_program(), str(out))
    text = out.read_text()
    assert "PROGRAM Main" in text
    assert "FUNCTION Doubled" in text
    assert "END_PROGRAM" in text
    assert "END_FUNCTION" in text


def test_rusty_backend_rejects_xml_with_pointer_to_openplc(tmp_path):
    """rusty's input language is ST, not XML.  ``.xml`` writes
    raise ``ValueError`` with a pointer to the openplc backend
    (which DOES emit XML) rather than silently producing wrong
    output."""
    from rusty_backend import RustyBackend
    out = tmp_path / "prog.xml"
    with pytest.raises(ValueError, match="openplc"):
        RustyBackend().write(_representative_program(), str(out))
