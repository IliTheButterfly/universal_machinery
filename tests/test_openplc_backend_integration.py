"""Integration tests for the ``openplc_backend`` submodule.

The submodule at ``backends/openplc/`` wires the parent project's
ST and PLCopen XML emitters into the ``Backend`` ABC and registers
itself as ``"openplc"``.  This module exercises the integration
end-to-end against representative IL programs so a parent emitter
regression that breaks the backend surfaces in the parent's
test suite, not just in the submodule's own CI.

Skip behaviour
--------------

If ``openplc_backend`` isn't installed (the package isn't pulled in
by the parent's dev extras yet, so dev workflows that don't
``pip install -e backends/openplc`` won't have it), the whole
module skips with an install hint.

Install hint
------------

::

    pip install -e ./backends/openplc

(no extras needed; the submodule's own deps don't include pytest
since this test runs from the parent's pytest invocation).
"""
from __future__ import annotations

import pytest

openplc_backend = pytest.importorskip(
    "openplc_backend",
    reason=(
        "openplc_backend not installed; install with "
        "``pip install -e ./backends/openplc`` to run this module"
    ),
)


def _representative_program():
    """A small program exercising LD + ST + FUNCTION + TON so the
    integration covers the headline IL surface in one shot."""
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


def test_openplc_backend_registered_via_side_effect_import():
    """Importing the package triggers ``@register('openplc')``, so
    ``get_backend('openplc')`` resolves it without a separate
    registration step."""
    from universal_machinery.backends import get_backend, registered_names
    assert "openplc" in registered_names()
    backend = get_backend("openplc")
    assert backend.name == "openplc"


def test_openplc_backend_write_xml_passes_parent_xsd_validation(tmp_path):
    """Headline integration: the backend's PLCopen XML output
    passes the parent's bundled TC6 v2.01 XSD validation.  This
    is the same XSD the parent's emitter tests run against, so a
    parent emit shape regression would break this test first."""
    from openplc_backend import OpenPlcBackend
    from universal_machinery.emitters.plcopen_xml import validate_plcopen_xml
    out = tmp_path / "prog.xml"
    OpenPlcBackend().write(_representative_program(), str(out))
    validate_plcopen_xml(out.read_text())


def test_openplc_backend_xml_round_trip_preserves_pou_set(tmp_path):
    """``backend.write -> backend.read`` over PLCopen XML preserves
    the IL's POU set.  Deeper structural fidelity is covered by
    ``tests/parsers/test_plcopen_xml_reader.py``; this pins that
    the backend's dispatch wiring doesn't truncate the program."""
    from openplc_backend import OpenPlcBackend
    backend = OpenPlcBackend()
    out = tmp_path / "prog.xml"
    src = _representative_program()
    backend.write(src, str(out))
    parsed = backend.read(str(out))
    assert sorted(s.name for s in parsed.subroutines) == \
        sorted(s.name for s in src.subroutines)
