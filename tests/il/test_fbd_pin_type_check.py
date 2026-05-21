"""Type-checking tests for FBD ``<block>`` pin connections.

Last gap from the type-checker series: when an ``FbBlock`` in an
``FbdNetwork`` references a user-defined POU and its input pins
are wired to producers (other blocks or ``InVariable``
connectors), the validator checks that the producer's output
type is compatible with the consumer pin's expected type.

New error code: ``fbd-pin-type-mismatch``.

Builtins (TON / ADD / etc.) skip silently -- their pin
signatures are polymorphic / vendor-specific and need a
separate database (deferred follow-up).
"""
import pytest

from universal_machinery.builders import (
    fb, fb_block, fbd_network, fn, in_var, inout_var, out_var, pin, prog,
    program, tag_decl, var_in, var_inout, var_out,
)
from universal_machinery.il import TagType
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


# -----------------------------------------------------------------------------
# Clean networks
# -----------------------------------------------------------------------------


def test_clean_int_to_int_pin_passes():
    p = program(
        tags=[tag_decl("x", TagType.INT), tag_decl("y", TagType.INT)],
        subroutines=[
            fb("Process",
               inputs=[var_in("value", TagType.INT)],
               outputs=[var_out("result", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "x"),
                fb_block(1, "Process",
                         inputs=[pin("value", source_id=0)],
                         outputs=[pin("result")]),
                out_var(2, "y", source_id=1, source_pin="result"),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_compatible_buckets_pass():
    """UINT producer -> INT consumer is within the integer
    family bucket, no mismatch."""
    p = program(
        tags=[tag_decl("u", TagType.UINT), tag_decl("y", TagType.INT)],
        subroutines=[
            fb("Worker",
               inputs=[var_in("v", TagType.INT)],
               outputs=[var_out("r", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "u"),
                fb_block(1, "Worker",
                         inputs=[pin("v", source_id=0)],
                         outputs=[pin("r")]),
                out_var(2, "y", source_id=1, source_pin="r"),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_function_block_with_in_out_pin():
    """VAR_IN_OUT pins type-check the same way as VAR_INPUT."""
    p = program(
        tags=[tag_decl("counter", TagType.INT)],
        subroutines=[
            fb("Counter",
               in_outs=[var_inout("count", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "counter"),
                fb_block(1, "Counter",
                         in_outs=[pin("count", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Mismatch cases
# -----------------------------------------------------------------------------


def test_bool_to_int_pin_raises():
    """BOOL inVariable -> INT input pin on a user-defined FB."""
    p = program(
        tags=[tag_decl("flag", TagType.BOOL)],
        subroutines=[
            fb("Worker",
               inputs=[var_in("value", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "flag"),
                fb_block(1, "Worker",
                         inputs=[pin("value", source_id=0)]),
            )),
        ],
    )
    codes = _codes(p)
    assert "fbd-pin-type-mismatch" in codes
    err = next(e for e in validate(p)
                if e.code == "fbd-pin-type-mismatch")
    assert "Worker" in err.message
    assert "value" in err.message
    assert "INT" in err.message
    assert "BOOL" in err.message


def test_real_to_int_pin_raises():
    """REAL and INT live in different buckets per V1 type rules."""
    p = program(
        tags=[tag_decl("r", TagType.REAL)],
        subroutines=[
            fb("Worker",
               inputs=[var_in("value", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "r"),
                fb_block(1, "Worker",
                         inputs=[pin("value", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" in _codes(p)


def test_string_to_int_pin_raises():
    p = program(
        tags=[tag_decl("name", TagType.STRING)],
        subroutines=[
            fb("Worker",
               inputs=[var_in("value", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "name"),
                fb_block(1, "Worker",
                         inputs=[pin("value", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" in _codes(p)


# -----------------------------------------------------------------------------
# Chained blocks: producer is another user-defined POU's output
# -----------------------------------------------------------------------------


def test_chained_block_compatible_outputs_pass():
    """Worker outputs INT, Consumer expects INT.  Chain works."""
    p = program(
        tags=[tag_decl("x", TagType.INT), tag_decl("z", TagType.INT)],
        subroutines=[
            fb("Worker",
               inputs=[var_in("v", TagType.INT)],
               outputs=[var_out("r", TagType.INT)]),
            fb("Consumer",
               inputs=[var_in("data", TagType.INT)],
               outputs=[var_out("done", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "x"),
                fb_block(1, "Worker",
                         inputs=[pin("v", source_id=0)],
                         outputs=[pin("r")]),
                fb_block(2, "Consumer",
                         inputs=[pin("data",
                                       source_id=1, source_pin="r")],
                         outputs=[pin("done")]),
                out_var(3, "z", source_id=2, source_pin="done"),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_chained_block_mismatched_output_raises():
    """Worker outputs BOOL, Consumer expects INT -- the chain
    breaks at the Consumer's input pin."""
    p = program(
        tags=[tag_decl("x", TagType.INT)],
        subroutines=[
            fb("Worker",
               inputs=[var_in("v", TagType.INT)],
               outputs=[var_out("r", TagType.BOOL)]),
            fb("Consumer",
               inputs=[var_in("data", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "x"),
                fb_block(1, "Worker",
                         inputs=[pin("v", source_id=0)],
                         outputs=[pin("r")]),
                fb_block(2, "Consumer",
                         inputs=[pin("data",
                                       source_id=1, source_pin="r")]),
            )),
        ],
    )
    codes = _codes(p)
    assert "fbd-pin-type-mismatch" in codes
    err = next(e for e in validate(p)
                if e.code == "fbd-pin-type-mismatch")
    assert "Consumer" in err.message
    assert "data" in err.message


# -----------------------------------------------------------------------------
# FUNCTION return-type acts as the implicit output
# -----------------------------------------------------------------------------


def test_function_return_type_drives_producer_pin():
    """A FUNCTION POU has no VAR_OUTPUT but does have a
    ``return_type``.  An FbBlock pointing to it should expose
    that return type as its producer pin."""
    p = program(
        tags=[tag_decl("x", TagType.INT), tag_decl("y", TagType.REAL)],
        subroutines=[
            fn("ToReal",
               inputs=[var_in("v", TagType.INT)],
               return_type=TagType.REAL),
            fb("Worker",
               inputs=[var_in("r", TagType.REAL)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "x"),
                fb_block(1, "ToReal",
                         inputs=[pin("v", source_id=0)]),
                fb_block(2, "Worker",
                         inputs=[pin("r", source_id=1)]),
            )),
        ],
    )
    # ToReal returns REAL, Worker expects REAL -- compatible
    assert "fbd-pin-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Builtin blocks skip silently (no signature database yet)
# -----------------------------------------------------------------------------


def test_builtin_block_consumer_skips():
    """Block typeName=``TON`` doesn't resolve to a declared
    Subroutine -- the consumer-side check is skipped to avoid
    false positives until a builtin signature database lands."""
    p = program(
        tags=[tag_decl("clk", TagType.INT)],   # wrong type
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "clk"),
                fb_block(1, "TON",
                         instance_name="t1",
                         inputs=[pin("IN", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_builtin_block_producer_skips():
    """Producer is a builtin block -- its output type isn't in
    Program.subroutines, so the consumer pin check skips
    silently rather than firing a false positive."""
    p = program(
        subroutines=[
            fb("Worker",
               inputs=[var_in("v", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                # No inVariable; TON is the producer
                fb_block(0, "TON",
                         instance_name="t1",
                         outputs=[pin("Q")]),
                fb_block(1, "Worker",
                         inputs=[pin("v",
                                       source_id=0,
                                       source_pin="Q")]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Unwired pin / unresolved name doesn't trigger false positive
# -----------------------------------------------------------------------------


def test_unwired_pin_skips():
    """Pin without a Connection -- the check has nothing to
    compare against, so it stays silent."""
    p = program(
        subroutines=[
            fb("Worker",
               inputs=[var_in("v", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                fb_block(0, "Worker",
                         inputs=[pin("v")]),  # no source_id
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_invariable_with_unresolved_name_skips():
    """``in_var(0, "missing_var")`` -- the variable lookup
    falls back to None, and the consumer pin check skips
    silently (structural unresolved-tagref would catch this
    separately for rung-op uses, but not FBD)."""
    p = program(
        subroutines=[
            fb("Worker",
               inputs=[var_in("v", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "missing_var"),
                fb_block(1, "Worker",
                         inputs=[pin("v", source_id=0)]),
            )),
        ],
    )
    # No fbd-pin-type-mismatch since the producer type
    # couldn't be determined.
    assert "fbd-pin-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Location annotation
# -----------------------------------------------------------------------------


def test_error_location_names_block_and_pou():
    p = program(
        tags=[tag_decl("flag", TagType.BOOL)],
        subroutines=[
            fb("Process",
               inputs=[var_in("value", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "flag"),
                fb_block(1, "Process",
                         inputs=[pin("value", source_id=0)]),
            )),
        ],
    )
    err = next(e for e in validate(p)
                if e.code == "fbd-pin-type-mismatch")
    assert "Main" in err.location
    assert "block 1" in err.location
