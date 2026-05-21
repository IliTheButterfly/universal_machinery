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

import re
from dataclasses import dataclass
from typing import Optional

from .il import (
    AccessVar, Assignment, ConfigVar, Configuration, FbBlock, FbdJump,
    FbdLabel, FbdNetwork, FbdReturn, ForStatement, GotoStatement,
    InOutVariable, InVariable, Interface, LabelStatement, Method,
    NamedType, OutVariable, PouInstance, PouKind, Program, Resource,
    Subroutine, TagRef,
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
    """A POU's body must be exactly one of ``rungs`` / ``sfc`` /
    ``st_body`` / ``fbd_body``.

    Declaring more than one is ambiguous (which one runs?); declaring
    none is fine for SUBROUTINE / abstract methods but produces a
    warning for non-abstract callable POUs.  Same rule applies to
    ``Method`` bodies on FUNCTION_BLOCK POUs.
    """
    errors: list[ValidationError] = []

    def _check_body(name: str, where: str,
                    rungs, sfc, st_body, fbd_body) -> None:
        kinds_set = sum(1 for x in (rungs, sfc, st_body, fbd_body) if x)
        if kinds_set > 1:
            errors.append(ValidationError(
                code="multiple-body-kinds",
                message=(f"{name!r} declares more than one body kind "
                         f"(rungs / sfc / st_body / fbd_body); "
                         f"pick exactly one"),
                location=where,
            ))

    for sub in prog.subroutines:
        _check_body(sub.name, f"Subroutine '{sub.name}'",
                    sub.rungs, sub.sfc, sub.st_body, sub.fbd_body)
        for m in sub.methods:
            # Methods don't have sfc; rungs default to [] (falsy);
            # st_body / fbd_body default to None.
            _check_body(m.name,
                        f"Subroutine '{sub.name}' / Method '{m.name}'",
                        m.rungs, None, m.st_body, m.fbd_body)

    return errors


_INSTANCE_PATH_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[0-9]+\])+$"
)


def _check_access_and_config_vars(prog: Program) -> list[ValidationError]:
    """IEC §2.4.3.2 / §2.7.1 well-formedness for ``VAR_ACCESS`` and
    ``VAR_CONFIG`` entries on each configuration.

    Emits:

      - access-var-bad-direction   : ``AccessVar.direction`` isn't
                                      one of ``READ_ONLY`` /
                                      ``READ_WRITE``.
      - access-var-duplicate-alias : two ``AccessVar``s in the same
                                      configuration share the same
                                      alias (external clients would
                                      see ambiguity).
      - access-var-bad-path        : ``instance_path`` doesn't look
                                      like a valid IEC §2.4.3.2 path
                                      (must contain at least one
                                      ``.`` or ``[]`` to qualify the
                                      reference into a resource).
      - config-var-bad-path        : same shape check for ConfigVar.
      - config-var-duplicate-path  : two ``ConfigVar``s in the same
                                      configuration bind the same
                                      ``instance_path`` (ambiguous
                                      link-time value).
    """
    errors: list[ValidationError] = []
    valid_directions = {"READ_ONLY", "READ_WRITE"}

    for cfg in prog.configurations:
        if not isinstance(cfg, Configuration):
            continue
        where = f"Configuration '{cfg.name}'"

        # AccessVars
        seen_aliases: set[str] = set()
        for v in cfg.access_vars:
            if v.direction not in valid_directions:
                errors.append(ValidationError(
                    code="access-var-bad-direction",
                    message=(f"AccessVar {v.alias!r} has direction "
                             f"{v.direction!r}; expected one of "
                             f"{sorted(valid_directions)}"),
                    location=where,
                ))
            if v.alias in seen_aliases:
                errors.append(ValidationError(
                    code="access-var-duplicate-alias",
                    message=(f"AccessVar alias {v.alias!r} declared "
                             f"more than once"),
                    location=where,
                ))
            seen_aliases.add(v.alias)
            if not _INSTANCE_PATH_RE.match(v.instance_path):
                errors.append(ValidationError(
                    code="access-var-bad-path",
                    message=(f"AccessVar {v.alias!r} instance_path "
                             f"{v.instance_path!r} doesn't look like "
                             f"an IEC §2.4.3.2 access path "
                             f"(expected ``Resource.PouInstance.var`` "
                             f"or similar)"),
                    location=where,
                ))

        # ConfigVars
        seen_paths: set[str] = set()
        for v in cfg.config_vars:
            if not _INSTANCE_PATH_RE.match(v.instance_path):
                errors.append(ValidationError(
                    code="config-var-bad-path",
                    message=(f"ConfigVar instance_path "
                             f"{v.instance_path!r} doesn't look like "
                             f"an IEC §2.4.3.2 access path"),
                    location=where,
                ))
            if v.instance_path in seen_paths:
                errors.append(ValidationError(
                    code="config-var-duplicate-path",
                    message=(f"ConfigVar binds {v.instance_path!r} "
                             f"more than once"),
                    location=where,
                ))
            seen_paths.add(v.instance_path)

    return errors


def _check_st_goto_labels(prog: Program) -> list[ValidationError]:
    """Goto/Label well-formedness inside ST bodies.

    Emits:
      - st-duplicate-label    : two ``LabelStatement``s in the
                                 same body share a name (jumps
                                 would be ambiguous).
      - st-unresolved-goto    : a ``GotoStatement.label`` doesn't
                                 match any label declared in the
                                 same body (forward + backward
                                 references are both fine -- ST
                                 doesn't require declare-before-use).
    """
    errors: list[ValidationError] = []

    def _collect_labels_and_gotos(stmts, labels: list[str],
                                    gotos: list[str]) -> None:
        from .il import (
            CaseStatement, ForStatement, IfStatement, RepeatStatement,
            WhileStatement,
        )
        for s in stmts:
            if isinstance(s, LabelStatement):
                labels.append(s.name)
            elif isinstance(s, GotoStatement):
                gotos.append(s.label)
            elif isinstance(s, IfStatement):
                for _cond, body in s.branches:
                    _collect_labels_and_gotos(body, labels, gotos)
                if s.else_branch is not None:
                    _collect_labels_and_gotos(s.else_branch, labels, gotos)
            elif isinstance(s, CaseStatement):
                for clause in s.clauses:
                    _collect_labels_and_gotos(clause.body, labels, gotos)
                if s.else_branch is not None:
                    _collect_labels_and_gotos(s.else_branch, labels, gotos)
            elif isinstance(s, (WhileStatement, RepeatStatement)):
                _collect_labels_and_gotos(s.body, labels, gotos)
            elif isinstance(s, ForStatement):
                _collect_labels_and_gotos(s.body, labels, gotos)

    def _check_body(stmts, where: str) -> None:
        labels: list[str] = []
        gotos:  list[str] = []
        _collect_labels_and_gotos(stmts, labels, gotos)
        # Duplicate labels
        seen: set[str] = set()
        for name in labels:
            if name in seen:
                errors.append(ValidationError(
                    code="st-duplicate-label",
                    message=(f"Label {name!r} declared more than "
                             f"once in body"),
                    location=where,
                ))
            seen.add(name)
        # Unresolved gotos
        label_set = set(labels)
        for target in gotos:
            if target not in label_set:
                errors.append(ValidationError(
                    code="st-unresolved-goto",
                    message=(f"GOTO targets undeclared label "
                             f"{target!r}"),
                    location=where,
                ))

    for sub in prog.subroutines:
        if sub.st_body is not None:
            _check_body(sub.st_body, f"Subroutine '{sub.name}' / st_body")
        for m in sub.methods:
            if m.st_body is not None:
                _check_body(m.st_body,
                            f"Subroutine '{sub.name}' / Method "
                            f"'{m.name}' / st_body")
    return errors


def _check_fbd_wellformedness(prog: Program) -> list[ValidationError]:
    """Structural checks on every ``FbdNetwork`` body in the program.

    Emits:

      - fbd-duplicate-local-id     : two elements share the same
                                      ``local_id``
      - fbd-unresolved-connection  : ``Connection.source_id`` doesn't
                                      match any element's ``local_id``
      - fbd-unknown-source-pin     : ``Connection.source_pin`` names
                                      a pin the source block doesn't
                                      declare as an output (only
                                      checked when the source is an
                                      ``FbBlock``; in-/out-/inout-
                                      variables have a single
                                      implicit pin)
      - fbd-unknown-jump-label     : ``FbdJump.label`` doesn't match
                                      any ``FbdLabel`` in the network
    """
    errors: list[ValidationError] = []

    def _check_network(net: FbdNetwork, where: str) -> None:
        # local_id uniqueness
        seen_ids: set[int] = set()
        for e in net.elements:
            if e.local_id in seen_ids:
                errors.append(ValidationError(
                    code="fbd-duplicate-local-id",
                    message=(f"FBD element {e.local_id!r} appears "
                             f"more than once in network"),
                    location=where,
                ))
            seen_ids.add(e.local_id)

        by_id = {e.local_id: e for e in net.elements}
        label_names = {e.label for e in net.elements
                        if isinstance(e, FbdLabel)}

        # Every Connection.source_id must resolve; if the source is
        # an FbBlock and source_pin is set, that pin must exist as
        # an output (or in_out) formal-parameter.
        def _check_conn(conn, where_inner: str) -> None:
            if conn is None:
                return
            src = by_id.get(conn.source_id)
            if src is None:
                errors.append(ValidationError(
                    code="fbd-unresolved-connection",
                    message=(f"Connection references unknown "
                             f"local_id {conn.source_id}"),
                    location=where_inner,
                ))
                return
            if isinstance(src, FbBlock) and conn.source_pin is not None:
                produced = {p.formal_parameter for p in src.outputs}
                produced |= {p.formal_parameter for p in src.in_outs}
                if conn.source_pin not in produced:
                    errors.append(ValidationError(
                        code="fbd-unknown-source-pin",
                        message=(f"Connection refers to pin "
                                 f"{conn.source_pin!r} on block "
                                 f"{src.type_name!r} (local_id "
                                 f"{src.local_id}), which doesn't "
                                 f"declare it as output or in_out"),
                        location=where_inner,
                    ))

        for e in net.elements:
            base = f"{where} / element local_id {e.local_id}"
            if isinstance(e, FbBlock):
                for p in e.inputs:
                    _check_conn(p.connection,
                                f"{base} / input '{p.formal_parameter}'")
                for p in e.in_outs:
                    _check_conn(p.connection,
                                f"{base} / in_out '{p.formal_parameter}'")
            elif isinstance(e, (OutVariable, InOutVariable, FbdJump,
                                  FbdReturn)):
                _check_conn(e.connection, base)
            if isinstance(e, FbdJump):
                if e.label not in label_names:
                    errors.append(ValidationError(
                        code="fbd-unknown-jump-label",
                        message=(f"FbdJump targets unknown label "
                                 f"{e.label!r}"),
                        location=base,
                    ))

    for sub in prog.subroutines:
        if sub.fbd_body is not None:
            _check_network(sub.fbd_body,
                           f"Subroutine '{sub.name}' / fbd_body")
        for m in sub.methods:
            if m.fbd_body is not None:
                _check_network(m.fbd_body,
                               f"Subroutine '{sub.name}' / Method "
                               f"'{m.name}' / fbd_body")

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


# -----------------------------------------------------------------------------
# Semantic type checking (IEC §6.5 type compatibility)
# -----------------------------------------------------------------------------


#: IEC §6.5 type-compatibility buckets.  Two types are
#: implicit-compatible when they share a bucket; everything else
#: requires an explicit ``<SRC>_TO_<DST>`` conversion call.
#:
#: The mapping is intentionally permissive within numeric families
#: (signed + unsigned + width promotions all allowed) because real-
#: world PLC code mixes them freely and most vendor compilers
#: accept the lot.  A stricter "width-promotion only" mode could
#: be a follow-up.
_INT_TYPES = frozenset({
    "SINT", "INT", "DINT", "LINT",
    "USINT", "UINT", "UDINT", "ULINT",
})
_REAL_TYPES = frozenset({"REAL", "LREAL"})
_BIT_STRING_TYPES = frozenset({"BYTE", "WORD", "DWORD"})  # LWORD per IEC; absent from TagType
_TIME_TYPES = frozenset({"TIME"})                          # DATE/TOD/DT absent from TagType today
_STRING_TYPES = frozenset({"STRING"})


def _tagtype_name(t) -> Optional[str]:
    """Return the IEC type-name string for a ``TagType`` /
    ``NamedType`` / UserType, or ``None`` if the type is unknown
    or hasn't resolved.

    User-defined types resolve to their *base* via the program's
    user_types registry one level deep -- AliasType chains
    eventually hit an elementary or stop.  Structs / arrays /
    enums fall back to their own name (treated as distinct
    types for compatibility purposes; we don't unify across
    UDTs without a deeper resolver).
    """
    from .il import (
        AliasType, ArrayType, EnumType, NamedType, StructType,
        SubrangeType, TagType,
    )
    if isinstance(t, TagType):
        return t.value
    if isinstance(t, (StructType, ArrayType, EnumType)):
        return t.name
    if isinstance(t, SubrangeType):
        # Subrange types are compatible with their base
        return _tagtype_name(t.base)
    if isinstance(t, AliasType):
        return _tagtype_name(t.base)
    if isinstance(t, NamedType):
        return None  # caller looks up in registry
    return None


def _resolve_named_type(prog: Program, type_obj):
    """Walk ``NamedType`` references through ``Program.user_types``
    until landing on an elementary ``TagType`` or a structural
    type (Struct / Array / Enum).  Returns the resolved type or
    the original on failure."""
    from .il import AliasType, NamedType
    seen: set[str] = set()
    cur = type_obj
    while isinstance(cur, NamedType):
        if cur.name in seen:
            break
        seen.add(cur.name)
        nxt = prog.find_user_type(cur.name)
        if nxt is None:
            return cur
        cur = nxt
    # Aliases chain too
    seen_alias: set[str] = set()
    while isinstance(cur, AliasType):
        if cur.name in seen_alias:
            break
        seen_alias.add(cur.name)
        if isinstance(cur.base, NamedType):
            nxt = prog.find_user_type(cur.base.name)
            cur = nxt if nxt is not None else cur.base
        else:
            cur = cur.base
    return cur


def _are_types_compatible(a_name: Optional[str],
                          b_name: Optional[str]) -> bool:
    """True iff two type names share an IEC compatibility bucket
    (or one side is unknown -- we don't raise on missing info).

    Same-name comparisons are always compatible.  ``None`` on
    either side means "we couldn't resolve the operand", which we
    treat as a non-failure so the structural validator's
    ``unresolved-tagref`` (or similar) is the canonical complaint.
    """
    if a_name is None or b_name is None:
        return True
    if a_name == b_name:
        return True
    # Bit-strings convert to integers of comparable width
    if (a_name in _INT_TYPES and b_name in _INT_TYPES):
        return True
    if (a_name in _REAL_TYPES and b_name in _REAL_TYPES):
        return True
    if (a_name in _BIT_STRING_TYPES and b_name in _BIT_STRING_TYPES):
        return True
    # Integer <-> bit-string: permissive
    if ((a_name in _INT_TYPES and b_name in _BIT_STRING_TYPES)
            or (a_name in _BIT_STRING_TYPES and b_name in _INT_TYPES)):
        return True
    return False


def _is_numeric(name: Optional[str]) -> bool:
    """Numeric = integer family ∪ real family.  Used by
    ``BinaryMath`` checks (math doesn't apply to BOOL, TIME,
    STRING, structs)."""
    if name is None:
        return True  # unknown -> don't complain
    return name in _INT_TYPES or name in _REAL_TYPES


def _is_bool(name: Optional[str]) -> bool:
    if name is None:
        return True
    return name == "BOOL"


def _build_type_env(prog: Program,
                    sub: Subroutine) -> dict[str, str]:
    """Map every name in scope for ``sub`` to its IEC type-name
    string.  Sources:

      - Program.tags
      - sub.inputs / outputs / in_outs / local_vars
      - Top-level UDT registry (for NamedType resolution)

    Local names shadow globals when both define the same name --
    POU parameters / locals take precedence over Program.tags.
    """
    env: dict[str, str] = {}
    for tag in prog.tags.values():
        env[tag.name] = tag.data_type.value
    for v in (sub.inputs + sub.outputs + sub.in_outs + sub.local_vars):
        resolved = _resolve_named_type(prog, v.data_type)
        name = _tagtype_name(resolved)
        if name is not None:
            env[v.name] = name
    return env


def _operand_type(prog: Program, env: dict[str, str], value) -> Optional[str]:
    """Type-name of a Value (Address / TagRef / literal) in scope.

    ``Address`` looks up the raw address in the tag table; ``TagRef``
    looks up the name in the local env first, then globals (already
    merged into env).  Literals (untyped strings) return ``None``
    -- the check skips comparisons against literals to avoid false
    positives.
    """
    from .il import Address, TagRef
    if isinstance(value, Address):
        # Match a Tag whose address.raw matches
        for tag in prog.tags.values():
            if tag.address is not None and tag.address.raw == value.raw:
                return tag.data_type.value
        return None
    if isinstance(value, TagRef):
        return env.get(value.name)
    return None  # literal / something else


def _producer_pin_type(element, pin_name: Optional[str],
                       prog: Program,
                       env: dict[str, str]) -> Optional[str]:
    """Type of the output pin that ``element`` exposes (the
    producer side of an FBD wire).

    Element kinds:
      - InVariable / InOutVariable: parse ``expression`` --
        treats it as a variable name (look up in env) or as a
        literal (skip).
      - FbBlock: look up the referenced POU in
        ``Program.subroutines``.  If it's a user-defined
        FUNCTION_BLOCK / FUNCTION, find the matching output Var
        by formal-parameter name (or use the FUNCTION's
        ``return_type`` if the pin name matches the convention
        "OUT" / unspecified).

    Returns ``None`` when the producer is a built-in (TON / ADD /
    etc. -- no signature database yet), an unresolved variable
    name, or a literal expression -- callers skip the check.
    """
    from .il import (
        FbBlock, InOutVariable, InVariable, OutVariable,
    )
    if isinstance(element, (InVariable, InOutVariable)):
        # Heuristic: look up the operand as a variable name.
        # Literals and complex expressions return None.
        expr = element.expression.strip()
        # Strip any IEC unary minus / NOT prefix
        if expr.startswith("-") or expr.upper().startswith("NOT "):
            return None
        return env.get(expr)
    if isinstance(element, FbBlock):
        callee = prog.find_subroutine(element.type_name)
        if callee is None:
            return None
        # Find the named output pin
        if pin_name is not None:
            for v in callee.outputs + callee.in_outs:
                if v.name == pin_name:
                    resolved = _resolve_named_type(prog, v.data_type)
                    return _tagtype_name(resolved)
            # No matching output -- might be the FUNCTION return slot
        if callee.kind is PouKind.FUNCTION and callee.return_type is not None:
            resolved = _resolve_named_type(prog, callee.return_type)
            return _tagtype_name(resolved)
        return None
    return None


def _consumer_pin_type(block, formal_parameter: str,
                       prog: Program) -> Optional[str]:
    """Type the block's named input pin expects.

    Only user-defined POU calls resolve -- we look up the callee
    in ``Program.subroutines`` and find the matching VAR_INPUT /
    VAR_IN_OUT.  Builtin block names (TON, ADD, etc.) return
    None until we plumb in a builtin signature database.
    """
    callee = prog.find_subroutine(block.type_name)
    if callee is None:
        return None
    for v in callee.inputs + callee.in_outs:
        if v.name == formal_parameter:
            resolved = _resolve_named_type(prog, v.data_type)
            return _tagtype_name(resolved)
    return None


def _check_fbd_pin_types(prog: Program) -> list[ValidationError]:
    """Type-check FBD pin connections against the referenced POU's
    interface (user-defined FUNCTION / FUNCTION_BLOCK only).

    For each ``FbBlock`` whose ``type_name`` resolves to a
    declared POU, walk every input pin:

      1. Look up the consumer's expected type from the callee's
         VAR_INPUT / VAR_IN_OUT.
      2. Resolve the producer's output type:
           - InVariable / InOutVariable: variable-name lookup
           - FbBlock pointing to a user-defined POU: output
             var lookup
      3. If both sides resolve and the buckets don't match,
         emit ``fbd-pin-type-mismatch``.

    Builtin blocks (TON / ADD / AND / etc.) are skipped pending
    a signature database -- their pin types are polymorphic and
    vendor-specific.
    """
    from .il import (
        FbBlock, FbdNetwork,
    )
    errors: list[ValidationError] = []

    for sub in prog.subroutines:
        net: Optional[FbdNetwork] = sub.fbd_body
        if net is None:
            continue
        env = _build_type_env(prog, sub)
        elements_by_id: dict[int, object] = {
            e.local_id: e for e in net.elements
        }

        for block in net.elements:
            if not isinstance(block, FbBlock):
                continue
            # Only check user-defined POU calls
            if prog.find_subroutine(block.type_name) is None:
                continue
            for pin in block.inputs + block.in_outs:
                conn = pin.connection
                if conn is None:
                    continue
                consumer_t = _consumer_pin_type(
                    block, pin.formal_parameter, prog,
                )
                if consumer_t is None:
                    continue
                producer = elements_by_id.get(conn.source_id)
                if producer is None:
                    # Unresolved connection -- structural check
                    # already flagged it
                    continue
                producer_t = _producer_pin_type(
                    producer, conn.source_pin, prog, env,
                )
                if producer_t is None:
                    continue
                if not _are_types_compatible(consumer_t, producer_t):
                    errors.append(ValidationError(
                        code="fbd-pin-type-mismatch",
                        message=(
                            f"FBD pin "
                            f"{block.type_name!r}.{pin.formal_parameter!r} "
                            f"expects type {consumer_t!r} but "
                            f"connection from local_id "
                            f"{conn.source_id} carries "
                            f"{producer_t!r}"
                        ),
                        location=(f"Subroutine '{sub.name}' / "
                                   f"fbd_body / block "
                                   f"{block.local_id}"),
                    ))

    return errors


def _check_op_types(prog: Program) -> list[ValidationError]:
    """Type-check the rung ops in every POU's body.

    Checks (IEC §6.5):

      - move-type-mismatch        : Move src and dst aren't
                                     compatible.
      - binary-math-non-numeric   : One side of BinaryMath isn't
                                     in the numeric family.
      - binary-math-type-mismatch : dst type doesn't share a
                                     bucket with the operand types.
      - compare-type-mismatch     : Compare operands sit in
                                     different buckets.
      - coil-target-not-bool      : OutCoil/OutSet/OutReset target
                                     isn't BOOL.

    Each error names the offending subroutine + rung index.
    """
    from .il.ops import (
        BinaryMath, Compare, Move, OutCoil, OutReset, OutSet,
    )
    errors: list[ValidationError] = []

    for sub in prog.subroutines:
        env = _build_type_env(prog, sub)
        for rung_idx, rung in enumerate(sub.rungs):
            loc = f"Subroutine '{sub.name}' / rung {rung_idx}"
            for op in rung.ops:
                if isinstance(op, Move):
                    src_t = _operand_type(prog, env, op.src)
                    dst_t = _operand_type(prog, env, op.dst)
                    if not _are_types_compatible(src_t, dst_t):
                        errors.append(ValidationError(
                            code="move-type-mismatch",
                            message=(f"Move src type {src_t!r} not "
                                     f"compatible with dst type "
                                     f"{dst_t!r}"),
                            location=loc,
                        ))
                elif isinstance(op, BinaryMath):
                    lhs_t = _operand_type(prog, env, op.lhs)
                    rhs_t = _operand_type(prog, env, op.rhs)
                    dst_t = _operand_type(prog, env, op.dst)
                    if not _is_numeric(lhs_t):
                        errors.append(ValidationError(
                            code="binary-math-non-numeric",
                            message=(f"BinaryMath lhs has non-numeric "
                                     f"type {lhs_t!r}"),
                            location=loc,
                        ))
                    if not _is_numeric(rhs_t):
                        errors.append(ValidationError(
                            code="binary-math-non-numeric",
                            message=(f"BinaryMath rhs has non-numeric "
                                     f"type {rhs_t!r}"),
                            location=loc,
                        ))
                    if (not _are_types_compatible(lhs_t, dst_t)
                            or not _are_types_compatible(rhs_t, dst_t)):
                        errors.append(ValidationError(
                            code="binary-math-type-mismatch",
                            message=(f"BinaryMath dst type {dst_t!r} "
                                     f"not compatible with operand "
                                     f"types lhs={lhs_t!r} "
                                     f"rhs={rhs_t!r}"),
                            location=loc,
                        ))
                elif isinstance(op, Compare):
                    lhs_t = _operand_type(prog, env, op.lhs)
                    rhs_t = _operand_type(prog, env, op.rhs)
                    if not _are_types_compatible(lhs_t, rhs_t):
                        errors.append(ValidationError(
                            code="compare-type-mismatch",
                            message=(f"Compare operands have "
                                     f"incompatible types lhs={lhs_t!r} "
                                     f"rhs={rhs_t!r}"),
                            location=loc,
                        ))
                elif isinstance(op, (OutCoil, OutSet, OutReset)):
                    addr_t = _operand_type(prog, env, op.address)
                    if not _is_bool(addr_t):
                        errors.append(ValidationError(
                            code="coil-target-not-bool",
                            message=(f"{type(op).__name__} target has "
                                     f"non-BOOL type {addr_t!r}"),
                            location=loc,
                        ))
    return errors


# -----------------------------------------------------------------------------
# Semantic type checking on ST AST (IEC §3)
# -----------------------------------------------------------------------------


#: Map from ST ``Literal.kind`` -> IEC type-name string.  Used by
#: the ST type inferrer; covers the literal kinds the builder
#: produces and the parser emits.  Unknown kinds (typed literals
#: like ``T#100ms``, ``16#FF``) fall through to None -- the type
#: check skips comparisons that involve them.
_LITERAL_KIND_TO_TYPE = {
    "bool":   "BOOL",
    "int":    "INT",
    "real":   "REAL",
    "string": "STRING",
}


#: Return-type table for IEC §2.5.2 standard functions with a
#: fixed (non-polymorphic) result type.  Polymorphic functions
#: (ABS, MIN, MAX, SEL, LIMIT, MUX -- result type depends on input)
#: aren't in the table; their FunctionCallExpr leaves the inferrer
#: returning ``None`` so callers skip the check.
_STDLIB_FIXED_RETURN_TYPES: dict[str, str] = {
    # §2.5.2.4 numerical (transcendentals all yield REAL)
    "SQRT": "REAL", "LN":   "REAL", "LOG":  "REAL", "EXP":  "REAL",
    "SIN":  "REAL", "COS":  "REAL", "TAN":  "REAL",
    "ASIN": "REAL", "ACOS": "REAL", "ATAN": "REAL",
    # §2.5.2.9 character-string -- LEN returns INT; the rest
    # return STRING.
    "LEN":    "INT",
    "LEFT":   "STRING", "RIGHT":   "STRING", "MID":     "STRING",
    "CONCAT": "STRING", "INSERT":  "STRING", "DELETE":  "STRING",
    "REPLACE": "STRING", "FIND":   "INT",
    # §2.5.2.10 time/date arithmetic
    "ADD_TIME":  "TIME", "SUB_TIME": "TIME",
    "MUL_TIME":  "TIME", "DIV_TIME": "TIME",
    "MULTIME":   "TIME", "DIVTIME":  "TIME",
    "ADD_TOD_TIME": "TOD", "SUB_TOD_TIME": "TOD",
    "ADD_DT_TIME":  "DT",  "SUB_DT_TIME":  "DT",
    "SUB_DATE_DATE": "TIME", "SUB_TOD_TOD": "TIME", "SUB_DT_DT": "TIME",
    "CONCAT_DATE_TOD": "DT",
    "DT_TO_DATE": "DATE", "DT_TO_TOD": "TOD", "DT_TO_TIME": "TIME",
    # TRUNC defaults to DINT per IEC
    "TRUNC": "DINT",
}


#: Polymorphic IEC §2.5.2 builtins whose return type depends on
#: a specific operand's type.  The integer in the tuple is the
#: 0-based index of the "controlling" argument; the result type
#: tracks that arg's inferred type.
#:
#: For ABS/MIN/MAX, the controlling arg is the first numeric
#: input.  For SEL(g, in0, in1), the controlling arg is ``in0``
#: (index 1) -- IEC requires in0 and in1 to match, so picking
#: either works.  For LIMIT(lo, value, hi), the controlling arg
#: is ``value`` (index 1).  For MUX(k, in0, in1, ...), the
#: controlling arg is the first ``inN`` (index 1) -- IEC requires
#: all inN to share a type.
_POLYMORPHIC_BUILTINS: dict[str, int] = {
    "ABS":   0,
    "MIN":   0,
    "MAX":   0,
    "SEL":   1,
    "LIMIT": 1,
    "MUX":   1,
}


def _function_return_type(name: str, prog: Program,
                          call_expr=None,
                          env: Optional[dict[str, str]] = None,
                          sub: Optional[Subroutine] = None,
                          ) -> Optional[str]:
    """Resolve a function-call name to its return type-name.

    Lookup order:

      1. User-defined FUNCTION POUs in ``Program.subroutines``.
      2. IEC type-conversion convention: ``<SRC>_TO_<DST>`` and
         ``<SRC>_TRUNC_<DST>`` -- the destination drives the
         result type.  ``REAL_TRUNC_INT`` returns INT, etc.
      3. The fixed-return-type table for §2.5.2 builtins with
         a known result.
      4. Polymorphic builtins (ABS / MIN / MAX / SEL / LIMIT /
         MUX) inherit their return type from a controlling
         operand.  Requires ``call_expr`` / ``env`` / ``sub`` to
         walk into the args; without them, polymorphic names
         resolve to ``None`` (skip).

    Returns ``None`` for unknown / vendor-extension / FB names.
    """
    # 1. User-defined FUNCTION
    callee = prog.find_subroutine(name)
    if callee is not None and callee.kind is PouKind.FUNCTION:
        if callee.return_type is not None:
            # ``return_type`` is a DataType; resolve via the
            # existing helpers.
            resolved = _resolve_named_type(prog, callee.return_type)
            return _tagtype_name(resolved)
        return None
    # 2. Type-conversion convention: <SRC>_TO_<DST>
    if "_TO_" in name:
        dst = name.rsplit("_TO_", 1)[-1]
        # Validate against the elementary-type set so we don't
        # mis-parse ``MY_TO_VENDOR`` as a real conversion.
        from .il import TagType
        try:
            TagType(dst)
            return dst
        except ValueError:
            pass
    # ``<SRC>_TRUNC_<DST>`` -- destination is the part after
    # ``_TRUNC_``
    if "_TRUNC_" in name:
        dst = name.rsplit("_TRUNC_", 1)[-1]
        from .il import TagType
        try:
            TagType(dst)
            return dst
        except ValueError:
            pass
    # 3. Fixed table
    fixed = _STDLIB_FIXED_RETURN_TYPES.get(name)
    if fixed is not None:
        return fixed
    # 4. Polymorphic builtins: walk into the controlling arg
    if (name in _POLYMORPHIC_BUILTINS
            and call_expr is not None
            and env is not None):
        idx = _POLYMORPHIC_BUILTINS[name]
        args = call_expr.positional
        if 0 <= idx < len(args):
            return _infer_st_expr_type(args[idx], env, prog, sub)
    return None


def _resolve_subrange(prog: Program, type_obj):
    """Walk a ``NamedType`` / ``AliasType`` chain looking for an
    underlying ``SubrangeType``.  Returns it on hit; ``None`` on
    miss (the type resolves to a non-subrange).

    Unlike ``_resolve_named_type`` (which collapses SubrangeType
    to its base), this helper preserves the subrange so callers
    can read its ``lower`` / ``upper`` bounds.
    """
    from .il import AliasType, NamedType, SubrangeType
    seen: set[str] = set()
    cur = type_obj
    while True:
        if isinstance(cur, SubrangeType):
            return cur
        if isinstance(cur, NamedType):
            if cur.name in seen:
                return None
            seen.add(cur.name)
            nxt = prog.find_user_type(cur.name)
            if nxt is None:
                return None
            cur = nxt
            continue
        if isinstance(cur, AliasType):
            if isinstance(cur.base, NamedType):
                if cur.base.name in seen:
                    return None
                seen.add(cur.base.name)
                nxt = prog.find_user_type(cur.base.name)
                cur = nxt if nxt is not None else cur.base
            else:
                cur = cur.base
            continue
        return None


def _literal_int_value(expr) -> Optional[int]:
    """Extract an integer value from a ``Literal`` expression, or
    ``None`` for non-literal / non-integer / unparseable forms.

    Handles:
      - ``Literal("42", kind="int")`` -> 42
      - ``Literal("-5", kind="int")`` -> -5 (negative-literal fold
        produced by the ST parser)
      - ``Literal("16#FF", kind="typed")`` -> 255 (based-int)
      - ``Literal("FALSE"|"TRUE", kind="bool")`` -> 0 / 1

    REAL literals, strings, time literals, etc. return ``None``.
    """
    from .il import Literal
    if not isinstance(expr, Literal):
        return None
    if expr.kind == "int":
        try:
            return int(expr.value)
        except ValueError:
            return None
    if expr.kind == "bool":
        return 1 if expr.value == "TRUE" else 0
    if expr.kind == "typed":
        # Based-int form: ``<base>#<digits>``
        text = expr.value
        if "#" in text:
            base_s, _, digits = text.partition("#")
            try:
                base = int(base_s)
                if 2 <= base <= 36:
                    return int(digits.replace("_", ""), base)
            except ValueError:
                return None
    return None


def _lvalue_subrange(target, sub: Optional[Subroutine],
                     prog: Program):
    """Find the ``SubrangeType`` (if any) for an Assignment
    target.  Mirrors ``_lvalue_type``'s shape:

      - ``VarRef`` : look up the variable's declared type, walk
                     to a SubrangeType via ``_resolve_subrange``.
      - ``FieldAccess`` / ``IndexAccess`` : drill through the
        chain via ``_resolve_chain_datatype`` (a SubrangeType-
        preserving variant of ``_resolve_chain_type``).

    Returns the ``SubrangeType`` or ``None``.
    """
    from .il import FieldAccess, IndexAccess, TagRef, VarRef
    if isinstance(target, VarRef):
        if sub is None or not isinstance(target.ref, TagRef):
            # Address-keyed lookup uses Program.tags's data_type,
            # which is ``TagType`` -- never a SubrangeType.
            return None
        name = target.ref.name
        for v in (sub.inputs + sub.outputs + sub.in_outs
                    + sub.local_vars):
            if v.name == name:
                return _resolve_subrange(prog, v.data_type)
        return None
    if isinstance(target, (FieldAccess, IndexAccess)):
        if sub is None:
            return None
        return _resolve_chain_subrange(target, sub, prog)
    return None


def _resolve_chain_subrange(expr, sub: Subroutine, prog: Program):
    """Subrange-preserving variant of ``_resolve_chain_type``.

    Walks the same ``FieldAccess`` / ``IndexAccess`` chain but
    returns the leaf ``SubrangeType`` (or None) instead of a
    collapsed type-name string.  Used by ``_lvalue_subrange``.
    """
    from .il import (
        ArrayType, FieldAccess, IndexAccess, StructType,
    )
    steps: list = []
    cur = expr
    while isinstance(cur, (FieldAccess, IndexAccess)):
        steps.append(cur)
        cur = cur.base
    steps.reverse()

    cur_type = _root_var_datatype(expr, sub, prog)
    if cur_type is None:
        return None

    for step in steps:
        cur_type = _resolve_named_type(prog, cur_type)
        if isinstance(step, FieldAccess):
            if not isinstance(cur_type, StructType):
                return None
            member = None
            for m in cur_type.members:
                if m.name == step.field:
                    member = m
                    break
            if member is None:
                return None
            cur_type = member.data_type
        elif isinstance(step, IndexAccess):
            if not isinstance(cur_type, ArrayType):
                return None
            cur_type = cur_type.element_type

    return _resolve_subrange(prog, cur_type)


def _root_var_datatype(expr, sub: Subroutine, prog: Program):
    """Walk a chained FieldAccess / IndexAccess expression back to
    its root ``VarRef``, then look up the variable's full
    ``DataType`` (not just type-name string).

    Returns the resolved ``DataType`` (with ``NamedType`` /
    ``AliasType`` chains followed via ``_resolve_named_type``), or
    ``None`` if the chain doesn't bottom out at a known variable.
    """
    from .il import FieldAccess, IndexAccess, TagRef, VarRef
    # Drill to the leaf VarRef
    cur = expr
    while isinstance(cur, (FieldAccess, IndexAccess)):
        cur = cur.base
    if not isinstance(cur, VarRef):
        return None
    if not isinstance(cur.ref, TagRef):
        return None  # Address-targeted struct access is unusual; skip
    name = cur.ref.name

    # Find the declared variable, preferring locals over globals.
    for v in (sub.inputs + sub.outputs + sub.in_outs + sub.local_vars):
        if v.name == name:
            return _resolve_named_type(prog, v.data_type)
    tag = prog.tags.get(name)
    if tag is not None:
        return tag.data_type
    return None


def _resolve_chain_type(expr, sub: Subroutine,
                        prog: Program) -> Optional[str]:
    """Resolve a chained ``FieldAccess`` / ``IndexAccess`` expression
    through ``Program.user_types`` and return the leaf type-name.

    Walks ``.field`` steps as StructType member lookups, ``[idx]``
    steps as ArrayType element accesses.  Each step resolves
    ``NamedType`` / ``AliasType`` chains.  ``None`` on any failure
    (unknown root, missing member, non-struct field access, etc.)
    so callers treat the operand as "type unknown" instead of
    raising false positives.
    """
    from .il import (
        ArrayType, FieldAccess, IndexAccess, StructType, VarRef,
    )
    # Collect the chain of steps from leaf-up, in source order
    steps: list = []
    cur = expr
    while isinstance(cur, (FieldAccess, IndexAccess)):
        steps.append(cur)
        cur = cur.base
    steps.reverse()  # now in apply order: outermost-first to innermost-last? No -- want innermost first.
    # Actually we built leaf-to-root, then reversed: that's root-to-leaf.
    # Apply each step in document order (root . step1 . step2 . ...).

    cur_type = _root_var_datatype(expr, sub, prog)
    if cur_type is None:
        return None

    for step in steps:
        cur_type = _resolve_named_type(prog, cur_type)
        if isinstance(step, FieldAccess):
            if not isinstance(cur_type, StructType):
                return None
            member = None
            for m in cur_type.members:
                if m.name == step.field:
                    member = m
                    break
            if member is None:
                return None
            cur_type = _resolve_named_type(prog, member.data_type)
        elif isinstance(step, IndexAccess):
            if not isinstance(cur_type, ArrayType):
                return None
            cur_type = _resolve_named_type(prog, cur_type.element_type)

    return _tagtype_name(cur_type)


def _infer_st_expr_type(expr, env: dict[str, str],
                         prog: Program,
                         sub: Optional[Subroutine] = None) -> Optional[str]:
    """Best-effort IEC type-name inference for an ST Expression.

    Returns ``None`` when the type can't be determined (function
    calls, unknown literal forms, unresolved chains).  Callers
    treat ``None`` as "skip the check" -- a missed diagnosis
    beats a false positive.

    ``sub`` is required to resolve ``FieldAccess`` / ``IndexAccess``
    chains through the POU's variable interface and the
    ``Program.user_types`` registry.  When omitted, those
    expression kinds return ``None`` (legacy behaviour).
    """
    from .il import (
        BinaryExpr, BinaryOp, FieldAccess, FunctionCallExpr, IndexAccess,
        Literal, UnaryExpr, UnaryOp, VarRef,
    )
    if isinstance(expr, Literal):
        return _LITERAL_KIND_TO_TYPE.get(expr.kind)
    if isinstance(expr, VarRef):
        return _operand_type(prog, env, expr.ref)
    if isinstance(expr, (FieldAccess, IndexAccess)):
        if sub is None:
            return None
        return _resolve_chain_type(expr, sub, prog)
    if isinstance(expr, UnaryExpr):
        operand_t = _infer_st_expr_type(expr.operand, env, prog, sub)
        if expr.op is UnaryOp.NOT:
            # NOT preserves the family: BOOL stays BOOL, integers
            # behave as bitwise-NOT.
            return operand_t
        # NEG preserves numeric type
        return operand_t
    if isinstance(expr, BinaryExpr):
        lhs_t = _infer_st_expr_type(expr.lhs, env, prog, sub)
        rhs_t = _infer_st_expr_type(expr.rhs, env, prog, sub)
        # Comparison operators yield BOOL regardless of operand
        # types (subject to operand-compatibility, which the
        # check pass enforces separately).
        comparison_ops = {
            BinaryOp.EQ, BinaryOp.NE, BinaryOp.LT, BinaryOp.LE,
            BinaryOp.GT, BinaryOp.GE,
        }
        if expr.op in comparison_ops:
            return "BOOL"
        # Logical operators take BOOL inputs (or bit-strings) and
        # produce the same family.  Preserve lhs type.
        return lhs_t or rhs_t
    if isinstance(expr, FunctionCallExpr):
        return _function_return_type(
            expr.name, prog, call_expr=expr, env=env, sub=sub,
        )
    # Otherwise: unhandled expression kind -> skip.
    return None


def _check_st_types(prog: Program) -> list[ValidationError]:
    """Type-check ST AST statements in every POU's ``st_body``.

    Emits:

      - st-assignment-type-mismatch  : Assignment.target and
                                        Assignment.value live in
                                        different IEC §6.5 buckets.
      - st-condition-not-bool        : IF / WHILE / REPEAT
                                        condition isn't BOOL.
      - st-for-index-not-numeric     : FOR loop's index variable
                                        isn't in the integer
                                        family (REAL not allowed
                                        per IEC §3.3.2.4).
      - st-for-bound-not-numeric     : FOR start/end/step isn't
                                        numeric.

    Method bodies (``Subroutine.methods[*].st_body``) get checked
    too, using the method's variable interface unioned with the
    enclosing FB's locals.
    """
    from .il import (
        Assignment, CaseStatement, FieldAccess, ForStatement,
        IfStatement, IndexAccess, RepeatStatement, VarRef,
        WhileStatement,
    )
    errors: list[ValidationError] = []

    def _check_block(stmts, env: dict[str, str],
                       sub: Subroutine, where: str) -> None:
        for s in stmts:
            if isinstance(s, Assignment):
                target_t = _lvalue_type(s.target, env, prog, sub)
                value_t = _infer_st_expr_type(s.value, env, prog, sub)
                if (target_t is not None and value_t is not None
                        and not _are_types_compatible(target_t, value_t)):
                    errors.append(ValidationError(
                        code="st-assignment-type-mismatch",
                        message=(f"ST assignment target type "
                                 f"{target_t!r} not compatible with "
                                 f"value type {value_t!r}"),
                        location=where,
                    ))
                # Subrange range check: if the target's declared
                # type is a SubrangeType and the value is a
                # literal integer, verify the literal sits in
                # [lower, upper].  Non-literal RHS (variables,
                # expressions) can't be checked without value-
                # flow analysis -- a follow-up slice.
                subrange = _lvalue_subrange(s.target, sub, prog)
                if subrange is not None:
                    lit_val = _literal_int_value(s.value)
                    if (lit_val is not None
                            and not (subrange.lower <= lit_val
                                       <= subrange.upper)):
                        errors.append(ValidationError(
                            code="subrange-out-of-range",
                            message=(f"literal value {lit_val} is "
                                     f"outside SUBRANGE "
                                     f"{subrange.name!r} bounds "
                                     f"[{subrange.lower}, "
                                     f"{subrange.upper}]"),
                            location=where,
                        ))
            elif isinstance(s, IfStatement):
                for cond, body in s.branches:
                    cond_t = _infer_st_expr_type(cond, env, prog, sub)
                    if cond_t is not None and not _is_bool(cond_t):
                        errors.append(ValidationError(
                            code="st-condition-not-bool",
                            message=(f"IF condition has non-BOOL "
                                     f"type {cond_t!r}"),
                            location=where,
                        ))
                    _check_block(body, env, sub, where)
                if s.else_branch is not None:
                    _check_block(s.else_branch, env, sub, where)
            elif isinstance(s, WhileStatement):
                cond_t = _infer_st_expr_type(s.condition, env, prog, sub)
                if cond_t is not None and not _is_bool(cond_t):
                    errors.append(ValidationError(
                        code="st-condition-not-bool",
                        message=(f"WHILE condition has non-BOOL "
                                 f"type {cond_t!r}"),
                        location=where,
                    ))
                _check_block(s.body, env, sub, where)
            elif isinstance(s, RepeatStatement):
                _check_block(s.body, env, sub, where)
                cond_t = _infer_st_expr_type(s.until, env, prog, sub)
                if cond_t is not None and not _is_bool(cond_t):
                    errors.append(ValidationError(
                        code="st-condition-not-bool",
                        message=(f"REPEAT UNTIL condition has "
                                 f"non-BOOL type {cond_t!r}"),
                        location=where,
                    ))
            elif isinstance(s, ForStatement):
                # IEC §3.3.2.4: index var must be an integer
                # type specifically (REAL not allowed).
                idx_t = env.get(s.index_var)
                if idx_t is not None and idx_t not in _INT_TYPES:
                    errors.append(ValidationError(
                        code="st-for-index-not-numeric",
                        message=(f"FOR index {s.index_var!r} has "
                                 f"non-integer type {idx_t!r}"),
                        location=where,
                    ))
                for bound, label in (
                    (s.start, "start"),
                    (s.end,   "end"),
                    (s.step,  "step"),
                ):
                    if bound is None:
                        continue
                    bt = _infer_st_expr_type(bound, env, prog, sub)
                    if bt is not None and not _is_numeric(bt):
                        errors.append(ValidationError(
                            code="st-for-bound-not-numeric",
                            message=(f"FOR {label} bound has "
                                     f"non-numeric type {bt!r}"),
                            location=where,
                        ))
                _check_block(s.body, env, sub, where)
            elif isinstance(s, CaseStatement):
                # Descend into clause bodies + else; selector /
                # label type checks are deferred (literal labels
                # often use typed-prefix forms whose types are
                # only loosely modelled).
                for clause in s.clauses:
                    _check_block(clause.body, env, sub, where)
                if s.else_branch is not None:
                    _check_block(s.else_branch, env, sub, where)

    for sub in prog.subroutines:
        env = _build_type_env(prog, sub)
        if sub.st_body is not None:
            _check_block(sub.st_body, env, sub,
                          f"Subroutine '{sub.name}' / st_body")
        for m in sub.methods:
            if m.st_body is None:
                continue
            # Build a synthetic Subroutine that exposes the
            # method's variable interface to the chain resolver.
            # We union the enclosing FB's vars with the method's
            # locals so both are in scope.
            method_sub = Subroutine(
                name=f"{sub.name}.{m.name}",
                kind=sub.kind,
                inputs=list(sub.inputs) + list(m.inputs),
                outputs=list(sub.outputs) + list(m.outputs),
                in_outs=list(sub.in_outs) + list(m.in_outs),
                local_vars=list(sub.local_vars) + list(m.local_vars),
            )
            method_env = dict(env)
            for v in (m.inputs + m.outputs + m.in_outs + m.local_vars):
                resolved = _resolve_named_type(prog, v.data_type)
                name = _tagtype_name(resolved)
                if name is not None:
                    method_env[v.name] = name
            _check_block(m.st_body, method_env, method_sub,
                          f"Subroutine '{sub.name}' / Method "
                          f"'{m.name}' / st_body")

    return errors


def _lvalue_type(target, env: dict[str, str],
                 prog: Program,
                 sub: Optional[Subroutine] = None) -> Optional[str]:
    """Resolve an Assignment.target's type-name.

    Targets per ``il.st.is_lvalue``:
      - ``VarRef(addr)``    : look up in env / Program.tags
      - ``FieldAccess``     : walk through StructType members via
                              ``_resolve_chain_type``
      - ``IndexAccess``     : walk through ArrayType element type
                              via ``_resolve_chain_type``
    """
    from .il import FieldAccess, IndexAccess, VarRef
    if isinstance(target, VarRef):
        return _operand_type(prog, env, target.ref)
    if isinstance(target, (FieldAccess, IndexAccess)):
        if sub is None:
            return None
        return _resolve_chain_type(target, sub, prog)
    return None


def _check_sfc_condition_types(prog: Program) -> list[ValidationError]:
    """Type-check the IL ops inside each ``Transition.condition``.

    The condition is a tuple of LD-style ops (ContactNO /
    ContactNC / ParallelGroup / Compare / etc.).  We apply the
    same IEC §6.5 rules as ``_check_op_types``:

      - sfc-contact-not-bool      : ContactNO / ContactNC target
                                     isn't BOOL.
      - sfc-compare-type-mismatch : Compare operands cross IEC
                                     §6.5 buckets.

    ``ParallelGroup`` (OR branches) recurses into each branch.
    Other op types are skipped -- SFC transition guards
    overwhelmingly use contacts + compares; math / call / coil
    ops on a transition would be a structural mistake the rung
    checker can't see but which is rare enough to defer.
    """
    from .il.ops import (
        Compare, ContactNC, ContactNO, ParallelGroup,
    )
    errors: list[ValidationError] = []

    def _walk_ops(ops, env: dict[str, str], where: str) -> None:
        for op in ops:
            if isinstance(op, (ContactNO, ContactNC)):
                addr_t = _operand_type(prog, env, op.address)
                if not _is_bool(addr_t):
                    errors.append(ValidationError(
                        code="sfc-contact-not-bool",
                        message=(f"SFC transition contact "
                                 f"{type(op).__name__} target has "
                                 f"non-BOOL type {addr_t!r}"),
                        location=where,
                    ))
            elif isinstance(op, Compare):
                lhs_t = _operand_type(prog, env, op.lhs)
                rhs_t = _operand_type(prog, env, op.rhs)
                if not _are_types_compatible(lhs_t, rhs_t):
                    errors.append(ValidationError(
                        code="sfc-compare-type-mismatch",
                        message=(f"SFC transition compare operands "
                                 f"have incompatible types "
                                 f"lhs={lhs_t!r} rhs={rhs_t!r}"),
                        location=where,
                    ))
            elif isinstance(op, ParallelGroup):
                for branch in op.branches:
                    _walk_ops(branch, env, where)

    for sub in prog.subroutines:
        if sub.sfc is None:
            continue
        env = _build_type_env(prog, sub)
        for tr_idx, tr in enumerate(sub.sfc.transitions):
            where = (f"Subroutine '{sub.name}' / SFC transition "
                       f"{tr_idx} ({'+'.join(tr.from_steps)} -> "
                       f"{'+'.join(tr.to_steps)})")
            _walk_ops(tr.condition, env, where)

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
      9. Body kind mutex (rungs / sfc / st_body / fbd_body exactly one)
     10. ST Assignment.target is an lvalue
     11. ST FOR index variable is declared
     12. FBD well-formedness (local_id uniqueness, resolved
         connections, known source pins, known jump labels)
     13. ST Goto/Label resolution (no duplicates, every GOTO has
         a target in the same body)
     14. VAR_ACCESS / VAR_CONFIG well-formedness (direction valid,
         alias uniqueness, instance-path syntax)
     15. Semantic type checking on rung ops (IEC §6.5
         compatibility: Move src/dst, BinaryMath numericity,
         Compare operand symmetry, coil targets are BOOL)
     16. Semantic type checking on ST AST bodies (Assignment
         target↔value compat, IF/WHILE/REPEAT condition is BOOL,
         FOR index numericity, SUBRANGE literal-bounds checks)
     17. Semantic type checking on SFC transition conditions
         (contacts must be BOOL, compares must balance)
     18. Semantic type checking on FBD pin connections
         (producer output type compatible with consumer pin type
         for user-defined POU calls)
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
    errors.extend(_check_fbd_wellformedness(prog))
    errors.extend(_check_st_goto_labels(prog))
    errors.extend(_check_access_and_config_vars(prog))
    errors.extend(_check_op_types(prog))
    errors.extend(_check_st_types(prog))
    errors.extend(_check_sfc_condition_types(prog))
    errors.extend(_check_fbd_pin_types(prog))
    return errors


def is_valid(prog: Program) -> bool:
    """Convenience: ``True`` iff ``validate(prog)`` is empty."""
    return not validate(prog)
