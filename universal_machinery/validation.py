"""Pure-IL validation pass.

Checks a ``Program`` for structural and referential consistency
*before* any emitter or lowering pass touches it.  Catches the
class of mistakes that produce confusing failures further down the
pipeline -- unresolved tag references, unknown call targets,
call-graph cycles, parameter-binding mismatches, missing task
declarations -- and surfaces them as a structured list of
``ValidationError`` records with location info.

This is the right place for:

  - **Pre-lowering checks.**  Run before slot allocation / call
    marshalling so failures are reported in source terms, not in
    terms of the lowered output.
  - **Agent-produced IL.**  AI agents building IL programs benefit
    from a sharp, fast check that flags their mistakes before
    they hit the emitter or the PLCopen tool.
  - **CI / pre-commit hooks.**  A program that validate()s clean
    is one that's at least structurally sound; emitter + XSD +
    reference-tool steps then verify behaviour.

What's *not* here (deferred): semantic type checking on op
operands (e.g. `Move(src=BOOL_addr, dst=INT_addr)`), constant-
evaluation of literals, range checks on subrange types, deeper
cross-resource consistency in multi-PLC Configurations.  These
need a type-resolver pass that's a separate slice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .il import (
    Configuration, NamedType, PouInstance, Program, Resource, Subroutine,
    TagRef,
)
from .il.ast import VarDirection
from .il.ops import Call, ParallelGroup, tags_of


@dataclass(frozen=True)
class ValidationError:
    """One structural / referential problem found in a Program.

    ``code`` is a stable short identifier (e.g. ``"unresolved-tagref"``)
    callers / CI tooling can match on without parsing the message.
    ``message`` is human-readable.  ``location`` is a free-form
    breadcrumb path like ``"Subroutine 'Main' / rung 3"`` to help
    track down the offending element.
    """

    code: str
    message: str
    location: str = ""


# -----------------------------------------------------------------------------
# Individual checks
# -----------------------------------------------------------------------------


def _check_tag_references(prog: Program) -> list[ValidationError]:
    """Every TagRef in any rung op must name a Tag declared in
    ``Program.tags``, OR a formal parameter of the enclosing POU.

    TagRefs to formal-parameter names are resolved by the callee-
    body rewriter; TagRefs to global names are resolved by the
    tag-allocator / writer.  Names that are neither produce a
    ``unresolved-tagref`` error.
    """
    errors: list[ValidationError] = []
    declared_tags = set(prog.tags)

    for sub in prog.subroutines:
        param_names = {v.name for v in (sub.inputs + sub.outputs
                                          + sub.in_outs + sub.local_vars)}
        scope = declared_tags | param_names

        # Walk rung-body refs.
        for rung_idx, rung in enumerate(sub.rungs):
            for op in rung.ops:
                refs = tags_of(op)
                for ref_name in refs:
                    # Field/index access: TagRef("axis.position") or
                    # TagRef("buf[3]") -- the base name (everything up
                    # to the first . or [) is what we check against
                    # the scope.
                    base = ref_name.split(".")[0].split("[")[0]
                    if base not in scope:
                        errors.append(ValidationError(
                            code="unresolved-tagref",
                            message=(f"TagRef {ref_name!r} doesn't match "
                                     f"any global Tag or formal parameter"),
                            location=f"Subroutine '{sub.name}' / rung {rung_idx}",
                        ))

        # Walk SFC body if present.
        if sub.sfc is not None:
            for tr_idx, tr in enumerate(sub.sfc.transitions):
                for op in tr.condition:
                    for ref_name in tags_of(op):
                        base = ref_name.split(".")[0].split("[")[0]
                        if base not in scope:
                            errors.append(ValidationError(
                                code="unresolved-tagref",
                                message=(f"TagRef {ref_name!r} in SFC "
                                         f"transition doesn't match any "
                                         f"global Tag or formal parameter"),
                                location=(f"Subroutine '{sub.name}' / "
                                          f"SFC transition {tr_idx}"),
                            ))
    return errors


def _check_named_type_references(prog: Program) -> list[ValidationError]:
    """Every ``NamedType("X")`` must reference a UDT declared in
    ``Program.user_types``.

    Walks Var.data_type on every member of every UDT and every
    Var.data_type on every POU parameter / local.
    """
    errors: list[ValidationError] = []
    declared = {getattr(ut, "name", None) for ut in prog.user_types}

    def _walk_data_type(dt, where: str) -> None:
        if isinstance(dt, NamedType):
            if dt.name not in declared:
                errors.append(ValidationError(
                    code="unresolved-named-type",
                    message=f"NamedType {dt.name!r} is not declared "
                            f"in Program.user_types",
                    location=where,
                ))

    # UDT-internal references (struct member types, array element
    # types, alias base types).
    for ut in prog.user_types:
        from .il import AliasType, ArrayType, StructType
        if isinstance(ut, StructType):
            for m in ut.members:
                _walk_data_type(m.data_type,
                                where=f"StructType '{ut.name}' member '{m.name}'")
        elif isinstance(ut, ArrayType):
            _walk_data_type(ut.element_type,
                            where=f"ArrayType '{ut.name}' element_type")
        elif isinstance(ut, AliasType):
            _walk_data_type(ut.base,
                            where=f"AliasType '{ut.name}' base")

    # POU parameter / local types.
    for sub in prog.subroutines:
        for kind, vars_ in (("input",   sub.inputs),
                            ("output",  sub.outputs),
                            ("in_out",  sub.in_outs),
                            ("local",   sub.local_vars)):
            for v in vars_:
                _walk_data_type(v.data_type,
                                where=f"Subroutine '{sub.name}' {kind} '{v.name}'")

    # DataBlock member types.
    for db in prog.data_blocks:
        for m in db.members:
            _walk_data_type(m.data_type,
                            where=f"DataBlock '{db.name}' member '{m.name}'")

    return errors


def _check_call_targets(prog: Program) -> list[ValidationError]:
    """Every ``Call.target`` in any rung op must name a Subroutine
    declared in ``Program.subroutines``.

    Bare Calls (no inputs/outputs/instance/return_to) can target
    hand-authored vendor subroutines that aren't in the IL's POU
    list; we skip those.  Parameterised calls require resolution.
    """
    errors: list[ValidationError] = []
    declared = {s.name for s in prog.subroutines}

    for sub in prog.subroutines:
        for rung_idx, rung in enumerate(sub.rungs):
            for op_idx, op in enumerate(rung.ops):
                _walk_call(op, sub.name, rung_idx, op_idx,
                           declared, errors)
    return errors


def _walk_call(op, sub_name: str, rung_idx: int, op_idx: int,
               declared: set[str],
               errors: list[ValidationError]) -> None:
    """Recursive helper: find every Call inside an op (incl.
    ParallelGroup branches) and check its target."""
    if isinstance(op, Call):
        parameterized = bool(op.inputs or op.outputs
                             or op.instance is not None
                             or op.return_to is not None)
        if parameterized and op.target not in declared:
            errors.append(ValidationError(
                code="unknown-call-target",
                message=(f"Parameterized Call targets {op.target!r}, "
                         f"which is not declared in Program.subroutines"),
                location=(f"Subroutine '{sub_name}' / rung {rung_idx} "
                          f"/ op {op_idx}"),
            ))
    elif isinstance(op, ParallelGroup):
        for branch in op.branches:
            for inner_idx, inner in enumerate(branch):
                _walk_call(inner, sub_name, rung_idx,
                           op_idx * 1000 + inner_idx, declared, errors)


def _check_call_parameter_bindings(prog: Program) -> list[ValidationError]:
    """Every ``Call.inputs`` / ``Call.outputs`` formal-parameter
    name must match a declared VAR_INPUT/IN_OUT (for inputs) or
    VAR_OUTPUT/IN_OUT (for outputs) on the callee."""
    errors: list[ValidationError] = []
    pou_by_name: dict[str, Subroutine] = {s.name: s for s in prog.subroutines}

    for sub in prog.subroutines:
        for rung_idx, rung in enumerate(sub.rungs):
            for op in rung.ops:
                _walk_param_check(op, sub.name, rung_idx,
                                  pou_by_name, errors)
    return errors


def _walk_param_check(op, sub_name: str, rung_idx: int,
                     pou_by_name: dict[str, Subroutine],
                     errors: list[ValidationError]) -> None:
    if isinstance(op, Call):
        callee = pou_by_name.get(op.target)
        if callee is None:
            return  # already flagged by _check_call_targets
        input_names = {v.name for v in (callee.inputs + callee.in_outs)}
        output_names = {v.name for v in (callee.outputs + callee.in_outs)}

        for formal, _ in op.inputs:
            if formal not in input_names:
                errors.append(ValidationError(
                    code="bad-input-binding",
                    message=(f"Call to {op.target!r} binds input "
                             f"{formal!r}, which is not declared as a "
                             f"VAR_INPUT or VAR_IN_OUT on {op.target!r}"),
                    location=f"Subroutine '{sub_name}' / rung {rung_idx}",
                ))
        for formal, _ in op.outputs:
            if formal not in output_names:
                errors.append(ValidationError(
                    code="bad-output-binding",
                    message=(f"Call to {op.target!r} binds output "
                             f"{formal!r}, which is not declared as a "
                             f"VAR_OUTPUT or VAR_IN_OUT on {op.target!r}"),
                    location=f"Subroutine '{sub_name}' / rung {rung_idx}",
                ))
        if op.return_to is not None and not callee.outputs:
            errors.append(ValidationError(
                code="return-to-no-outputs",
                message=(f"Call to {op.target!r} sets return_to but "
                         f"{op.target!r} declares no VAR_OUTPUT"),
                location=f"Subroutine '{sub_name}' / rung {rung_idx}",
            ))
    elif isinstance(op, ParallelGroup):
        for branch in op.branches:
            for inner in branch:
                _walk_param_check(inner, sub_name, rung_idx,
                                  pou_by_name, errors)


def _check_call_graph_cycles(prog: Program) -> list[ValidationError]:
    """The CLICK scheduler model forbids self-recursion (a POU
    can't be on the stack twice).  Detect call cycles in the
    static call graph.
    """
    errors: list[ValidationError] = []
    pou_by_name = {s.name: s for s in prog.subroutines}
    main_name = prog.main_subroutine().name if prog.main_subroutine() else None

    # Build adjacency: callers -> targets.
    callees: dict[str, set[str]] = {s.name: set() for s in prog.subroutines}
    for sub in prog.subroutines:
        for rung in sub.rungs:
            for op in rung.ops:
                _collect_call_targets(op, callees[sub.name])

    # DFS for cycles.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in callees}
    cycle_path: list[str] = []

    def visit(name: str, stack: list[str]) -> bool:
        if color[name] == GRAY:
            # Cycle detected -- record the back edge.
            idx = stack.index(name) if name in stack else 0
            cycle_path.extend(stack[idx:] + [name])
            return True
        if color[name] == BLACK:
            return False
        color[name] = GRAY
        stack.append(name)
        for tgt in sorted(callees.get(name, set())):
            if tgt in pou_by_name:
                if visit(tgt, stack):
                    return True
        stack.pop()
        color[name] = BLACK
        return False

    for name in callees:
        if color[name] == WHITE:
            if visit(name, []):
                break

    if cycle_path:
        errors.append(ValidationError(
            code="call-graph-cycle",
            message=("Call graph contains a cycle: "
                     + " -> ".join(cycle_path)),
            location="Program",
        ))
    return errors


def _collect_call_targets(op, into: set[str]) -> None:
    """Add every Call's target name into the set, recursing into
    ParallelGroup branches."""
    if isinstance(op, Call):
        into.add(op.target)
    elif isinstance(op, ParallelGroup):
        for branch in op.branches:
            for inner in branch:
                _collect_call_targets(inner, into)


def _check_task_references(prog: Program) -> list[ValidationError]:
    """Every ``PouInstance.task`` name must match a ``TaskSpec`` in
    the same Resource.  Every ``PouInstance.type_name`` must match a
    declared Subroutine.
    """
    errors: list[ValidationError] = []
    declared_pous = {s.name for s in prog.subroutines}

    for cfg in prog.configurations:
        for res in cfg.resources:
            task_names = {t.name for t in res.tasks}
            for inst in res.pou_instances:
                if inst.task is not None and inst.task not in task_names:
                    errors.append(ValidationError(
                        code="unknown-task",
                        message=(f"PouInstance {inst.name!r} binds to "
                                 f"task {inst.task!r}, which is not "
                                 f"declared in Resource {res.name!r}"),
                        location=(f"Configuration '{cfg.name}' / "
                                  f"Resource '{res.name}'"),
                    ))
                if inst.type_name not in declared_pous:
                    errors.append(ValidationError(
                        code="unknown-pou-type",
                        message=(f"PouInstance {inst.name!r} has type "
                                 f"{inst.type_name!r}, which is not a "
                                 f"declared Subroutine"),
                        location=(f"Configuration '{cfg.name}' / "
                                  f"Resource '{res.name}'"),
                    ))
    return errors


def _check_sfc_wellformedness(prog: Program) -> list[ValidationError]:
    """Delegate to ``SfcNetwork.validate()`` for each POU's SFC body."""
    errors: list[ValidationError] = []
    for sub in prog.subroutines:
        if sub.sfc is None:
            continue
        for issue in sub.sfc.validate():
            errors.append(ValidationError(
                code="sfc-issue",
                message=issue,
                location=f"Subroutine '{sub.name}' / SFC body",
            ))
    return errors


# -----------------------------------------------------------------------------
# Top-level pass
# -----------------------------------------------------------------------------


def validate(prog: Program) -> list[ValidationError]:
    """Run all structural checks on ``prog`` and return the
    aggregated error list.  Returns an empty list if the program
    is structurally sound.

    Checks run in this order (early checks don't block later
    ones; users see a complete picture per call):

      1. Unresolved TagRefs
      2. Unresolved NamedType references
      3. Unknown Call targets
      4. Call parameter-binding mismatches
      5. Call-graph cycles
      6. PouInstance task / type references
      7. SFC well-formedness (per POU's SFC body)
    """
    errors: list[ValidationError] = []
    errors.extend(_check_tag_references(prog))
    errors.extend(_check_named_type_references(prog))
    errors.extend(_check_call_targets(prog))
    errors.extend(_check_call_parameter_bindings(prog))
    errors.extend(_check_call_graph_cycles(prog))
    errors.extend(_check_task_references(prog))
    errors.extend(_check_sfc_wellformedness(prog))
    return errors


def is_valid(prog: Program) -> bool:
    """Convenience: ``True`` iff ``validate(prog)`` is empty."""
    return not validate(prog)
