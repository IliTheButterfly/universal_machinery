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
    Assignment, Configuration, ForStatement, Interface, Method, NamedType,
    PouInstance, PouKind, Program, Resource, Subroutine, TagRef,
)
from .il.ast import VarDirection
from .il.ops import Call, ParallelGroup, tags_of
from .il.st import is_lvalue, walk_expressions


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


def _check_oop_references(prog: Program) -> list[ValidationError]:
    """IEC 61131-3 3rd-edition OOP checks (METHOD / INTERFACE /
    EXTENDS / IMPLEMENTS / ABSTRACT).

    Catches:
      - extends-unknown-fb     : Subroutine.extends names a non-FB
                                 or undeclared POU.
      - implements-unknown-iface: Subroutine.implements names an
                                 Interface that isn't in Program.interfaces.
      - abstract-method-on-concrete-fb: a FUNCTION_BLOCK declares
                                 ``abstract=False`` but has methods
                                 with ``abstract=True``.
      - interface-method-not-abstract: a method on an ``Interface``
                                 has ``abstract=False`` or a body.
      - abstract-method-has-body: any ``abstract=True`` method also
                                 carries rungs (contradictory).
    """
    errors: list[ValidationError] = []
    declared_fbs = {s.name: s for s in prog.subroutines
                    if s.kind is PouKind.FUNCTION_BLOCK}
    declared_ifaces = {getattr(i, "name", None) for i in prog.interfaces}

    for sub in prog.subroutines:
        if sub.kind is not PouKind.FUNCTION_BLOCK:
            continue
        if sub.extends and sub.extends not in declared_fbs:
            errors.append(ValidationError(
                code="extends-unknown-fb",
                message=(f"FUNCTION_BLOCK {sub.name!r} extends "
                         f"{sub.extends!r}, which is not a declared "
                         f"FUNCTION_BLOCK"),
                location=f"Subroutine '{sub.name}'",
            ))
        for iface_name in sub.implements:
            if iface_name not in declared_ifaces:
                errors.append(ValidationError(
                    code="implements-unknown-iface",
                    message=(f"FUNCTION_BLOCK {sub.name!r} implements "
                             f"{iface_name!r}, which is not a declared "
                             f"Interface"),
                    location=f"Subroutine '{sub.name}'",
                ))
        for m in sub.methods:
            if getattr(m, "abstract", False):
                if not sub.abstract:
                    errors.append(ValidationError(
                        code="abstract-method-on-concrete-fb",
                        message=(f"FUNCTION_BLOCK {sub.name!r} declares "
                                 f"abstract method {m.name!r} but the "
                                 f"FB itself is not marked abstract"),
                        location=f"Subroutine '{sub.name}' / Method '{m.name}'",
                    ))
                if m.rungs:
                    errors.append(ValidationError(
                        code="abstract-method-has-body",
                        message=(f"Method {m.name!r} on FB {sub.name!r} "
                                 f"is abstract but carries a body"),
                        location=f"Subroutine '{sub.name}' / Method '{m.name}'",
                    ))

    for iface in prog.interfaces:
        if not isinstance(iface, Interface):
            continue
        for m in iface.methods:
            if not m.abstract:
                errors.append(ValidationError(
                    code="interface-method-not-abstract",
                    message=(f"Interface {iface.name!r} method "
                             f"{m.name!r} must be abstract "
                             f"(interfaces declare only signatures)"),
                    location=f"Interface '{iface.name}' / Method '{m.name}'",
                ))
            if m.rungs:
                errors.append(ValidationError(
                    code="interface-method-has-body",
                    message=(f"Interface {iface.name!r} method "
                             f"{m.name!r} must not carry a body"),
                    location=f"Interface '{iface.name}' / Method '{m.name}'",
                ))

    return errors


def _check_body_kind_mutex(prog: Program) -> list[ValidationError]:
    """A POU's body must be exactly one of ``rungs`` / ``sfc`` / ``st_body``.

    Declaring more than one is ambiguous (which one runs?); declaring
    none is fine for SUBROUTINE / abstract methods but produces a
    warning for non-abstract callable POUs.  Same rule applies to
    ``Method`` bodies on FUNCTION_BLOCK POUs.
    """
    errors: list[ValidationError] = []

    def _check_body(name: str, where: str, rungs, sfc, st_body) -> None:
        kinds_set = sum(1 for x in (rungs, sfc, st_body) if x)
        if kinds_set > 1:
            errors.append(ValidationError(
                code="multiple-body-kinds",
                message=(f"{name!r} declares more than one body kind "
                         f"(rungs / sfc / st_body); pick exactly one"),
                location=where,
            ))

    for sub in prog.subroutines:
        _check_body(sub.name, f"Subroutine '{sub.name}'",
                    sub.rungs, sub.sfc, sub.st_body)
        for m in sub.methods:
            # Method's rungs default to [] (falsy); st_body defaults
            # to None.  Only the abstract case both-empty is allowed
            # without warning.
            _check_body(m.name,
                        f"Subroutine '{sub.name}' / Method '{m.name}'",
                        m.rungs, None, m.st_body)

    return errors


def _check_st_lvalues(prog: Program) -> list[ValidationError]:
    """``Assignment.target`` must be an lvalue (VarRef / FieldAccess /
    IndexAccess).  Literals, math expressions, and function-call
    results can't be assigned to."""
    errors: list[ValidationError] = []

    for sub in prog.subroutines:
        if sub.st_body is not None:
            _walk_st_stmts(sub.st_body, f"Subroutine '{sub.name}' / st_body",
                           errors)
        for m in sub.methods:
            if m.st_body is not None:
                _walk_st_stmts(m.st_body,
                               f"Subroutine '{sub.name}' / Method "
                               f"'{m.name}' / st_body", errors)

    return errors


def _walk_st_stmts(stmts, where: str,
                   errors: list[ValidationError]) -> None:
    """Recursively walk a Statement list, checking each for
    structural issues.  Used by ``_check_st_lvalues``.
    """
    from .il import (
        CaseStatement, ForStatement, IfStatement, RepeatStatement,
        WhileStatement,
    )
    for s in stmts:
        if isinstance(s, Assignment):
            if not is_lvalue(s.target):
                errors.append(ValidationError(
                    code="bad-assignment-target",
                    message=(f"Assignment.target must be an lvalue; "
                             f"got {type(s.target).__name__}"),
                    location=where,
                ))
        elif isinstance(s, IfStatement):
            for _cond, body in s.branches:
                _walk_st_stmts(body, where, errors)
            if s.else_branch is not None:
                _walk_st_stmts(s.else_branch, where, errors)
        elif isinstance(s, CaseStatement):
            for clause in s.clauses:
                _walk_st_stmts(clause.body, where, errors)
            if s.else_branch is not None:
                _walk_st_stmts(s.else_branch, where, errors)
        elif isinstance(s, WhileStatement):
            _walk_st_stmts(s.body, where, errors)
        elif isinstance(s, RepeatStatement):
            _walk_st_stmts(s.body, where, errors)
        elif isinstance(s, ForStatement):
            _walk_st_stmts(s.body, where, errors)


def _check_st_for_index_declared(prog: Program) -> list[ValidationError]:
    """``ForStatement.index_var`` must name a local variable or
    parameter declared on the enclosing POU."""
    errors: list[ValidationError] = []

    def _collect_for_indices(stmts, into: list[str]) -> None:
        from .il import (
            CaseStatement, ForStatement, IfStatement, RepeatStatement,
            WhileStatement,
        )
        for s in stmts:
            if isinstance(s, ForStatement):
                into.append(s.index_var)
                _collect_for_indices(s.body, into)
            elif isinstance(s, IfStatement):
                for _cond, body in s.branches:
                    _collect_for_indices(body, into)
                if s.else_branch is not None:
                    _collect_for_indices(s.else_branch, into)
            elif isinstance(s, CaseStatement):
                for clause in s.clauses:
                    _collect_for_indices(clause.body, into)
                if s.else_branch is not None:
                    _collect_for_indices(s.else_branch, into)
            elif isinstance(s, (WhileStatement, RepeatStatement)):
                _collect_for_indices(s.body, into)

    for sub in prog.subroutines:
        scope = {v.name for v in (sub.inputs + sub.outputs
                                    + sub.in_outs + sub.local_vars)}
        if sub.st_body is not None:
            indices: list[str] = []
            _collect_for_indices(sub.st_body, indices)
            for name in indices:
                if name not in scope:
                    errors.append(ValidationError(
                        code="for-index-undeclared",
                        message=(f"FOR index variable {name!r} is not "
                                 f"declared as a local or parameter"),
                        location=f"Subroutine '{sub.name}'",
                    ))
        for m in sub.methods:
            method_scope = scope | {v.name for v in (m.inputs + m.outputs
                                                      + m.in_outs
                                                      + m.local_vars)}
            if m.st_body is not None:
                indices = []
                _collect_for_indices(m.st_body, indices)
                for name in indices:
                    if name not in method_scope:
                        errors.append(ValidationError(
                            code="for-index-undeclared",
                            message=(f"FOR index variable {name!r} is "
                                     f"not declared as a local or "
                                     f"parameter"),
                            location=(f"Subroutine '{sub.name}' / "
                                      f"Method '{m.name}'"),
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
      7. IEC 3rd-edition OOP references (EXTENDS / IMPLEMENTS /
         ABSTRACT consistency, Interface method shape)
      8. SFC well-formedness (per POU's SFC body)
      9. ST body kind mutex (rungs / sfc / st_body exactly one)
     10. ST Assignment.target is an lvalue
     11. ST FOR index variable is declared
    """
    errors: list[ValidationError] = []
    errors.extend(_check_tag_references(prog))
    errors.extend(_check_named_type_references(prog))
    errors.extend(_check_call_targets(prog))
    errors.extend(_check_call_parameter_bindings(prog))
    errors.extend(_check_call_graph_cycles(prog))
    errors.extend(_check_task_references(prog))
    errors.extend(_check_oop_references(prog))
    errors.extend(_check_sfc_wellformedness(prog))
    errors.extend(_check_body_kind_mutex(prog))
    errors.extend(_check_st_lvalues(prog))
    errors.extend(_check_st_for_index_declared(prog))
    return errors


def is_valid(prog: Program) -> bool:
    """Convenience: ``True`` iff ``validate(prog)`` is empty."""
    return not validate(prog)
