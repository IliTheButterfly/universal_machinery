"""Integration tests for the ``click_plc.ClickBackend`` submodule.

The submodule at ``backends/click/`` wires the ``Backend`` ABC for
the CLICK PLC vendor.  This module exercises the API-shape
integration end-to-end so a parent regression (Backend ABC change,
register/get_backend rework, etc.) that breaks the backend
surfaces here, not just in the submodule's own CI.

The actual ``.ckp`` encoder + ``CkpProject`` ↔ IL bridge are
pending roadmap items.  ``write()`` and ``read()`` raise
``NotImplementedError`` until they land; this module pins that
contract so future encoder PRs can change the assertion to a real
round-trip without removing the test.

Skip behaviour
--------------

If ``click_plc`` isn't installed (the package isn't pulled in by
the parent's dev extras yet), the whole module skips with an
install hint.  Same pattern as
``tests/test_openplc_backend_integration.py`` and
``tests/test_rusty_backend_integration.py``.

Install hint::

    pip install -e ./backends/click
"""
from __future__ import annotations

import pytest

click_plc = pytest.importorskip(
    "click_plc",
    reason=(
        "click_plc not installed; install with "
        "``pip install -e ./backends/click`` to run this module"
    ),
)


def test_click_backend_registered_via_side_effect_import():
    """Importing the package triggers ``@register('click')``; the
    backend is then resolvable via ``get_backend('click')`` -- no
    separate registration call needed."""
    from universal_machinery.backends import get_backend, registered_names
    assert "click" in registered_names()
    backend = get_backend("click")
    assert backend.name == "click"


def test_click_backend_advertises_lowering_capabilities():
    """``capabilities`` must reflect what the IL → CLICK lowering
    (``universal_machinery.lowering.click_calling``) covers.  This
    test compares against the ``openplc_backend`` / ``rusty_backend``
    capability sets to make the cross-backend asymmetries explicit
    -- consumers checking ``backend.supports(X)`` get a consistent
    answer regardless of which backend is in play.
    """
    from click_plc import ClickBackend
    rusty_only = {"methods", "interfaces", "extends",
                   "implements", "abstract"}
    matiec_only = {"sfc"}
    click_only: set[str] = {"data_blocks"}   # vendor-extension territory
    common_to_iec_backends = {
        "ld", "timers", "counters", "compare", "math",
        "call", "function_blocks", "jump", "parallel",
    }
    for cap in common_to_iec_backends:
        assert cap in ClickBackend.capabilities
    # CLICK has no OOP, no SFC text representation, no IEC FUNCTION
    # POU shape -- those are explicitly NOT advertised.
    for cap in rusty_only | matiec_only | {"functions", "st"}:
        assert cap not in ClickBackend.capabilities, (
            f"ClickBackend must not advertise {cap!r}"
        )
    # CLICK does carry data_blocks (the vendor-extension contiguous
    # DS region the IL → CLICK lowering already handles).
    for cap in click_only:
        assert cap in ClickBackend.capabilities


def test_click_backend_write_scaffold_raises_not_implemented(tmp_path):
    """Scaffold contract: ``write()`` raises ``NotImplementedError``
    until the .ckp encoder + ``CkpProject`` ↔ IL bridge land.  When
    those land, this test gets replaced with a real round-trip; the
    test name signals the transition."""
    from click_plc import ClickBackend
    from universal_machinery.builders import prog, program
    out = tmp_path / "prog.ckp"
    p = program(subroutines=[prog("Main", main=True)])
    with pytest.raises(NotImplementedError, match="encoder"):
        ClickBackend().write(p, str(out))


def test_click_backend_read_dispatches_through_ckp_to_il(tmp_path):
    """``ClickBackend.read`` decodes the bytes via ``decode_ckp``
    and translates the vendor-native ``CkpProject`` through
    ``ckp_to_il`` into an IL ``Program``.

    This is a parent-side integration check that the wiring
    survives across the submodule boundary; the byte-level
    decoder and the adapter itself have their own dedicated
    tests in the ``click_plc`` submodule.  Mocking the two halves
    at their source modules keeps this test fixture-free."""
    from unittest.mock import patch
    from click_plc import ClickBackend
    from universal_machinery.builders import prog, program
    out = tmp_path / "prog.ckp"
    out.write_bytes(b"any-bytes")
    expected = program(subroutines=[prog("Main", main=True)])
    with patch("click_plc.ckp_decoder.decode_ckp",
                 return_value="CkpProject-sentinel"), \
         patch("click_plc.ckp_to_il.ckp_to_il",
                 return_value=expected) as adapter:
        result = ClickBackend().read(str(out))
    adapter.assert_called_once_with("CkpProject-sentinel")
    assert result is expected


def test_three_backends_registered_after_imports():
    """End-to-end view of the registry after all three backend
    packages are imported: ``click`` / ``openplc`` / ``rusty`` all
    resolvable.  Pins the project's vendor-neutral claim --
    multiple backends targeting the same IL surface."""
    import click_plc       # noqa: F401  (registers as 'click')
    import openplc_backend  # noqa: F401  (registers as 'openplc')
    import rusty_backend    # noqa: F401  (registers as 'rusty')
    from universal_machinery.backends import registered_names
    names = registered_names()
    assert "click" in names
    assert "openplc" in names
    assert "rusty" in names
