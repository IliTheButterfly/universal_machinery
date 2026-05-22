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
# Builtin blocks use the bundled signature database in
# ``_BUILTIN_BLOCK_PIN_TYPES``: concrete pin types check against
# the producer / consumer's resolved type; polymorphic pins (e.g.
# ADD.IN1, MUX.IN0..N) return ``None`` and the check skips them.
# -----------------------------------------------------------------------------


def test_builtin_block_consumer_catches_concrete_pin_mismatch():
    """``TON.IN`` expects ``BOOL``; wiring an ``INT`` variable
    into it should fire ``fbd-pin-type-mismatch`` via the
    builtin signature database."""
    p = program(
        tags=[tag_decl("clk", TagType.INT)],
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "clk"),
                fb_block(1, "TON",
                         instance_name="t1",
                         inputs=[pin("IN", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" in _codes(p)


def test_builtin_block_producer_catches_concrete_pin_mismatch():
    """``TON.Q`` outputs ``BOOL``; piping it into a consumer
    whose pin expects ``INT`` should fire ``fbd-pin-type-mismatch``."""
    p = program(
        subroutines=[
            fb("Worker",
               inputs=[var_in("v", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
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
    assert "fbd-pin-type-mismatch" in _codes(p)


def test_builtin_block_correct_types_no_error():
    """Wiring a BOOL inVariable into ``TON.IN`` and a TIME literal
    into ``TON.PT`` (via a TagRef of type TIME) is type-correct
    and produces no error."""
    p = program(
        tags=[
            tag_decl("clk", TagType.BOOL),
            tag_decl("preset", TagType.TIME),
        ],
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "clk"),
                in_var(1, "preset"),
                fb_block(2, "TON",
                         instance_name="t1",
                         inputs=[pin("IN", source_id=0),
                                   pin("PT", source_id=1)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_polymorphic_builtin_pin_skips_check():
    """``ADD.IN1`` is polymorphic across numeric types -- the
    signature stores ``None`` for that pin so the check skips it
    even when the producer's type is something unusual."""
    p = program(
        tags=[tag_decl("clk", TagType.BOOL)],
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "clk"),
                fb_block(1, "ADD",
                         inputs=[pin("IN1", source_id=0)]),
            )),
        ],
    )
    # ADD isn't in our signature database (we only carry the
    # comparison family + stateful FBs there).  Even if it were,
    # IN1/IN2 are polymorphic and would skip.
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


# -----------------------------------------------------------------------------
# Extended builtin signatures: §2.5.2.8 selection (SEL.G), §2.5.2.9
# string functions (LEN's INT output, FIND's INT output), MOVE.
# -----------------------------------------------------------------------------


def test_sel_g_wired_to_int_fires_mismatch():
    """``SEL.G`` is the boolean selector; wiring an INT into it
    is a type error caught by the builtin signature DB."""
    p = program(
        tags=[tag_decl("selector_int", TagType.INT)],
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "selector_int"),
                fb_block(1, "SEL",
                         inputs=[pin("G", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" in _codes(p)


def test_sel_g_wired_to_bool_no_error():
    p = program(
        tags=[tag_decl("ok_flag", TagType.BOOL)],
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "ok_flag"),
                fb_block(1, "SEL",
                         inputs=[pin("G", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_len_output_into_int_consumer_no_error():
    """``LEN`` returns INT.  Wiring the OUT pin into another
    block's INT-typed input is type-correct."""
    p = program(
        subroutines=[
            fb("Worker",
               inputs=[var_in("count", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                fb_block(0, "LEN",
                         outputs=[pin("OUT")]),
                fb_block(1, "Worker",
                         inputs=[pin("count",
                                       source_id=0,
                                       source_pin="OUT")]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_len_output_into_bool_consumer_fires_mismatch():
    """``LEN`` returns INT; piping it into a BOOL-typed input
    is a type error."""
    p = program(
        subroutines=[
            fb("Worker",
               inputs=[var_in("flag", TagType.BOOL)]),
            prog("Main", main=True, fbd_body=fbd_network(
                fb_block(0, "LEN",
                         outputs=[pin("OUT")]),
                fb_block(1, "Worker",
                         inputs=[pin("flag",
                                       source_id=0,
                                       source_pin="OUT")]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" in _codes(p)


def test_find_output_into_int_consumer_no_error():
    p = program(
        subroutines=[
            fb("Worker",
               inputs=[var_in("pos", TagType.INT)]),
            prog("Main", main=True, fbd_body=fbd_network(
                fb_block(0, "FIND",
                         outputs=[pin("OUT")]),
                fb_block(1, "Worker",
                         inputs=[pin("pos",
                                       source_id=0,
                                       source_pin="OUT")]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_string_function_polymorphic_inputs_skip_check():
    """``LEFT.IN`` is polymorphic over STRING / WSTRING -- the
    signature stores ``None``, so wiring an INT into it
    (semantically wrong but not caught) still produces no
    type-mismatch.  This test pins the polymorphic skip in
    place: catching it would require a more elaborate
    bucket model."""
    p = program(
        tags=[tag_decl("count_int", TagType.INT)],
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "count_int"),
                fb_block(1, "LEFT",
                         inputs=[pin("IN", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)


def test_move_pins_are_polymorphic_no_check():
    """``MOVE`` is the canonical polymorphic op.  Both pins
    are ``None`` in the signature DB so the check skips."""
    p = program(
        tags=[tag_decl("v", TagType.STRING)],
        subroutines=[
            prog("Main", main=True, fbd_body=fbd_network(
                in_var(0, "v"),
                fb_block(1, "MOVE",
                         inputs=[pin("IN", source_id=0)]),
            )),
        ],
    )
    assert "fbd-pin-type-mismatch" not in _codes(p)
