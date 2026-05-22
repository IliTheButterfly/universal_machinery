# Conformance Test Plan

Promised by `docs/IEC_CONFORMANCE.md` §"Verification posture": a
mapping from each IEC 61131-3 conformance row to the concrete
test fixture(s) that exercise it.  Used as a checklist when adding
new constructs and as a public corpus that future certification
work (PLCopen-tool round-trip, hardware-in-the-loop) can build on.

Every row links to either a passing test file under `tests/` or a
follow-up that's tracked in `docs/IEC_CONFORMANCE.md`.  Test
counts are snapshotted; the current passing total is **1205 tests**.

The plan is self-auditing:
[`tests/test_conformance_plan_pointers.py`](../tests/test_conformance_plan_pointers.py)
parses every `tests/foo.py(::test_bar)?` pointer below and fails
CI if any of them resolve to a missing file or a missing test
function.  Wildcard pointers (`::test_prefix_*`) require at
least one matching test in the cited file.  The snapshot-count
line above must also stay consistent with the
`status: N / N passing` line at the bottom of this document.

## Reading this document

```
| IEC §   | Construct          | Status | Test fixtures        |
|---------|--------------------|--------|----------------------|
| §X.Y    | The thing          |   ✅   | tests/path::test_x   |
```

Status legend:

| Status | Meaning                                                       |
|--------|---------------------------------------------------------------|
| ✅     | Implemented + tested + on the round-trip path                 |
| ⚠️     | Partial: implemented in some directions but not all           |
| 📋     | Tested via XSD validation only (no IL ↔ IL round-trip yet)    |
| ❌     | Not implemented                                                |

## §2.2 Program Organization Units (POUs)

| IEC §   | Construct        | Status | Test fixtures |
|---------|------------------|--------|---------------|
| §2.2    | PROGRAM          | ✅     | tests/il/test_pou_db_sfc.py, tests/parsers/test_plcopen_xml_reader.py::test_round_trip_program_with_inputs_outputs_locals |
| §2.2    | FUNCTION         | ✅     | tests/il/test_pou_db_sfc.py, tests/parsers/test_plcopen_xml_reader.py::test_round_trip_function_preserves_return_type |
| §2.2    | FUNCTION_BLOCK   | ✅     | tests/il/test_pou_db_sfc.py, tests/parsers/test_plcopen_xml_reader.py::test_round_trip_function_block_with_in_out |
| §2.5.1.5| METHOD (3rd ed.) | ⚠️     | tests/il/test_oop.py — ST + JSON only; doubly blocked for cert (no PLCopen v2.02 XSD + matiec rejects METHOD as a 2nd-edition compiler) |
| §2.5.1.5| INTERFACE        | ⚠️     | tests/il/test_oop.py — same doubly-blocked posture |
| §2.5.1.5| EXTENDS          | ⚠️     | tests/il/test_oop.py — matiec rejects `EXTENDS` |
| §2.5.1.5| IMPLEMENTS       | ⚠️     | tests/il/test_oop.py — depends on INTERFACE |
| §2.5.1.5| ABSTRACT         | ⚠️     | tests/il/test_oop.py — depends on METHOD |

## §2.3.3 User-defined types

| IEC §    | Construct           | Status | Test fixtures |
|----------|---------------------|--------|---------------|
| §2.3.3   | STRUCT              | ✅     | tests/il/test_user_types.py, tests/parsers/test_plcopen_xml_reader_udt.py::test_struct_type_round_trips |
| §2.3.3   | ARRAY (1-D)         | ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_array_type_single_dim_round_trips |
| §2.3.3   | ARRAY (multi-dim)   | ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_array_type_multi_dim_round_trips |
| §2.3.3   | ENUM                | ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_enum_type_round_trips |
| §2.3.3.1 | SUBRANGE (signed)   | ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_subrange_type_signed_round_trips |
| §2.3.3.1 | SUBRANGE (unsigned) | ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_subrange_type_unsigned_round_trips |
| §2.3.3   | ALIAS (elementary)  | ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_alias_type_of_elementary_round_trips |
| §2.3.3   | ALIAS (of NamedType)| ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_alias_type_of_named_type_round_trips |
| §2.3.3   | NamedType reference | ✅     | tests/parsers/test_plcopen_xml_reader_udt.py::test_pou_with_all_three_directions_resolving_to_udts |

## §2.4 Variables

| IEC §    | Construct                  | Status | Test fixtures |
|----------|----------------------------|--------|---------------|
| §2.4.1.1 | Direct representation (%I/%Q/%M ×B/W/D/L/X) | ✅ | tests/il/test_il.py — direct-rep coverage |
| §2.4.3   | VAR_INPUT                  | ✅     | tests/il/test_pou_db_sfc.py |
| §2.4.3   | VAR_OUTPUT                 | ✅     | tests/il/test_pou_db_sfc.py |
| §2.4.3   | VAR_IN_OUT                 | ✅     | tests/il/test_pou_db_sfc.py |
| §2.4.3   | VAR (local)                | ✅     | tests/il/test_pou_db_sfc.py |
| §2.4.3   | VAR_EXTERNAL               | ✅     | `Subroutine.external_vars: list[Var]` -> `<externalVars>`; tests/parsers/test_plcopen_xml_reader.py — round-trip preserves names + types, stays distinct from locals / globals / temp |
| §2.4.3   | VAR_TEMP                   | ✅     | `Subroutine.temp_vars: list[Var]` -> `<tempVars>`; tests/parsers/test_plcopen_xml_reader.py — same round-trip + isolation guarantees as VAR_EXTERNAL |
| §2.4.3   | VAR_ACCESS                 | ✅     | tests/il/test_var_access_config.py, tests/parsers/test_plcopen_xml_reader.py::test_round_trip_access_vars_and_config_vars |
| §2.4.3.2 | VAR_CONFIG                 | ✅     | tests/il/test_var_access_config.py, tests/parsers/test_plcopen_xml_reader.py |
| §2.4.3   | VAR_GLOBAL (POU-scope)     | ✅     | `Subroutine.global_vars: list[Var]`; PLCopen XML emits `<globalVars>` block inside `<interface>`; tests/parsers/test_plcopen_xml_reader.py — round-trip preserves names, types, initial values, and keeps globals distinct from locals |
| §2.7.1   | VAR_GLOBAL (config-scope)  | ✅     | tests/il/test_var_access_config.py |
| §2.7.1   | VAR_GLOBAL (resource-scope)| ✅     | tests/il/test_configuration.py |

## §6.4 Elementary types

| IEC §  | Type   | Status | Test fixtures |
|--------|--------|--------|---------------|
| §6.4   | BOOL   | ✅ | tests/il/test_il.py, tests/parsers/test_plcopen_xml_reader.py |
| §6.4   | BYTE / WORD / DWORD / LWORD | ✅ | tests/il/test_elementary_types.py (LWORD round-trip), tests/il/test_std_function_registry.py |
| §6.4   | SINT / INT / DINT / LINT    | ✅ | tests/il/test_std_function_registry.py |
| §6.4   | USINT / UINT / UDINT / ULINT| ✅ | tests/il/test_std_function_registry.py |
| §6.4   | REAL / LREAL                | ✅ | tests/il/test_il.py |
| §6.4   | TIME / DATE / TOD / DT      | ✅ | tests/il/test_elementary_types.py — all four members in TagType with round-trip + XSD validation |
| §6.4   | STRING / WSTRING            | ✅ | tests/il/test_elementary_types.py — WSTRING round-trip + XSD-compliant lowercase `<string/>` / `<wstring/>` emission |

## §2.5.2 Standard library

| IEC §    | Family               | Status | Test fixtures |
|----------|----------------------|--------|---------------|
| §2.5.2.1 | Type conversions (`<SRC>_TO_<DST>`, all 420 pairs) | ✅ | tests/il/test_std_function_registry.py::test_all_pairs_count |
| §2.5.2.1 | BCD conversions      | ✅ | tests/il/test_std_function_registry.py::test_bcd_conversions_registered |
| §2.5.2.1 | TRUNC family         | ✅ | tests/il/test_std_function_registry.py::test_trunc_generic_and_typed_variants |
| §2.5.2.4 | Numerical (ABS/SQRT/LN/LOG/EXP/trig) | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.5 | Arithmetic (ADD/SUB/MUL/DIV/MOD)     | ✅ | tests/il/test_builders.py, tests/lowering/test_fbd_to_st.py |
| §2.5.2.6 | Bit-string (SHL/SHR/ROR/ROL)         | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.7 | Logical (AND/OR/XOR/NOT)             | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.8 | Selection (SEL/MAX/MIN/LIMIT/MUX)    | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.8 | Comparison (GT/GE/EQ/LE/LT/NE)       | ✅ | tests/il/test_builders.py |
| §2.5.2.9 | Character-string (LEN/LEFT/...)      | ✅ | tests/il/test_std_function_registry.py |
| §2.5.2.10| Time / date arithmetic               | ✅ | tests/il/test_std_function_registry.py::test_time_date_function_registered |

## §2.5.3 Standard function blocks

| IEC §      | FB                | Status | Test fixtures |
|------------|-------------------|--------|---------------|
| §2.5.2.3.1 | TON / TOF / TP    | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.3.2 | CTU / CTD / CTUD  | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.3.3 | R_TRIG / F_TRIG   | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.3.3 | SR / RS           | ✅ | tests/il/test_iec_fbs.py |
| §2.5.2.3.4 | Communication FBs | ❌ | Out of scope until fieldbus modeling lands |

## §2.6 SFC (Sequential Function Chart)

| Element                | Status | Test fixtures |
|------------------------|--------|---------------|
| Steps + initial flag   | ✅ | tests/il/test_pou_db_sfc.py, tests/emitters/test_plcopen_xml_sfc.py |
| Transitions            | ✅ | tests/emitters/test_plcopen_xml_sfc.py::test_round_trip_three_step_pipeline |
| Simultaneous convergence/divergence | ✅ | tests/emitters/test_plcopen_xml_sfc.py::test_simultaneous_divergence_emits_marker_with_per_branch_pins, test_simultaneous_convergence_emits_marker_with_multi_inputs |
| Inline ST conditions   | ✅ | tests/parsers/test_sfc_condition_lowering.py — full AND / NOT / OR lowering to LD ops |
| Named-reference cond.  | ✅ | tests/emitters/test_plcopen_xml_sfc.py::test_named_reference_condition_round_trips_as_textual_name |
| Explicit `<selectionDivergence>` / `<simultaneousDivergence>` / `<selectionConvergence>` / `<simultaneousConvergence>` markers | ✅ | tests/emitters/test_plcopen_xml_sfc.py — all four shapes emit + read with formalParameter pin wiring; reader traces through chained markers; combined diamond + fork-join networks round-trip |
| Action blocks (`<actionBlock>` with `connectionPointOutAction`) | ✅ | tests/emitters/test_plcopen_xml_sfc.py — all 12 IEC §2.6.4.4 qualifiers (N/R/S/L/D/P/P0/P1/DS/DL/SD/SL), `duration=` time literal round-trip, multi-action blocks |
| Inline action bodies (`<action><inline><ST>...</ST></inline></action>`) | ✅ | tests/emitters/test_plcopen_xml_sfc.py — `Action.inline_body: tuple[Statement, ...]` carries embedded ST AST; emit writes the body as ST text inside `<inline><ST><xhtml:pre>`, reader parses it back via `parse_st_body`; mixed inline + reference actions in one block round-trip; unparseable inline ST drops to empty rather than failing the read |
| `<jumpStep targetName=...>` | ✅ | tests/emitters/test_plcopen_xml_sfc.py — back-edge transitions auto-promote to `<jumpStep>` on emit; reader resolves `targetName` (incl. through marker indirection) into the transition's `to_steps`; dangling targets drop gracefully |
| `<macroStep>` (hierarchical sub-networks) | ✅ | tests/emitters/test_plcopen_xml_sfc.py — `Step.macro: Optional[SfcNetwork]` carries the nested network; emit wraps it in `<body><SFC>...</SFC></body>` with fresh per-body localId scope; reader recurses; arbitrary-depth nesting + mixed step/macroStep networks + JSON serialization all round-trip |

## §4 Languages

| Language | Status | Test fixtures |
|----------|--------|---------------|
| LD (Ladder Diagram) | ✅ | tests/emitters/test_plcopen_xml_ld.py — native `<LD>` emit + read; contacts (NO / NC), edge contacts (rising / falling via XSD `edge=`, including the `negated=true` × `edge=` combination via `ContactRisingEdge(..., negated=True)` / `ContactFallingEdge(..., negated=True)`), coils (regular / SET / RESET) all round-trip |
| LD `ParallelGroup` (OR branches inside rungs) | ✅ | tests/emitters/test_plcopen_xml_ld.py — multi-branch / multi-contact-per-branch / mixed NO+NC branches all round-trip via native `<LD>` multi-incoming wire shape |
| LD Compare ops (`<block typeName="GT|GE|EQ|LE|LT|NE">` in LD body) | ✅ | tests/emitters/test_plcopen_xml_ld.py — all six IEC §2.5.2.8 comparison symbols round-trip via inVariable × 2 + block; reader recognises hand-rolled blocks |
| LD Move op (`<block typeName="MOVE">` + `<inVariable>` + `<outVariable>` in LD body) | ✅ | tests/emitters/test_plcopen_xml_ld.py — IEC §2.5.2.1 MOVE round-trips with Address / TagRef / literal src; ENO continues rung gate; Compare + Move in one rung also round-trips |
| LD BinaryMath ops (`<block typeName="ADD|SUB|MUL|DIV|MOD">` + 2 inVariables + outVariable) | ✅ | tests/emitters/test_plcopen_xml_ld.py — all five IEC §2.5.2.5 arithmetic ops round-trip with Address / literal operands; gated rungs preserve EN wiring through the upstream contact |
| LD StdFunc ops (`<block typeName=NAME>` with variable IN/IN1..INn pins + outVariable) | ✅ | tests/emitters/test_plcopen_xml_ld.py — IEC §2.5.2 standard-library calls (ABS / SQRT / AND / OR / SEL / LIMIT / MUX / SHL / SHR / ...) round-trip; single-input form uses `IN` pin, multi-input uses `IN1`..`INn` |
| LD Call ops (POU invocation via `<block typeName=<target>>` with named formalParameter bindings) | ✅ | tests/emitters/test_plcopen_xml_ld.py — unparameterised subroutine, function with return_to, and FB call with `instanceName` + outputs all round-trip; function-return pin uses target name as formalParameter |
| LD timer FBs (TON / TOF / TP via `<block typeName=TON|TOF|TP instanceName=<addr>>`) | ✅ | tests/emitters/test_plcopen_xml_ld.py — IEC §2.5.2.3.1 timer family round-trips with IN <- rung gate, PT <- `T#<ms>ms` inVariable, Q/ET <- outVariables; multi-ms-range presets preserved |
| LD counter FBs (CTU / CTD / CTUD via `<block typeName=CTU|CTD|CTUD instanceName=<addr>>`) | ✅ | tests/emitters/test_plcopen_xml_ld.py — IEC §2.5.2.3.2 counter family round-trips; CTU's CU and CTD's CD come from the rung gate, all other bool inputs (R / LD, plus CTUD's CU/CD) come from auxiliary inVariables.  Reader picks up "orphan" CTUD rungs whose primary inputs don't trace back to leftRail |
| LD bistables + edge triggers (SR / RS / R_TRIG / F_TRIG via `<block typeName=... instanceName=<addr>>`) | ✅ | tests/emitters/test_plcopen_xml_ld.py — IEC §2.5.2.3.3 bistables (Q1 storage = instance name) + edge triggers (state = instance name).  All bool inputs come from auxiliary inVariables; Q / Q1 outputs to outVariables |
| LD control-flow ops (Jump / Label / Return via `<jump label=...>` / `<label label=...>` / `<return>`) | ✅ | tests/emitters/test_plcopen_xml_ld.py — IEC §6.6.4 jump/label/return primitives round-trip via the XSD's commonObjects group; Label rungs are picked up by the reader's "orphan element" second pass.  `End` is the only IL op still using ST-text fallback (no XSD element for "end of main program") |
| FBD (Function Block Diagram) | ✅ | tests/emitters/test_plcopen_xml_fbd.py, tests/parsers/test_plcopen_xml_reader_fbd.py, tests/lowering/test_fbd_to_st.py |
| ST (Structured Text) | ✅ | tests/il/test_st_ast.py, tests/parsers/test_st_text_parser.py — emit + parse round-trip |
| SFC | ✅ | tests/emitters/test_plcopen_xml_sfc.py |
| IL (Instruction List, deprecated) | ❌ | Out of scope -- deprecated in IEC 3rd ed. |

## §2.7 Configuration / Resource / Task

| Element | Status | Test fixtures |
|---------|--------|---------------|
| CONFIGURATION  | ✅ | tests/il/test_configuration.py, tests/parsers/test_plcopen_xml_reader.py |
| RESOURCE       | ✅ | tests/il/test_configuration.py |
| TASK (cyclic / single / interrupt) | ✅ | tests/parsers/test_plcopen_xml_reader.py::test_round_trip_configuration_with_task_and_pou_instance |
| PouInstance bound to task | ✅ | tests/parsers/test_plcopen_xml_reader.py |

## Round-trip integrity

| Direction                                  | Status | Test fixtures |
|--------------------------------------------|--------|---------------|
| IL → JSON → IL                             | ✅ | tests/il/test_serialisation.py |
| IL → ST → IL (via parser)                  | ✅ | tests/parsers/test_st_text_parser.py — 14 round-trip tests at the body level |
| IL → PLCopen XML → IL (POU + Configuration + UDTs) | ✅ | tests/parsers/test_plcopen_xml_reader.py + _udt.py |
| IL → PLCopen XML → IL (FBD body)           | ✅ | tests/parsers/test_plcopen_xml_reader_fbd.py |
| IL → PLCopen XML → IL (SFC body)           | ✅ | tests/emitters/test_plcopen_xml_sfc.py |
| IL → PLCopen XML → IL (LD body)            | ✅ | tests/emitters/test_plcopen_xml_ld.py |
| FBD → ST lowering                          | ✅ | tests/lowering/test_fbd_to_st.py |
| IL → ST → matiec ``iec2c`` parse-accept    | ✅ | tests/test_matiec_roundtrip.py — CI-skipped when matiec not installed; covers LD / TON / CTU / R_TRIG / SR / Compare+Move / BinaryMath / ABS / FB call / FUNCTION POU + call / jump+label / SFC (single-flow + simultaneous-convergence + timed actions) / UDTs (STRUCT field access + ARRAY index + ENUM literal) / ST control flow (IF-ELSE / CASE / FOR / WHILE / REPEAT) / CONFIGURATION + RESOURCE + TASK / direct rep AT clause (%IX/%QX) + vendor-AT comment fallback.  25/25 cases pass on a real matiec install. |

## XSD-level conformance

Every test file under `tests/emitters/test_plcopen_xml*.py` runs the
emitted XML through the bundled PLCopen TC6 v2.01 XSD via
`validate_plcopen_xml(xml)`.  Schema-level validity is verified for
every shape covered by the test corpus.

The XSD itself ships under
`universal_machinery/emitters/schemas/tc6_xml_v201.xsd`
(sourced from Beremiz's public mirror).

## Validation rules

The validator (`universal_machinery.validation.validate`) emits 31
distinct error codes covering:

| Code                              | What it catches |
|-----------------------------------|-----------------|
| `unresolved-tagref`               | TagRef without a matching Tag or POU parameter |
| `unresolved-named-type`           | NamedType reference to undeclared UDT |
| `unknown-call-target`             | Parameterised Call targeting an undeclared POU |
| `bad-input-binding` / `bad-output-binding` | Call parameter name not in callee's interface |
| `return-to-no-outputs`            | Call sets `return_to` but callee declares no VAR_OUTPUT |
| `call-graph-cycle`                | Cycle in the static call graph |
| `unknown-task` / `unknown-pou-type` | Bad Configuration cross-references |
| `extends-unknown-fb` / `implements-unknown-iface` | OOP extends / implements broken refs |
| `abstract-method-on-concrete-fb` / `abstract-method-has-body` / `interface-method-not-abstract` / `interface-method-has-body` | OOP shape rules |
| `multiple-body-kinds`             | More than one of rungs / sfc / st_body / fbd_body set |
| `bad-assignment-target`           | ST Assignment.target isn't an lvalue |
| `for-index-undeclared`            | ST FOR loop's index variable isn't declared |
| `sfc-issue`                       | Issues from SfcNetwork.validate() |
| `fbd-duplicate-local-id` / `fbd-unresolved-connection` / `fbd-unknown-source-pin` / `fbd-unknown-jump-label` | FBD graph well-formedness |
| `st-duplicate-label` / `st-unresolved-goto` | ST GOTO / label consistency |
| `access-var-bad-direction` / `access-var-duplicate-alias` / `access-var-bad-path` | VAR_ACCESS shape |
| `config-var-bad-path` / `config-var-duplicate-path` | VAR_CONFIG shape |
| `move-type-mismatch`              | Move src/dst types don't share an IEC §6.5 compat bucket |
| `binary-math-non-numeric`         | BinaryMath lhs or rhs isn't in integer/real family |
| `binary-math-type-mismatch`       | BinaryMath dst doesn't share a bucket with operand types |
| `compare-type-mismatch`           | Compare operands cross IEC §6.5 buckets |
| `coil-target-not-bool`            | OutCoil/OutSet/OutReset target isn't BOOL |
| `st-assignment-type-mismatch`     | ST Assignment target/value cross IEC §6.5 buckets |
| `st-condition-not-bool`           | IF / WHILE / REPEAT condition isn't BOOL |
| `st-for-index-not-numeric`        | FOR loop index variable isn't in the integer family (IEC §3.3.2.4) |
| `st-for-bound-not-numeric`        | FOR start / end / step bound isn't numeric |
| `sfc-contact-not-bool`            | SFC transition contact target isn't BOOL |
| `sfc-compare-type-mismatch`       | SFC transition compare operands cross IEC §6.5 buckets |
| `subrange-out-of-range`           | Literal value assigned to a SUBRANGE-typed target is outside `[lower, upper]` |
| `fbd-pin-type-mismatch`           | FBD block input pin's expected type doesn't match its producer's output type.  Resolves against user-defined POU calls + the bundled builtin signature database: IEC §2.5.2.3 stateful FBs (TON/TOF/TP, CTU/CTD/CTUD, SR/RS, R_TRIG/F_TRIG), §2.5.2.7 logical (AND/OR/XOR/NOT — EN/ENO only), §2.5.2.8 comparison (EQ/NE/LT/LE/GT/GE — OUT=BOOL), selection (SEL.G=BOOL, MUX/MAX/MIN/LIMIT polymorphic), §2.5.2.9 strings (LEN/FIND return INT; LEFT/RIGHT/MID/CONCAT/INSERT/DELETE/REPLACE polymorphic), and MOVE.  Polymorphic pins skip the check to avoid false positives |

Coverage: `tests/il/test_validation.py`, `tests/il/test_oop.py`,
`tests/il/test_st_ast.py`, `tests/il/test_fbd.py`,
`tests/il/test_var_access_config.py`, `tests/il/test_type_check.py`,
`tests/il/test_st_type_check.py`, `tests/il/test_sfc_type_check.py`,
`tests/il/test_subrange_range_check.py`,
`tests/il/test_fbd_pin_type_check.py`.

## What's NOT covered (and why)

1. ⚠️ **PLCopen reference-tool round-trip**.  XSD validity is
   necessary but not sufficient for cert; the practical signal
   is that an accredited IEC compiler accepts our output.
   ``tests/test_matiec_roundtrip.py`` drives matiec's
   ``iec2c`` against representative LD / SFC / FB / Compare /
   Move / BinaryMath / StdFunc programs as a subprocess.  CI
   doesn't require matiec to pass; the module skips cleanly
   when ``iec2c`` isn't on ``$PATH`` (or via ``MATIEC_BIN``).
   Beremiz and openplc_editor are GUI tools that want a full
   Beremiz-project layout (``plc.xml`` + ``beremiz.xml`` +
   confnode tree) rather than a single PLCopen XML, so they're
   probed by the ``verify-cert`` skill but not driven from CI;
   building a Beremiz-project-from-IL synthesiser would be a
   separate slice.
2. ✅ ~~**Semantic type checking**.~~ *Done* for the
   self-contained set: rung ops, ST AST bodies, SFC transition
   conditions, struct field / array element access through UDTs,
   FunctionCallExpr return-type inference (user-defined
   FUNCTIONs + IEC ``<SRC>_TO_<DST>`` / ``<SRC>_TRUNC_<DST>``
   conversions + a fixed-return-type table for ~30 §2.5.2
   builtins), polymorphic-builtin operand-aware inference
   (``ABS`` / ``MIN`` / ``MAX`` / ``SEL`` / ``LIMIT`` / ``MUX``),
   SUBRANGE literal-bounds checks (signed + unsigned + alias
   chains + struct/array members), and FBD pin connections
   against the referenced user-defined POU's interface.  31
   distinct error codes across the type system.  A bundled
   builtin FBD pin signature database covers the IEC §2.5.2.3
   stateful FBs (TON / TOF / TP, CTU / CTD / CTUD, SR / RS,
   R_TRIG / F_TRIG) and the comparison / logical families on
   their concrete pins; polymorphic pins (ADD / MUX / MOVE
   etc.) skip the check rather than guessing.  Remaining gaps
   that need new infrastructure (out of scope for V1
   type-checker work): value-flow analysis for non-literal
   SUBRANGE RHS, multi-level constant evaluation.
3. **Hardware-in-the-loop**.  Per `docs/ARCHITECTURE.md`: the
   ultimate verification posture is emulator-validated-by-hardware;
   the corpus here is the seed.

## CI surface

`um lint <file>` invokes the validator with text or JSON output for
CI integration (GitHub Annotations, jq pipelines, error counters).
Coverage: `tests/test_cli.py::test_lint_*`.

The full test suite is run by `pytest` from the repo root.  Current
status: **1205 / 1205 passing**.
