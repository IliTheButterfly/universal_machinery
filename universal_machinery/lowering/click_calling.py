"""CLICK calling-convention lowering: slot allocator, caller-side
marshalling, and callee-body symbolic-reference resolution.

Implements the first three passes of the lowering described in
``docs/click_calling_convention.md``.  All passes run purely on the
IL -- no CKP bytes, no backend dependency.

What's here
-----------

  - ``LoweringConfig``: tunable region bases (DS9000 args, DS9100
    returns, DS9200 FB-instance pointers, etc.)
  - ``allocate_slots(prog)``: bump-allocates per-POU slot bases,
    FB-instance pointer-table slots, and scheduler POU ids (0 reserved
    for "idle").
  - ``marshal_call(call, alloc)``: expands one parameterized Call op
    into [Move..., bare Call, Move...] -- the explicit
    Move-Call-Move sequence the CLICK runtime executes.
  - ``lower_calls(prog)``: program-level pass that walks every rung
    and rewrites parameterized Calls in place, preserving each
    rung's contact-prefix guard.  Returns a *new* Program.
  - ``rewrite_callee_body(sub, alloc)``: rewrites a parameterized
    POU body's ``TagRef`` references to formal parameter names into
    the slot ``Address``es allocated for them.  Direction-aware:
    read positions (sources, contacts, compare operands) substitute
    against ``arg_slot``; write positions (sinks, coils, math
    destinations) substitute against ``ret_slot``.  IN_OUT
    parameters resolve correctly because they appear in both maps.
  - ``lower_pou_bodies(prog, alloc)``: program-level pass that
    applies ``rewrite_callee_body`` to every parameterized POU.

What's deferred (see ``docs/click_calling_convention.md`` §6)
-----------------------------------------------------------

  - Trampoline emission in Main + scheduler-state initialisation
  - Scheduled-call rewriter for subroutine-internal calls
  - DataBlock contiguous-layout pass
  - SFC lowering
  - VAR_LOCAL allocation (locals don't yet get auto-assigned slots;
    they're either explicitly addressed on the Var, or -- in
    FUNCTION_BLOCKs -- live in the instance DataBlock)

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
  - **TagRefs that don't name a formal parameter pass through
    unchanged.**  They may be references to global ``Program.tags``;
    a separate tag-resolver pass at the writer binds them later.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Optional

from ..il import (
    Address, PouKind, Program, Rung, Subroutine, TagRef, Var,
)
from ..il.ops import (
    BinaryMath, Call, Compare, ContactFallingEdge, ContactNC, ContactNO,
    ContactRisingEdge, CTD, CTU, CTUD, Jump, Label, Move, OutCoil, OutReset,
    OutSet, ParallelGroup, Return, TOF, TON, TP, VendorOp,
)


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
    uses these ranges for user data, or to size the scheduler stack
    (``sched_stack_depth``) for the program's worst-case nesting.

    Scheduler layout
    ----------------
    The scheduler state occupies a contiguous DS-register range
    starting at ``sched_next_id``, sized by ``sched_stack_depth``::

        sched_next_id        next POU id to dispatch (0 = idle)
        sched_stack_base..   depth slots: caller-id stack
        sched_sp             stack pointer (count, 0..depth)
        sched_resume_base..  depth slots: resume-id stack
        sched_resume_id      current resume id (written by trampoline,
                             read by POU body's resume-dispatch header)

    Plus one C-bit (``sched_yield_flag``) the POU's yield sequence
    sets before Return, telling Main's pop logic "the POU yielded;
    don't pop the frame I just pushed."

    Default layout with stack_depth=8 occupies DS9800..DS9818 (19
    registers) plus C1500 (1 bit).  Bumping stack_depth shifts the
    layout but keeps all bases at their nominal positions; callers
    overriding individual bases must keep them non-overlapping.
    """
    arg_base:          Address = Address("DS9000")   # VAR_INPUT / IN_OUT in
    ret_base:          Address = Address("DS9100")   # VAR_OUTPUT / IN_OUT out
    fb_instance_base:  Address = Address("DS9200")   # FB-instance pointer table

    # --- Scheduler state ---
    sched_next_id:     Address = Address("DS9800")   # next POU id to dispatch
    sched_stack_base:  Address = Address("DS9801")   # caller-id stack (depth slots)
    sched_sp:          Address = Address("DS9809")   # stack pointer
    sched_resume_base: Address = Address("DS9810")   # resume-id stack (depth slots)
    sched_resume_id:   Address = Address("DS9818")   # current resume id
    sched_yield_flag:  Address = Address("C1500")    # set by yield; cleared each scan
    sched_stack_depth: int     = 8                   # configurable per project

    # --- Activity bits ---
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


# -----------------------------------------------------------------------------
# Callee-body rewriter
# -----------------------------------------------------------------------------


def _bind(loc, table: dict[str, Address]):
    """If ``loc`` is a TagRef whose name is in ``table``, return the
    bound Address; otherwise pass through unchanged.  ``loc`` may be
    None, an Address, a TagRef, or a string literal -- only TagRefs
    matching a known name are rewritten."""
    if isinstance(loc, TagRef) and loc.name in table:
        return table[loc.name]
    return loc


def _rewrite_op_refs(op: object, reads: dict[str, Address],
                     writes: dict[str, Address]) -> object:
    """Return a copy of ``op`` with TagRef references resolved against
    the per-direction substitution tables.

    ``reads`` is the substitution table for *read* positions
    (contact addresses, ``src``, ``lhs`` / ``rhs``, ``cu_input`` /
    ``cd_input``, ``accumulator``, ``Call.instance``,
    ``Call.inputs`` sources); ``writes`` is for *write* positions
    (coil addresses, ``dst``, ``done_bit``, ``Call.outputs``
    destinations, ``Call.return_to``).

    For non-IN_OUT formal parameters this distinction is moot --
    they appear in exactly one of the maps.  For IN_OUT parameters,
    the same name has different bindings in each direction, which
    is precisely what the CLICK calling convention requires: the
    caller writes to the arg slot before CALL and reads from the
    ret slot after.
    """
    R, W = reads, writes   # local aliases

    if isinstance(op, (ContactNO, ContactNC, ContactRisingEdge,
                       ContactFallingEdge)):
        return dataclasses.replace(op, address=_bind(op.address, R))

    if isinstance(op, (OutCoil, OutSet, OutReset)):
        return dataclasses.replace(op, address=_bind(op.address, W))

    if isinstance(op, (TON, TOF, TP)):
        return dataclasses.replace(
            op,
            address=_bind(op.address, W),
            accumulator=_bind(op.accumulator, R) if op.accumulator else None,
            done_bit=_bind(op.done_bit, W) if op.done_bit else None,
        )

    if isinstance(op, (CTU, CTD)):
        gate = op.reset if isinstance(op, CTU) else op.load
        new_gate = _bind(gate, R) if gate is not None else None
        rebuilt = dataclasses.replace(
            op,
            address=_bind(op.address, W),
            accumulator=_bind(op.accumulator, R) if op.accumulator else None,
            done_bit=_bind(op.done_bit, W) if op.done_bit else None,
        )
        if isinstance(rebuilt, CTU):
            return dataclasses.replace(rebuilt, reset=new_gate)
        return dataclasses.replace(rebuilt, load=new_gate)

    if isinstance(op, CTUD):
        return dataclasses.replace(
            op,
            address=_bind(op.address, W),
            cu_input=_bind(op.cu_input, R),
            cd_input=_bind(op.cd_input, R),
            reset=_bind(op.reset, R) if op.reset else None,
            load=_bind(op.load, R) if op.load else None,
            accumulator=_bind(op.accumulator, R) if op.accumulator else None,
            qu=_bind(op.qu, W) if op.qu else None,
            qd=_bind(op.qd, W) if op.qd else None,
        )

    if isinstance(op, Compare):
        return dataclasses.replace(op,
                                   lhs=_bind(op.lhs, R),
                                   rhs=_bind(op.rhs, R))

    if isinstance(op, Move):
        return dataclasses.replace(op,
                                   src=_bind(op.src, R),
                                   dst=_bind(op.dst, W))

    if isinstance(op, BinaryMath):
        return dataclasses.replace(op,
                                   lhs=_bind(op.lhs, R),
                                   rhs=_bind(op.rhs, R),
                                   dst=_bind(op.dst, W))

    if isinstance(op, Call):
        # A Call inside a parameterized POU body is a NESTED call.  We
        # rewrite its argument bindings against the *current* POU's
        # slot maps (the nested call's source/destination operands may
        # refer to the current POU's formal parameters), but we leave
        # the Call op intact -- the scheduled-call rewriter (separate
        # pass, still TODO) is what transforms it into a yield/resume
        # form.  Caller-side marshalling for *that* call resolves its
        # own formal-parameter bindings against the *callee's* slot
        # maps, not the current POU's.
        return dataclasses.replace(
            op,
            inputs=tuple((name, _bind(src, R)) for name, src in op.inputs),
            outputs=tuple((name, _bind(dst, W)) for name, dst in op.outputs),
            instance=_bind(op.instance, R) if op.instance else None,
            return_to=_bind(op.return_to, W) if op.return_to else None,
        )

    if isinstance(op, ParallelGroup):
        return dataclasses.replace(
            op,
            branches=tuple(
                tuple(_rewrite_op_refs(inner, R, W) for inner in branch)
                for branch in op.branches
            ),
        )

    if isinstance(op, VendorOp):
        # VendorOp.addresses doesn't carry per-address direction info.
        # We rewrite each entry against the read table first, then the
        # write table -- which is correct for non-IN_OUT parameters
        # (the name appears in exactly one map) and a best-effort
        # fallback for IN_OUT (defaults to the read binding, which is
        # the more common case for a vendor block reading state).
        new_addrs = tuple(_bind(_bind(a, R), W) for a in op.addresses)
        return dataclasses.replace(op, addresses=new_addrs)

    # Return / End / Jump / Label have no Loc operands
    return op


def rewrite_callee_body(sub: Subroutine,
                        alloc: SlotAllocation) -> Subroutine:
    """Rewrite a POU body's TagRef references to formal parameter
    names into the slot Addresses allocated for them.

    Direction-aware: a ``TagRef("v")`` in a *read* position
    (contact, compare operand, ``Move.src``, ``BinaryMath.lhs`` /
    ``rhs``, ``Call.inputs`` source, ``Call.instance``) substitutes
    against the POU's ``arg_slot`` map; a ``TagRef("v")`` in a *write*
    position (coil, ``Move.dst``, ``BinaryMath.dst``, ``Call.outputs``
    destination, ``Call.return_to``) substitutes against
    ``ret_slot``.  This gets IN_OUT parameters right -- the same name
    binds to two different addresses depending on direction.

    TagRefs that don't name a formal parameter pass through
    unchanged -- they may be references to global ``Program.tags``,
    resolved by a separate writer-side tag-resolver pass.

    POUs without a slot allocation (kind == SUBROUTINE, or any POU
    with no inputs/outputs/in_outs) are returned untouched.

    Returns a new Subroutine; ``sub`` is not mutated.
    """
    pou_slots = alloc.per_pou.get(sub.name)
    if pou_slots is None:
        return sub

    reads = pou_slots.arg_slot
    writes = pou_slots.ret_slot

    new_rungs: list[Rung] = []
    for rung in sub.rungs:
        new_ops = [_rewrite_op_refs(op, reads, writes) for op in rung.ops]
        new_rungs.append(Rung(ops=new_ops, comment=rung.comment))

    return dataclasses.replace(sub, rungs=new_rungs)


def lower_pou_bodies(prog: Program,
                     config: Optional[LoweringConfig] = None,
                     alloc: Optional[SlotAllocation] = None,
                     ) -> tuple[Program, SlotAllocation]:
    """Apply ``rewrite_callee_body`` to every parameterized POU in
    ``prog``.

    Returns a new Program (input untouched) plus the
    ``SlotAllocation`` used.  Idempotent under repeated application:
    after one pass, no rung op holds a TagRef matching a formal
    parameter name, so a second pass is a no-op on those refs.
    """
    if alloc is None:
        alloc = allocate_slots(prog, config)

    new_subs = [rewrite_callee_body(sub, alloc) for sub in prog.subroutines]
    return dataclasses.replace(prog, subroutines=new_subs), alloc


# -----------------------------------------------------------------------------
# Trampoline + resume dispatch emitters
# -----------------------------------------------------------------------------
#
# Commit A of the scheduler: emit the trampoline that lives at the top of
# Main and the resume-dispatch header that each non-Main POU body needs.
# Commit B will add the scheduled-call rewriter that produces yield
# sequences inside non-Main POU bodies -- those sequences set
# ``sched_next_id``, push a frame, set ``sched_yield_flag``, and Return,
# at which point this trampoline takes over.
#
# Until commit B lands, no POU body yields, so the pop / resume paths
# below are inert (correct-but-unused).  Commit A is shaped so that
# arrives end-to-end ready: emit it now, exercise it later.


def emit_dispatch_rungs(alloc: SlotAllocation,
                        config: LoweringConfig) -> list[Rung]:
    """Emit Main's dispatch table -- one rung per non-Main POU.

    Each rung tests ``sched_next_id == k`` (the POU's id from
    ``alloc.pou_id``) and, on match, issues a CLICK-native CALL to
    that POU.  Within a single scan multiple dispatch rungs can fire
    in cascade: POU A yields to B by setting sched_next_id=B.id and
    returning; rung B then matches and dispatches B.

    The Main routine itself is excluded -- Main is the dispatcher,
    not a dispatched callee.  POUs are emitted in declaration order
    (whichever the ``alloc.pou_id`` walk yields).
    """
    rungs: list[Rung] = []
    for name, pou_id in alloc.pou_id.items():
        # Skip Main; it's the host of this trampoline.
        # ``alloc`` doesn't carry the main-flag, but the scheduler
        # never re-enters Main, so we filter by checking the parent
        # Program.  For now we leave the filter to the caller:
        # ``emit_trampoline`` knows which POU is Main.
        rungs.append(Rung(
            ops=[
                Compare(op="==", lhs=config.sched_next_id, rhs=str(pou_id)),
                Call(target=name),
            ],
            comment=f"dispatch: {name} (id {pou_id})",
        ))
    return rungs


def emit_pop_rungs(config: LoweringConfig) -> list[Rung]:
    """Emit the pop sequence that runs after Main's dispatch table.

    Pop only fires when the dispatched POU returned *normally* (i.e.
    did not yield): the POU's yield sequence sets ``sched_yield_flag``
    just before its Return, and the pop rungs are gated on
    ``ContactNC(sched_yield_flag)`` -- "if no yield happened."

    CLICK has no indirect-indexed register read, so the pop is
    expressed as a switch over the current ``sched_sp`` value: one
    rung per depth level, restoring caller id + resume id from the
    fixed slot at that depth.  The ``sp == 0`` case (stack empty)
    transitions ``sched_next_id`` to 0 (idle) and clears the resume
    register.

    The trailing rung resets ``sched_yield_flag`` so the next scan
    starts clean.
    """
    rungs: list[Rung] = []
    depth = config.sched_stack_depth

    for sp_now in range(depth, 0, -1):
        slot_index = sp_now - 1   # top-of-stack frame index
        rungs.append(Rung(
            ops=[
                ContactNC(config.sched_yield_flag),
                Compare(op="==", lhs=config.sched_sp, rhs=str(sp_now)),
                Move(src=_offset(config.sched_stack_base, slot_index),
                     dst=config.sched_next_id),
                Move(src=_offset(config.sched_resume_base, slot_index),
                     dst=config.sched_resume_id),
                Move(src=str(slot_index), dst=config.sched_sp),
            ],
            comment=f"pop: sp={sp_now} -> {slot_index}",
        ))

    rungs.append(Rung(
        ops=[
            ContactNC(config.sched_yield_flag),
            Compare(op="==", lhs=config.sched_sp, rhs="0"),
            Move(src="0", dst=config.sched_next_id),
            Move(src="0", dst=config.sched_resume_id),
        ],
        comment="pop: stack empty -> idle",
    ))

    rungs.append(Rung(
        ops=[OutReset(config.sched_yield_flag)],
        comment="clear yield flag for next scan",
    ))
    return rungs


def emit_trampoline(prog: Program, alloc: SlotAllocation,
                    config: Optional[LoweringConfig] = None) -> list[Rung]:
    """Assemble Main's full trampoline: dispatch table + pop sequence.

    The result is intended to be *prepended* to Main's existing rungs
    (see ``prepend_trampoline_to_main``).  Each scan, the trampoline
    runs first:

      1. Each dispatch rung tests ``sched_next_id == its POU id``;
         on match it issues the CLICK CALL.  POUs can cascade-yield
         to other POUs within the same scan (each yield re-targets
         ``sched_next_id`` and Returns; the next matching dispatch
         rung picks it up).
      2. The pop rungs run after the dispatch table.  If the most
         recently-dispatched POU did NOT set ``sched_yield_flag``,
         the pop restores the caller's id + resume id from the
         stack (or zeroes ``sched_next_id`` if the stack is empty).
      3. ``sched_yield_flag`` is reset for the next scan.

    Skips POUs whose name matches ``prog.main_subroutine()`` -- Main
    is the dispatcher and is never dispatched as a callee.
    """
    if config is None:
        config = alloc.config

    main = prog.main_subroutine()
    main_name = main.name if main is not None else None

    # Build dispatch rungs, filtering Main out.
    dispatch_rungs: list[Rung] = []
    for name, pou_id in alloc.pou_id.items():
        if name == main_name:
            continue
        dispatch_rungs.append(Rung(
            ops=[
                Compare(op="==", lhs=config.sched_next_id, rhs=str(pou_id)),
                Call(target=name),
            ],
            comment=f"dispatch: {name} (id {pou_id})",
        ))

    pop_rungs = emit_pop_rungs(config)
    return dispatch_rungs + pop_rungs


def prepend_trampoline_to_main(prog: Program, alloc: SlotAllocation,
                               config: Optional[LoweringConfig] = None,
                               ) -> Program:
    """Return a new Program whose Main subroutine has the trampoline
    rungs prepended to its existing body.

    Raises ``LoweringError`` if the program has no Main subroutine.
    The caller-side ``lower_calls`` pass and the callee-body
    ``lower_pou_bodies`` pass should typically run *before* this --
    the trampoline references concrete addresses, not symbolic
    TagRefs, so it's the same shape pre- or post-resolution.
    """
    if config is None:
        config = alloc.config

    main = prog.main_subroutine()
    if main is None:
        raise LoweringError(
            "Program has no main_subroutine; trampoline needs a Main "
            "to host the dispatch table"
        )

    trampoline = emit_trampoline(prog, alloc, config)
    new_main = dataclasses.replace(main, rungs=trampoline + main.rungs)

    new_subs = [new_main if s is main else s for s in prog.subroutines]
    return dataclasses.replace(prog, subroutines=new_subs)


def emit_resume_dispatch(resume_count: int,
                         config: LoweringConfig,
                         entry_label: str = "entry",
                         resume_label_prefix: str = "resume_",
                         ) -> list[Rung]:
    """Emit the resume-dispatch header for a non-Main POU body.

    The header runs at the top of every dispatch into the POU and
    decides whether this is a fresh entry (``sched_resume_id == 0``)
    or a resume after a nested call returned (``sched_resume_id ==
    N`` for the Nth resume site).

    ``resume_count`` is the number of distinct resume sites in the
    POU's body -- one per nested Call the scheduled-call rewriter
    (commit B) found.  POUs with no nested calls (``resume_count ==
    0``) need no resume header and this function returns an empty
    list; the POU body runs from the top every dispatch.

    Layout::

      Rung([Compare(resume_id == 0) -> Jump(entry)])
      Rung([Compare(resume_id == 1) -> Jump(resume_1)])
      ...
      Rung([Compare(resume_id == N) -> Jump(resume_N)])
      Rung([Label(entry)])     <-- body proper begins here

    The rewriter is responsible for emitting the matching
    ``Label(resume_K)`` rungs inside the body at each Call's
    continuation point.
    """
    if resume_count == 0:
        return []

    rungs: list[Rung] = [
        Rung(
            ops=[
                Compare(op="==", lhs=config.sched_resume_id, rhs="0"),
                Jump(label=entry_label),
            ],
            comment="resume dispatch: entry",
        ),
    ]
    for resume_id in range(1, resume_count + 1):
        rungs.append(Rung(
            ops=[
                Compare(op="==", lhs=config.sched_resume_id,
                        rhs=str(resume_id)),
                Jump(label=f"{resume_label_prefix}{resume_id}"),
            ],
            comment=f"resume dispatch: site {resume_id}",
        ))
    rungs.append(Rung(
        ops=[Label(name=entry_label)],
        comment="body entry",
    ))
    return rungs


# -----------------------------------------------------------------------------
# Scheduled-call rewriter (commit B)
# -----------------------------------------------------------------------------
#
# CLICK forbids any CALL inside a subroutine body, so every bare CLICK
# CALL that ``lower_calls`` placed inside a non-Main POU body must be
# transformed into a cooperative-yield sequence: marshal the inputs,
# push the caller's (id, resume_id) onto the scheduler stack, set
# sched_next_id to the callee's id, set the yield flag, and Return.
# The trampoline (commit A) picks up sched_next_id on a later
# dispatch cycle, runs the callee, then pops on the callee's return
# and re-dispatches the caller with sched_resume_id set so the
# caller's body resumes at the appropriate Label.
#
# Main is special.  Main is allowed to issue native CLICK CALLs, but
# only to leaf POUs (POUs whose bodies contain no further bare
# Calls).  Main's Call to a non-leaf POU is rewritten into a
# scheduler-dispatch sequence: set sched_next_id = target.id, fall
# through.  No push (Main is never on the scheduler stack); no yield
# flag (Main isn't suspending, it's initiating dispatch).


def _classify_leaves(prog: Program) -> set[str]:
    """Return the set of POU names that are "leaves" -- POUs whose
    body contains no bare CLICK ``Call`` ops.

    A leaf POU can be invoked from Main via a native CLICK CALL.  A
    non-leaf POU contains nested calls (after ``lower_calls`` runs)
    that the scheduler must mediate; invoking it from Main therefore
    has to go through the scheduler too.

    Run AFTER ``lower_calls`` -- by that point every parameterized
    Call has been expanded into Move-Call(bare)-Move, so "has a Call
    op anywhere in the body" is the right leaf-vs-non-leaf signal.
    """
    leaves: set[str] = set()
    for sub in prog.subroutines:
        has_call = any(
            isinstance(op, Call)
            for rung in sub.rungs
            for op in rung.ops
        )
        if not has_call:
            leaves.add(sub.name)
    return leaves


def _gate_prefix(ops: list) -> list:
    """Extract the leading contact / Compare prefix of a rung's ops.

    The "gate" of a rung is the contact-like input network that
    conducts to the rung's outputs.  When we split a rung at a Call,
    each emitted rung needs the gate re-applied so its outputs only
    fire under the same conditions as the original.

    Contact-like ops are: ContactNO/NC/RisingEdge/FallingEdge,
    Compare, ParallelGroup (which itself contains gate ops).
    """
    gate_types = (ContactNO, ContactNC, ContactRisingEdge,
                  ContactFallingEdge, Compare, ParallelGroup)
    prefix: list = []
    for op in ops:
        if isinstance(op, gate_types):
            prefix.append(op)
        else:
            break
    return prefix


def _emit_yield_rungs(gate: list, caller_id: int, resume_id: int,
                      callee_id: int, marshal_in: list,
                      config: LoweringConfig) -> list[Rung]:
    """Emit the yield rungs for one nested-call site.

    Layout (depth+1 rungs total for depth=N):

      Rung 0  : [gate, marshal_in...]                  -- runs unconditionally
      Rung 1..N: [gate, Compare(sp==k), push-at-k,    -- only one fires
                  Move(callee_id -> next_id),
                  Set(yield_flag), Return]

    CLICK lacks indirect indexed writes, so each push site is a
    switch over the current sp value: at runtime exactly one rung
    matches sp's current level and pushes at that index.  The other
    branches don't conduct (their Compare fails) and produce no
    side effects.

    ``gate`` is the contact prefix from the original rung (the
    conditions that gated the original Call); replicated on every
    yield rung so the yield only fires under the original guard.

    ``marshal_in`` is the list of Move ops ``lower_calls`` emitted
    before the bare Call -- the caller-side input marshalling that
    must happen before the yield (so the callee's VAR_INPUT slots
    are populated when the trampoline dispatches it).
    """
    rungs: list[Rung] = []

    if marshal_in:
        rungs.append(Rung(
            ops=list(gate) + list(marshal_in),
            comment=f"marshal inputs for resume_{resume_id}",
        ))

    depth = config.sched_stack_depth
    for k in range(depth):
        rungs.append(Rung(
            ops=list(gate) + [
                Compare(op="==", lhs=config.sched_sp, rhs=str(k)),
                Move(src=str(caller_id),
                     dst=_offset(config.sched_stack_base, k)),
                Move(src=str(resume_id),
                     dst=_offset(config.sched_resume_base, k)),
                Move(src=str(k + 1), dst=config.sched_sp),
                Move(src=str(callee_id), dst=config.sched_next_id),
                OutSet(config.sched_yield_flag),
                Return(),
            ],
            comment=f"yield to id={callee_id} from id={caller_id} "
                    f"(sp={k}, resume={resume_id})",
        ))
    return rungs


def _emit_continuation_rung(gate: list, resume_id: int,
                            tail_ops: list,
                            resume_label_prefix: str = "resume_",
                            ) -> Rung:
    """Emit the continuation rung that the resume-dispatch header
    jumps to after the called POU completes.

    Layout: [Label(resume_K), gate..., tail_ops...]

    ``tail_ops`` is everything that came after the bare Call in the
    original rung -- the caller-side output marshalling Moves from
    ``lower_calls``, plus any user-authored post-Call ops.  The
    gate is re-applied so the tail only fires if the original
    conditions are still true at resume time (this is the locked-in
    "re-evaluate gate on resume" semantic).
    """
    return Rung(
        ops=[Label(name=f"{resume_label_prefix}{resume_id}")] +
            list(gate) + list(tail_ops),
        comment=f"resume continuation: site {resume_id}",
    )


def _rewrite_pou_body(sub: Subroutine, alloc: SlotAllocation,
                      config: LoweringConfig,
                      leaves: set[str]) -> tuple[Subroutine, int]:
    """Rewrite a non-Main POU's body, replacing each bare CLICK Call
    with the corresponding yield sequence + continuation Label.

    Returns the rewritten Subroutine and the number of resume sites
    created (used by the caller to size the resume-dispatch header).

    Every bare ``Call`` op in the body is treated as a nested call
    (illegal CLICK natively).  The target may itself be a leaf or a
    non-leaf -- doesn't matter; all nested calls go through the
    scheduler.

    The rewriter walks each rung looking for ``Call`` ops.  For each
    Call found:

      1. Split the rung at the Call's position.
      2. Pre-Call ops = gate + marshal-in moves -> yield rungs.
      3. Post-Call ops = marshal-out moves + user ops ->
         continuation rung with Label(resume_K).
      4. Allocate a fresh resume id K for this call site.

    Rungs with no Calls pass through unchanged.

    Limitation (for now): if a rung contains MULTIPLE Calls, only
    the first is rewritten; the others are left for a future pass
    to handle.  Most generated rungs from lower_calls contain at
    most one Call.
    """
    caller_id = alloc.pou_id[sub.name]
    new_rungs: list[Rung] = []
    next_resume_id = 1

    for rung in sub.rungs:
        # Find the first Call op in this rung, if any.
        call_idx = None
        for i, op in enumerate(rung.ops):
            if isinstance(op, Call):
                call_idx = i
                break

        if call_idx is None:
            new_rungs.append(rung)
            continue

        call_op = rung.ops[call_idx]
        pre_ops = rung.ops[:call_idx]
        post_ops = rung.ops[call_idx + 1:]

        # Identify gate vs marshal-in within pre_ops.
        gate = _gate_prefix(pre_ops)
        marshal_in = pre_ops[len(gate):]

        callee_id = alloc.pou_id.get(call_op.target)
        if callee_id is None:
            raise LoweringError(
                f"POU {sub.name!r} contains Call to unknown target "
                f"{call_op.target!r} (not in pou_id map)"
            )

        resume_id = next_resume_id
        next_resume_id += 1

        new_rungs.extend(_emit_yield_rungs(
            gate=gate,
            caller_id=caller_id,
            resume_id=resume_id,
            callee_id=callee_id,
            marshal_in=marshal_in,
            config=config,
        ))
        new_rungs.append(_emit_continuation_rung(
            gate=gate,
            resume_id=resume_id,
            tail_ops=post_ops,
        ))

    resume_count = next_resume_id - 1
    if resume_count == 0:
        return sub, 0

    # Prepend the resume-dispatch header so the body re-enters at
    # the right continuation after each nested call returns.
    header = emit_resume_dispatch(resume_count, config)
    return dataclasses.replace(sub, rungs=header + new_rungs), resume_count


def _rewrite_main_body(main: Subroutine, alloc: SlotAllocation,
                       config: LoweringConfig,
                       leaves: set[str]) -> Subroutine:
    """Rewrite Main's body, replacing Call(non_leaf_pou) with a
    scheduler-dispatch sequence.

    Main may issue native CLICK CALLs to leaf POUs (those are legal
    -- a non-yielding callee returns normally).  But Main's Call to
    a non-leaf POU has to go through the scheduler, because the
    non-leaf POU's body cannot be reached via a normal CALL chain
    (its internal yields require trampoline mediation).

    For each Call(P) in Main:
      - P is a leaf  -> leave the Call op as-is (native CLICK CALL).
      - P is non-leaf -> replace the Call op with:
                          Move(P.id -> sched_next_id)
                          Move("0"  -> sched_resume_id)
                         (no push, no yield flag -- Main is the top
                          of the call graph; the trampoline picks up
                          sched_next_id on the next dispatch.)

    The dispatch happens on the NEXT scan, not this one (the
    trampoline ran at the top of this scan, before reaching Main's
    user body).  Main's body continues with whatever rungs follow.

    Known limitation: ``lower_calls`` emits Move-out ops AFTER the
    bare Call (caller-side output demarshalling).  When this
    rewriter replaces the Call with a scheduler-dispatch Move,
    those Move-out ops remain in the rung -- but the callee hasn't
    run yet (it'll dispatch on the next scan).  So Move-out reads
    stale data on the current scan.  Workarounds: (a) check a
    "result ready" flag the user authors; (b) gate Move-out on
    ``ContactNC(C2000 + P.id)`` (POU-not-active).  A future pass
    could auto-gate, but for now this is the user's responsibility.
    Functions called from a non-Main body don't have this issue:
    they go through the full yield/resume cycle which only reaches
    the continuation rung after the callee has completed.
    """
    new_rungs: list[Rung] = []
    for rung in main.rungs:
        new_ops: list = []
        for op in rung.ops:
            if isinstance(op, Call) and op.target not in leaves:
                # Rewrite to scheduler dispatch.
                callee_id = alloc.pou_id.get(op.target)
                if callee_id is None:
                    raise LoweringError(
                        f"Main contains Call to unknown target "
                        f"{op.target!r}"
                    )
                new_ops.append(Move(src=str(callee_id),
                                    dst=config.sched_next_id))
                new_ops.append(Move(src="0",
                                    dst=config.sched_resume_id))
            else:
                new_ops.append(op)
        new_rungs.append(Rung(ops=new_ops, comment=rung.comment))
    return dataclasses.replace(main, rungs=new_rungs)


def lower_scheduled_calls(prog: Program,
                          alloc: Optional[SlotAllocation] = None,
                          config: Optional[LoweringConfig] = None,
                          ) -> tuple[Program, SlotAllocation]:
    """Apply the scheduled-call rewriter across a Program.

    For each non-Main POU body: rewrite every bare Call into the
    yield + continuation form, prepending a resume-dispatch header
    that routes re-entry to the right continuation.

    For Main: rewrite each Call(non-leaf) into a scheduler dispatch
    (set ``sched_next_id``, clear ``sched_resume_id``).  Calls to
    leaf POUs remain native CLICK CALLs.

    Pipeline placement: run AFTER ``lower_calls`` (which expanded
    parameterized Calls into Move-Call-Move sequences) and BEFORE
    ``prepend_trampoline_to_main`` (which assumes the program's
    final dispatch-time shape).  Composing them::

        prog, alloc = lower_pou_bodies(prog)
        prog, _     = lower_calls(prog, alloc=alloc)
        prog, _     = lower_scheduled_calls(prog, alloc=alloc)
        prog        = prepend_trampoline_to_main(prog, alloc)

    Returns the rewritten Program + the allocation used.  Input is
    not mutated.
    """
    if alloc is None:
        alloc = allocate_slots(prog, config)
    if config is None:
        config = alloc.config

    leaves = _classify_leaves(prog)
    main = prog.main_subroutine()
    main_name = main.name if main is not None else None

    new_subs: list[Subroutine] = []
    for sub in prog.subroutines:
        if sub.name == main_name:
            new_subs.append(_rewrite_main_body(sub, alloc, config, leaves))
        else:
            rewritten, _ = _rewrite_pou_body(sub, alloc, config, leaves)
            new_subs.append(rewritten)

    return dataclasses.replace(prog, subroutines=new_subs), alloc
