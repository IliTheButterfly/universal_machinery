"""CLICK calling-convention lowering: slot allocator + caller marshalling.

This is the first slice of the lowering described in
``docs/click_calling_convention.md``.  It runs purely on the IL --
no CKP bytes, no backend dependency -- and is independently testable
against a ``Program`` AST.

What's here
-----------

  - ``LoweringConfig``: tunable region bases (DS9000 args, DS9100
    returns, DS9200 FB-instance pointers, etc.)
  - ``allocate_slots(prog)``: bump-allocates a non-overlapping
    ``slot_base`` per parameterized POU, an instance-pointer slot per
    declared FUNCTION_BLOCK, and a numeric POU id for the scheduler
    dispatcher (id 0 reserved for "idle").
  - ``marshal_call(call, alloc)``: expands one parameterized Call op
    into [Move..., bare Call, Move...] -- the explicit
    Move-Call-Move sequence the CLICK runtime executes.
  - ``lower_calls(prog)``: program-level pass that walks every rung
    and rewrites parameterized Calls in place, preserving each
    rung's contact-prefix guard.  Returns a *new* Program.

What's deferred (see ``docs/click_calling_convention.md`` §6)
-----------------------------------------------------------

  - Callee-body rewriting (the callee references the assigned slot
    addresses by hand for now; a future pass can rewrite symbolic
    VAR references)
  - Trampoline emission in Main + scheduler-state initialisation
  - Scheduled-call rewriter for subroutine-internal calls
  - DataBlock contiguous-layout pass
  - SFC lowering

Design choices (locked):
  - **Same-rung packing.**  Marshalling Moves go into the same rung
    as the original Call, so the rung's contact-prefix gates them
    uniformly.  CLICK accepts multiple outputs per rung.
  - **Var.address untouched.**  The allocation lives in
    ``SlotAllocation`` only.  Lowered IL stays structurally
    distinguishable from authoring IL.
  - **Unknown target raises.**  Parameterized Call to an unknown POU
    raises ``LoweringError`` immediately -- catches typos.  Bare
    Calls pass through unchanged (they may target hand-authored
    CLICK subroutines that the allocator never saw).
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Optional

from ..il import (
    Address, PouKind, Program, Rung, Subroutine, Var,
)
from ..il.ops import Call, Move


#: Reserved id meaning "no callee queued" in the scheduler trampoline.
IDLE_POU_ID = 0


class LoweringError(Exception):
    """Raised when the CLICK lowering can't be performed.

    Surfaces as a compile-time diagnostic; never silently dropped.
    Causes: unknown call target, unknown formal-parameter name,
    instance binding on a non-FB target, return_to on an
    output-less POU, malformed reserved-region base address.
    """


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LoweringConfig:
    """Address-region bases for the CLICK calling convention.

    Defaults match ``docs/click_calling_convention.md`` and target
    a C2-01CPU.  Override when targeting a project that already
    uses these ranges for user data.
    """
    arg_base:          Address = Address("DS9000")   # VAR_INPUT / IN_OUT in
    ret_base:          Address = Address("DS9100")   # VAR_OUTPUT / IN_OUT out
    fb_instance_base:  Address = Address("DS9200")   # FB-instance pointer table
    sched_base:        Address = Address("DS9800")   # scheduler work area
    pou_active_base:   Address = Address("C2000")    # POU-active flag bits
    step_active_base:  Address = Address("C2256")    # SFC step-active bits


# -----------------------------------------------------------------------------
# Allocation result types
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PouSlots:
    """Allocated slot map for a single parameterized POU.

    ``arg_slot[formal_name]``  -- absolute Address of the slot a caller
                                  writes to before issuing CALL
                                  (VAR_INPUT / VAR_IN_OUT direction).
    ``ret_slot[formal_name]``  -- absolute Address of the slot the
                                  caller reads from after CALL
                                  (VAR_OUTPUT / VAR_IN_OUT direction).
    ``output_order``           -- VAR_OUTPUT names in declaration order
                                  (used by ``Call.return_to`` to find the
                                  implicit first-output slot).
    ``slot_base``              -- offset relative to LoweringConfig.arg_base
                                  / ret_base.
    ``width``                  -- contiguous slots reserved (excludes Vars
                                  with manually-bound addresses).
    """
    pou_name: str
    slot_base: int
    width: int
    arg_slot: dict[str, Address]
    ret_slot: dict[str, Address]
    output_order: tuple[str, ...]


@dataclass(frozen=True)
class SlotAllocation:
    """Result of allocating slots across all POUs + FB instances in a Program.

    ``per_pou``           -- POU name -> PouSlots.  POUs with no formal
                             interface (PouKind.SUBROUTINE or any POU
                             with empty inputs/outputs/in_outs) do not
                             appear here.
    ``fb_instance_slot``  -- FUNCTION_BLOCK name -> index into the
                             FB-instance pointer table.  Each FB POU
                             gets exactly one slot; the caller writes
                             the instance DB's base address into it
                             immediately before CALL.
    ``pou_id``            -- every POU's numeric scheduler id.  Id 0 is
                             reserved (IDLE_POU_ID); assignments start
                             at 1 in declaration order.
    """
    config: LoweringConfig
    per_pou: dict[str, PouSlots]
    fb_instance_slot: dict[str, int]
    pou_id: dict[str, int]


# -----------------------------------------------------------------------------
# Address arithmetic
# -----------------------------------------------------------------------------


_ADDR_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _split_addr(addr: Address) -> tuple[str, int]:
    m = _ADDR_RE.match(addr.raw)
    if not m:
        raise LoweringError(
            f"not a CLICK-style address (expected <letters><digits>): {addr.raw!r}"
        )
    return m.group(1), int(m.group(2))


def _offset(base: Address, n: int) -> Address:
    prefix, base_n = _split_addr(base)
    return Address(f"{prefix}{base_n + n}")


# -----------------------------------------------------------------------------
# Allocation
# -----------------------------------------------------------------------------


def _allocate_pou(sub: Subroutine, slot_base: int,
                  config: LoweringConfig) -> tuple[PouSlots, int]:
    """Build the slot map for one parameterized POU.

    Vars with a manually-set ``Var.address`` bypass auto-allocation:
    that address is used verbatim and doesn't count toward ``width``.
    Vars with ``address=None`` consume one slot each (input side or
    output side as appropriate).
    """
    arg_slot: dict[str, Address] = {}
    ret_slot: dict[str, Address] = {}
    width = 0

    def assign_arg(v: Var, idx: int) -> None:
        nonlocal width
        if v.address is not None:
            arg_slot[v.name] = v.address
        else:
            arg_slot[v.name] = _offset(config.arg_base, slot_base + idx)
            width = max(width, idx + 1)

    def assign_ret(v: Var, idx: int) -> None:
        nonlocal width
        if v.address is not None:
            ret_slot[v.name] = v.address
        else:
            ret_slot[v.name] = _offset(config.ret_base, slot_base + idx)
            width = max(width, idx + 1)

    # Argument region: VAR_INPUT then VAR_IN_OUT
    for i, v in enumerate(sub.inputs):
        assign_arg(v, i)
    for j, v in enumerate(sub.in_outs):
        assign_arg(v, len(sub.inputs) + j)
    # Return region: VAR_OUTPUT then VAR_IN_OUT (parallel layout)
    for i, v in enumerate(sub.outputs):
        assign_ret(v, i)
    for j, v in enumerate(sub.in_outs):
        assign_ret(v, len(sub.outputs) + j)

    return PouSlots(
        pou_name=sub.name,
        slot_base=slot_base,
        width=width,
        arg_slot=arg_slot,
        ret_slot=ret_slot,
        output_order=tuple(v.name for v in sub.outputs),
    ), width


def allocate_slots(prog: Program,
                   config: Optional[LoweringConfig] = None) -> SlotAllocation:
    """Bump-allocate slot bases, POU ids, and FB-instance slots across a Program.

    Walks ``prog.subroutines`` in declaration order, assigning:

      - a numeric POU id to every Subroutine starting at 1 (id 0 is
        the scheduler's IDLE_POU_ID sentinel);
      - an FB-instance pointer-table slot to each POU of kind
        FUNCTION_BLOCK;
      - a non-overlapping argument/return slot base to each POU that
        declares any inputs / outputs / in_outs.

    POUs of kind SUBROUTINE with no formal interface get an id only.
    """
    if config is None:
        config = LoweringConfig()

    per_pou: dict[str, PouSlots] = {}
    fb_instance_slot: dict[str, int] = {}
    pou_id: dict[str, int] = {}

    next_slot = 0
    next_fb_slot = 0
    next_id = 1

    for sub in prog.subroutines:
        pou_id[sub.name] = next_id
        next_id += 1

        if sub.kind is PouKind.FUNCTION_BLOCK:
            fb_instance_slot[sub.name] = next_fb_slot
            next_fb_slot += 1

        if sub.inputs or sub.outputs or sub.in_outs:
            slots, consumed = _allocate_pou(sub, next_slot, config)
            per_pou[sub.name] = slots
            next_slot += consumed

    return SlotAllocation(
        config=config,
        per_pou=per_pou,
        fb_instance_slot=fb_instance_slot,
        pou_id=pou_id,
    )


# -----------------------------------------------------------------------------
# Caller-side marshalling
# -----------------------------------------------------------------------------


def _is_parameterized(call: Call) -> bool:
    return bool(call.inputs or call.outputs
                or call.instance is not None
                or call.return_to is not None)


def marshal_call(call: Call, alloc: SlotAllocation) -> list[object]:
    """Expand a single ``Call`` op into ``[Move..., bare Call, Move...]``.

    Layout per ``docs/click_calling_convention.md`` §2:

      1. One ``Move(src, F.input_slot(name))`` per ``call.inputs``.
      2. (FB only) ``Move(instance, FBINST + F.instance_slot)``.
      3. The bare CLICK ``Call(target=F.name)``.
      4. One ``Move(F.output_slot(name), dst)`` per ``call.outputs``.
      5. (FUNCTION only) ``Move(F.first_output_slot, return_to)``.

    Bare unparameterized Calls pass through unchanged (a one-element
    list).  Parameterized Calls require ``call.target`` in
    ``alloc.per_pou``; unknown targets raise ``LoweringError``.  Each
    binding's formal-parameter name must appear in the callee's slot
    map; unknown names raise too.

    ``TagRef`` and string-literal sources in ``call.inputs`` are
    forwarded verbatim into ``Move.src`` -- a downstream tag resolver
    binds TagRefs to concrete addresses after this pass.
    """
    if not _is_parameterized(call):
        return [call]

    callee = alloc.per_pou.get(call.target)
    if callee is None:
        raise LoweringError(
            f"Call to unknown target {call.target!r} "
            f"(no slot allocation; known: {sorted(alloc.per_pou)})"
        )

    out: list[object] = []

    # 1. Marshal VAR_INPUT / VAR_IN_OUT inputs
    for name, src in call.inputs:
        dst = callee.arg_slot.get(name)
        if dst is None:
            raise LoweringError(
                f"POU {call.target!r} has no VAR_INPUT/VAR_IN_OUT named "
                f"{name!r} (known: {sorted(callee.arg_slot)})"
            )
        out.append(Move(src=src, dst=dst))

    # 2. Bind FB instance
    if call.instance is not None:
        fb_slot = alloc.fb_instance_slot.get(call.target)
        if fb_slot is None:
            raise LoweringError(
                f"Call sets instance={call.instance!r} but target "
                f"{call.target!r} is not a FUNCTION_BLOCK"
            )
        out.append(Move(
            src=call.instance,
            dst=_offset(alloc.config.fb_instance_base, fb_slot),
        ))

    # 3. The bare CLICK CALL
    out.append(Call(target=call.target))

    # 4. Demarshal VAR_OUTPUT / VAR_IN_OUT outputs
    for name, dst in call.outputs:
        src = callee.ret_slot.get(name)
        if src is None:
            raise LoweringError(
                f"POU {call.target!r} has no VAR_OUTPUT/VAR_IN_OUT named "
                f"{name!r} (known: {sorted(callee.ret_slot)})"
            )
        out.append(Move(src=src, dst=dst))

    # 5. FUNCTION return value -- implicit, first VAR_OUTPUT slot
    if call.return_to is not None:
        if not callee.output_order:
            raise LoweringError(
                f"Call sets return_to={call.return_to!r} but target "
                f"{call.target!r} declares no VAR_OUTPUT"
            )
        first_out = callee.output_order[0]
        out.append(Move(
            src=callee.ret_slot[first_out],
            dst=call.return_to,
        ))

    return out


# -----------------------------------------------------------------------------
# Program-level pass
# -----------------------------------------------------------------------------


def lower_calls(prog: Program,
                config: Optional[LoweringConfig] = None,
                alloc: Optional[SlotAllocation] = None,
                ) -> tuple[Program, SlotAllocation]:
    """Rewrite every parameterized ``Call`` in ``prog`` into its
    Move/Call/Move expansion.

    Each rung's contact prefix is preserved unchanged; the
    parameterized Call op is replaced *in place* within the rung's
    op list by the expansion (per the "same-rung packing" design
    decision).  Bare Calls and non-Call ops pass through.

    Returns a new ``Program`` (the input is not mutated) plus the
    ``SlotAllocation`` that was used.  Callers can pass in a
    pre-computed allocation; otherwise one is built on the fly.

    Note: this lowers the *caller* side.  Callee bodies are
    unchanged -- their authors are expected to read inputs from /
    write outputs to the slot addresses published in
    ``SlotAllocation``.  A future pass will rewrite callee bodies
    to use those slots automatically.
    """
    if alloc is None:
        alloc = allocate_slots(prog, config)

    new_subs: list[Subroutine] = []
    for sub in prog.subroutines:
        new_rungs: list[Rung] = []
        for rung in sub.rungs:
            new_ops: list[object] = []
            for op in rung.ops:
                if isinstance(op, Call):
                    new_ops.extend(marshal_call(op, alloc))
                else:
                    new_ops.append(op)
            new_rungs.append(Rung(ops=new_ops, comment=rung.comment))
        new_subs.append(dataclasses.replace(sub, rungs=new_rungs))

    return dataclasses.replace(prog, subroutines=new_subs), alloc
