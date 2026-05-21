# IEC 61131-3 conformance status

This document tracks `universal_machinery`'s alignment with IEC
61131-3 Part 3 (Programmable Languages).  Each row maps an IEC
section to the IL construct that covers it (or notes the gap),
plus a coverage flag:

| Flag | Meaning |
| --- | --- |
| ✅ | Fully covered; round-trips through the IL faithfully |
| ⚠️ | Partial -- subset covered, gaps noted in the row |
| ❌ | Not modeled yet; see the [Roadmap](#roadmap) section |

The longer-term goal is to support **PLCopen IEC 61131-3 XML
conformance** at the Conversion level (and eventually the
Reusability level).  Full IEC 61508 functional safety / SIL
certification is intentionally out of scope; see
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md) for the project's
intended certification posture.

## Universal-compilation policy

The IL is designed as a **superset** of features found across PLC
vendors -- not the lowest common denominator (see
`design_principles.md` in the project memory).  Backends are
compilers: anything the IL expresses, they lower to their target's
runtime, synthesising whatever the target lacks natively.

For IEC conformance specifically this means: the IL covers the
IEC subset and more.  A *PLCopen-conformant* backend would emit
only IL constructs whose semantics are guaranteed to round-trip
through the PLCopen TC6 XML schema.  Vendor-specific extensions
(via `VendorOp`) are explicitly out-of-spec for that path.

---

## §2.2  Program Organization Units (POUs)

| Section | Construct | Status | IL representation | Notes |
| --- | --- | --- | --- | --- |
| §2.2 | PROGRAM | ✅ | `Subroutine(kind=PouKind.PROGRAM)` | |
| §2.2 | FUNCTION | ✅ | `Subroutine(kind=PouKind.FUNCTION, return_type=...)` | |
| §2.2 | FUNCTION_BLOCK | ✅ | `Subroutine(kind=PouKind.FUNCTION_BLOCK)` + instance `DataBlock` | |
| §2.5.1.5 | METHOD (3rd ed.) | ⚠️ | `Method` declared inside `Subroutine.methods` (FB) or `Interface.methods` | ST emitter renders full `METHOD ... END_METHOD` with PUBLIC/PRIVATE/PROTECTED/INTERNAL access specifiers, ABSTRACT, OVERRIDE.  PLCopen TC6 v2.01 XSD predates the 3rd edition and has no `<method>` element; PLCopen XML emission is incomplete for methods until a v2.02+ schema upgrade lands |
| §2.5.1.5 | INTERFACE (3rd ed.) | ⚠️ | `Interface` declared in `Program.interfaces`; FBs reference via `Subroutine.implements=[...]` | Multiple inheritance for interfaces supported; single inheritance for FBs (`Subroutine.extends`).  Same PLCopen XSD caveat as METHOD applies |
| §2.5.1.5 | EXTENDS (3rd ed.) | ⚠️ | `Subroutine.extends: Optional[str]` (single inheritance) | ST emits `FUNCTION_BLOCK Child EXTENDS Parent`; PLCopen XML omits per XSD limitation |
| §2.5.1.5 | IMPLEMENTS (3rd ed.) | ⚠️ | `Subroutine.implements: list[str]` | ST emits `FUNCTION_BLOCK Name IMPLEMENTS I1, I2`; PLCopen XML omits per XSD limitation |
| §2.5.1.5 | ABSTRACT (3rd ed.) | ⚠️ | `Subroutine.abstract: bool`, `Method.abstract: bool` | ST emits `FUNCTION_BLOCK ABSTRACT Name`; PLCopen XML omits per XSD limitation |
| (vendor) | SUBROUTINE | ✅ | `Subroutine(kind=PouKind.SUBROUTINE)` | Vendor-extension kind for CLICK-style unparameterized routines; outside IEC, but coexists |

## §2.4  Variables

| Section | Construct | Status | IL representation | Notes |
| --- | --- | --- | --- | --- |
| §2.4.1 | Direct representation (`%I`, `%Q`, `%M`, `%IX`, `%QX`, `%MX`, ...) | ✅ | `Address("%I0.0")` via smart builder coercion; PLCopen XML emits as the schema's optional ``address`` attribute on ``<variable>`` | All 5 size prefixes (X/B/W/D/L) × 3 location families (%I/%Q/%M) + hierarchical indices (`%I0.0.0`) recognised |
| §2.4.3 | VAR_INPUT | ✅ | `Var(direction=VarDirection.INPUT)` | |
| §2.4.3 | VAR_OUTPUT | ✅ | `Var(direction=VarDirection.OUTPUT)` | |
| §2.4.3 | VAR_IN_OUT | ✅ | `Var(direction=VarDirection.IN_OUT)` | |
| §2.4.3 | VAR (local) | ✅ | `Var(direction=VarDirection.LOCAL)` | |
| §2.4.3 | VAR_EXTERNAL | ✅ | `Var(direction=VarDirection.EXTERNAL)` | |
| §2.4.3 | VAR_TEMP | ✅ | `Var(direction=VarDirection.TEMP)` | |
| §2.4.3 | VAR_ACCESS | ✅ | `AccessVar(alias, instance_path, data_type, direction)` on `Configuration.access_vars` | Externally-visible aliases for HMI / OPC UA / fieldbus exposure.  ST emits `alias : instance_path : type direction;` per §2.7.1; PLCopen XML emits `<accessVariable alias= instancePathAndName= direction=>` with `direction` mapped to the XSD's `readOnly`/`readWrite` enum.  Validation: direction enum, alias uniqueness, instance-path syntax |
| §2.4.3 | VAR_GLOBAL | ⚠️ | `Tag` + (locked) `address` | Modeled as program-level Tag rather than IEC's VAR_GLOBAL block; semantics line up |
| §2.4.3 | VAR_CONFIG | ✅ | `ConfigVar(instance_path, data_type, initial_value)` on `Configuration.config_vars` | Pins per-instance parameter values at config-link time per §2.4.3.2.  ST emits `instance_path : type := initial_value;` inside `VAR_CONFIG ... END_VAR`; PLCopen XML emits `<configVars><configVariable .../></configVars>` with `<initialValue><simpleValue value=.../></initialValue>` body.  Validation: instance-path syntax, duplicate binding detection |

## §6.4  Data types

All 16 IEC elementary types are present in `TagType`:

| IEC type | TagType | Notes |
| --- | --- | --- |
| BOOL | `TagType.BOOL` | |
| BYTE | `TagType.BYTE` | |
| WORD | `TagType.WORD` | |
| DWORD | `TagType.DWORD` | |
| SINT | `TagType.SINT` | |
| INT | `TagType.INT` | |
| DINT | `TagType.DINT` | |
| LINT | `TagType.LINT` | |
| USINT | `TagType.USINT` | |
| UINT | `TagType.UINT` | |
| UDINT | `TagType.UDINT` | |
| ULINT | `TagType.ULINT` | |
| REAL | `TagType.REAL` | |
| LREAL | `TagType.LREAL` | |
| TIME | `TagType.TIME` | |
| STRING | `TagType.STRING` | |

User-defined types are now first-class: `StructType`, `ArrayType`,
`EnumType`, `SubrangeType`, and `AliasType` live in
[`universal_machinery/il/types.py`](../universal_machinery/il/types.py).
`Program.user_types` is the declaration table; both the ST emitter
(emits `TYPE ... END_TYPE` blocks) and the PLCopen XML emitter
(emits `<dataTypes><dataType><baseType>...` per the TC6 schema)
support every variant, XSD-validated.  Subrange types emit as
`<subrangeSigned>` or `<subrangeUnsigned>` driven by the
signed/unsigned classification of the underlying integer base.

## §2.5.2  Standard library functions

| Section | Family | Status | IL representation | Notes |
| --- | --- | --- | --- | --- |
| §2.5.2.1 | Type conversion (BOOL_TO_INT, INT_TO_REAL, ...) | ✅ | `StdFunc(name="INT_TO_REAL", ...)` or `convert("INT", "REAL", src, dst)` | All ``<SRC>_TO_<DST>`` pairs (21 IEC elementary types × 20 distinct destinations = 420 conversions) generated programmatically.  BCD family (``BCD_TO_INT``, ``INT_TO_BCD``, ...) and TRUNC family (generic ``TRUNC`` + 16 typed ``REAL_TRUNC_*`` / ``LREAL_TRUNC_*`` variants) included.  ``is_iec_std_function(name)`` predicate for backend / validation gatekeeping |
| §2.5.2.4 | Numerical (ABS, SQRT, LN, LOG, EXP, SIN/COS/TAN, ASIN/ACOS/ATAN) | ✅ | `StdFunc(name="ABS", ...)` | All standard names registered; backend support varies |
| §2.5.2.5 | Arithmetic (ADD, SUB, MUL, DIV, MOD) | ✅ | `BinaryMath(op="+", ...)` | Modeled as dedicated op since `dst = lhs OP rhs` is universal |
| §2.5.2.6 | Bit-string (SHL, SHR, ROR, ROL) | ✅ | `StdFunc(name="SHL", ...)` | |
| §2.5.2.7 | Logical / bitwise (AND, OR, XOR, NOT) | ✅ | `StdFunc(name="AND", ...)` | Applies to BOOL or bit-string per IEC |
| §2.5.2.8 | Selection (SEL, MAX, MIN, LIMIT, MUX) | ✅ | `StdFunc(name="SEL", ...)` | |
| §2.5.2.8 | Comparison (GT, GE, EQ, LE, LT, NE) | ✅ | `Compare(op=">", ...)` | Dedicated op (returns a boolean for rung gating) |
| §2.5.2.9 | Character-string (LEN, LEFT, RIGHT, MID, CONCAT, INSERT, DELETE, REPLACE, FIND) | ✅ | `StdFunc(name="LEN", ...)` | All 9 names from IEC §2.5.2.9 table 28 registered.  Backend STRING runtime support varies by target -- that's a vendor-capability axis tracked separately |
| §2.5.2.10 | Time / date (ADD_TIME / SUB_TIME / ADD_DT_TIME / ..., DT_TO_DATE / DT_TO_TOD / ..., CONCAT_DATE_TOD) | ✅ | `StdFunc(name="ADD_DT_TIME", ...)` | Full IEC §2.5.2.10 table 30: TIME arithmetic (ADD/SUB/MUL/DIV_TIME, MULTIME/DIVTIME), TOD/DT + TIME composition (ADD/SUB_TOD_TIME, ADD/SUB_DT_TIME), same-type subtractions yielding TIME (SUB_DATE_DATE, SUB_TOD_TOD, SUB_DT_DT), composition (CONCAT_DATE_TOD), extraction (DT_TO_DATE, DT_TO_TOD, DT_TO_TIME) |

## §2.5.3  Standard function blocks (stateful)

| Section | FB | Status | IL representation |
| --- | --- | --- | --- |
| §2.5.2.3.3 | R_TRIG | ✅ | `RTrig(state, clk, q)` |
| §2.5.2.3.3 | F_TRIG | ✅ | `FTrig(state, clk, q)` |
| §2.5.2.3.3 | SR (set-dominant) | ✅ | `SR(q1, s1, r)` |
| §2.5.2.3.3 | RS (reset-dominant) | ✅ | `RS(q1, r1, s)` |
| §2.5.2.3.1 | TON, TOF, TP | ✅ | `TON`, `TOF`, `TP` (dedicated ops) |
| §2.5.2.3.2 | CTU, CTD, CTUD | ✅ | `CTU`, `CTD`, `CTUD` |
| §2.5.2.3.4 | Communication FBs | ❌ | -- | Out of scope until fieldbus modeling lands |

## §4  Languages

| Language | Status | Notes |
| --- | --- | --- |
| LD (Ladder Diagram) | ✅ | Modeled via `Rung` + LD-flavoured ops (Contact/Coil/Compare/etc.).  PLCopen XML emits native `<body><LD>` for pure-LD rungs (contacts + coils): one `<leftPowerRail>` → contact(s) → coil → `<rightPowerRail>` chain per rung, wired sink-side; rungs containing math / call / stdlib / parallel-group ops fall back to ST translation pending the mixed LD+FBD slice.  Reader recovers the same shape into `Subroutine.rungs` |
| SFC (Sequential Function Chart) | ✅ | `SfcNetwork`, `Step`, `Transition`, `Action` -- see `il/sfc.py`.  PLCopen XML emits native `<SFC><step localId= name= initialStep=>` + `<transition>` with sink-side connection graph reconstructing `from_steps` / `to_steps`; conditions embed inline ST via `<condition><inline name="cond"><ST><xhtml:pre>...`.  Reader picks up the same shape (including PLCopen `<reference>` and `<inline>` condition forms) and lowers AND / NOT / OR / paren chains over bare variable refs into structured LD ops (`ContactNO` / `ContactNC` / `ParallelGroup`) via the ST expression parser, so round-trip is AST-equal for the common condition shapes.  Action blocks + `<selectionDivergence>` / `<simultaneousDivergence>` deferred to a follow-up |
| ST (Structured Text) | ✅ | First-class AST in [`il/st.py`](../universal_machinery/il/st.py): expressions (Literal, VarRef, FieldAccess, IndexAccess, UnaryExpr, BinaryExpr, FunctionCallExpr), statements (Assignment, IF/CASE/FOR/WHILE/REPEAT, RETURN/EXIT/CONTINUE, function-call statement).  `Subroutine.st_body` / `Method.st_body` carry ST programs; ST emitter renders the AST directly with IEC §3.3.1 operator precedence and parenthesisation |
| IL (Instruction List, aka STL) | ❌ | Deprecated in IEC 3rd ed. but still common in older systems |
| FBD (Function Block Diagram) | ✅ | First-class AST in [`il/fbd.py`](../universal_machinery/il/fbd.py): ``FbdNetwork`` containing ``FbBlock`` (function/FB call sites), ``InVariable``/``OutVariable``/``InOutVariable`` (variable connectors), ``FbdJump``/``FbdLabel``/``FbdReturn`` (control flow).  Wires stored sink-side as ``Connection(source_id, source_pin)`` matching PLCopen's connection model.  ``Subroutine.fbd_body`` / ``Method.fbd_body`` carry FBD bodies; PLCopen XML emits ``<FBD>`` with auto-layout positions, XSD-validated.  ST emission lowers via [`lowering/fbd_to_st.py`](../universal_machinery/lowering/fbd_to_st.py): topological sort + producer-expression resolution; stateless 2-input blocks (``ADD``/``MUL``/``AND``/``GT``/...) inline as ``BinaryExpr``, FB calls emit ``Inst(IN := src);`` + ``Inst.OUT`` dot-access, other functions route through temp vars.  ``FbdJump``/``FbdLabel``/``FbdReturn`` lower to real IEC §3.3.2.5 ``GotoStatement``/``LabelStatement``/``ReturnStatement`` |

## §2.7  Configuration / Resource / Task

| Section | Construct | Status | Notes |
| --- | --- | --- | --- |
| §2.7.1 | CONFIGURATION | ✅ | ``Configuration`` in [`il/configuration.py`](../universal_machinery/il/configuration.py); ST emits ``CONFIGURATION ... END_CONFIGURATION``; PLCopen XML emits ``<instances><configurations><configuration>`` with XSD validation |
| §2.7.1 | RESOURCE | ✅ | ``Resource`` -- one PLC CPU; multi-PLC = multi-resource within one Configuration |
| §2.7.2 | TASK | ✅ | ``TaskSpec`` with cyclic/single-shot/interrupt triggering + priority; PLCopen XML nests bound POU instances under their task element per the schema |
| §2.7.1 | VAR_ACCESS / accessVariable | ✅ | ``Configuration.access_vars: list[AccessVar]``; XML emits ``<accessVariable alias="..." instancePathAndName="..." direction="readOnly|readWrite">`` per the TC6 schema |
| §2.7.1 | VAR_GLOBAL (config-scope) | ✅ | ``Configuration.global_vars`` -- system-wide globals |
| §2.7.1 | VAR_GLOBAL (resource-scope) | ✅ | ``Resource.global_vars`` -- per-CPU globals |
| §2.7.1 | configVars | ✅ | ``Configuration.config_vars: list[ConfigVar]``; XML emits ``<configVars><configVariable instancePathAndName="..."><type>.../<initialValue><simpleValue value=.../></initialValue></configVariable></configVars>`` per the TC6 ``varListConfig`` type |

## §3  Common elements (extension hatches)

| Construct | IL representation | Notes |
| --- | --- | --- |
| Vendor-specific instructions (CLICK DRUM, Siemens SCL_S_LOOP, AB PIDE) | `VendorOp(vendor, name, ...)` | Preserves vendor instruction identity for round-trip; not certifiable -- a conformance-mode backend rejects `VendorOp` for vendors other than its own |
| Vendor-specific calling conventions | `Call` + IL primitives, lowered per backend | The CLICK scheduler ([`click_calling_convention.md`](click_calling_convention.md)) shows the model |

---

## Roadmap

Concrete slices to close the larger conformance gaps, in priority order:

1. **PLCopen TC6 XML emitter + reader**.  ✅ *Validated against the
   official PLCopen TC6 v2.01 XSD.*
   ``universal_machinery.emitters.plcopen_xml`` emits TC6 v2.01 XML
   with POU declarations, variable interfaces, return types, ST /
   FBD bodies, configurations (resources, tasks, pouInstances,
   globalVars, accessVars, configVars), and Tag declarations
   exported as a synthetic ``GlobalsHolder`` POU.

   ``universal_machinery.parsers.plcopen_xml`` reads those documents
   back into IL ``Program``s, closing the round-trip loop and
   unlocking the cross-vendor migration use case (import a program
   authored in matiec / Beremiz / OpenPLC editor → modify in IL →
   re-emit to another vendor).  Covers POU interfaces +
   the full Configuration model including accessVars / configVars
   / per-task pouInstance binding; plus user-defined types
   (``<dataTypes>`` block) for all five IEC §2.3.3 variants
   (STRUCT, ARRAY, ENUM, SUBRANGE signed/unsigned, ALIAS),
   resolved on variable interfaces via ``<derived name=>`` →
   ``NamedType``.  ST bodies are parsed back into structured AST
   via
   [`parsers.st_text`](../universal_machinery/parsers/st_text.py)
   (hand-rolled recursive-descent + Pratt expression parser per
   IEC §3.3.1 precedence): assignments, IF/ELSIF/ELSE, CASE with
   multi-label clauses + ELSE, FOR/BY, WHILE, REPEAT/UNTIL,
   RETURN/EXIT/CONTINUE, GOTO + labels, function-call statements,
   and the full expression grammar including field/index access
   and named-arg calls.  FBD bodies round-trip too: ``<block>``,
   ``<inVariable>`` / ``<outVariable>`` / ``<inOutVariable>``,
   ``<jump>`` / ``<label>`` / ``<return>`` plus all pin modifiers
   (negated / edge / storage), positions, and execution-order
   ids all survive the read pass.  Parse failures degrade to a
   single ``CommentStatement`` so a partial import stays usable.

   ``validate_plcopen_xml(xml)`` validates emitted output against
   the bundled XSD (sourced from Beremiz's public mirror).
   Schema-level conformance verified for: empty programs, single
   POUs, FUNCTION with return type, FUNCTION_BLOCK with VAR_IN_OUT
   + locals + initial values, programs with multi-op rungs
   (contacts / coils / set/reset / parallel / compare / math /
   call / stdlib / ret), FBD bodies, accessVars + configVars,
   and globals-tag export.

   Next:
     - Mixed LD+FBD bodies (rungs containing math / call /
       stdlib / parallel-group ops currently still fall back to
       ST translation; routing those through ``<block>`` per the
       fbdObjects group makes the LD round-trip lossless for
       every Rung shape).
     - SFC action blocks + selection/simultaneous divergence
       elements (current SFC slice covers steps + transitions
       only).
     - Round-trip against PLCopen reference tools -- XSD validity
       is necessary but not sufficient for full cert.

2. ✅ ~~**ST AST**.~~ *Done.*  First-class ST body in
   [`il/st.py`](../universal_machinery/il/st.py).  ``Subroutine``
   and ``Method`` gain a ``st_body: Optional[list[Statement]]``
   field alongside ``rungs`` and ``sfc`` -- the three are mutually
   exclusive, enforced by the validator.  The AST covers IEC §3
   expressions (literal, variable, field/index access, unary/
   binary operators with §3.3.1 precedence, function-call as
   expression) and statements (assignment, IF/ELSIF/ELSE,
   CASE/ELSE with multi-label clauses, FOR/BY, WHILE, REPEAT,
   RETURN, EXIT, CONTINUE, function-call as statement).
   Builder DSL helpers (``assign``, ``if_``, ``case_``,
   ``while_``, ``repeat_``, ``for_``, ``add_e``/``sub_e``/...,
   ``and_e``/``or_e``/...) and JSON round-trip are complete.
   The ST emitter renders the AST directly when ``st_body`` is
   set; otherwise it falls back to rung-to-ST translation.
   **PLCopen XML emission**: authored ``st_body`` is rendered
   verbatim inside ``<body><ST><xhtml:pre>...`` -- the XML
   emitter picks ``st_body`` over ``rungs`` whenever set.
   XSD-validated against the bundled TC6 v2.01 schema.

3. ✅ ~~**Direct representation parser**.~~ *Done.*  IEC §2.4.1.1
   direct-representation addresses (``%I0.0``, ``%QB5``, ``%MW10``,
   ``%MX5``, hierarchical ``%I0.0.0``) are recognised by the smart
   builder coercion and emit as the PLCopen schema's optional
   ``address`` attribute on ``<variable>``.  CLICK-style vendor
   addresses (``X001``, ``DS9000``) continue to emit as AT-comment
   annotations.

4. ⚠️ ~~**METHOD / INTERFACE**.~~ *Partial.*  IEC 3rd-edition OOP
   (`il/oop.py`): `Method`, `Interface`, plus `Subroutine.methods` /
   `extends` / `implements` / `abstract` fields.  Builder DSL
   (`method`, `abstract_method`, `interface`), ST emission, JSON
   serialisation, and validation are complete.  PLCopen TC6 v2.01
   XSD predates the 3rd edition and has no `<method>` / `<interface>`
   elements -- XML emission for OOP programs is incomplete until a
   v2.02+ schema upgrade lands.  ST is the conformant-output path
   for OOP IL today.

5. ✅ ~~**CONFIGURATION / RESOURCE / TASK**.~~ *Done.*  IEC §2.7 system-
   organisation model lives in [`il/configuration.py`](../universal_machinery/il/configuration.py).
   ST emits ``CONFIGURATION ... END_CONFIGURATION``; PLCopen XML
   emits ``<instances><configurations>`` with task-bound POU
   instances nested under ``<task>`` per the schema.  XSD-validated.
   Pairs with the multi-PLC Project container documented in
   [`ARCHITECTURE.md`](ARCHITECTURE.md) -- a multi-PLC project is
   one Configuration with multiple Resources.

6. ✅ ~~**User-defined types**.  STRUCT, ARRAY, ENUM, subrange.~~
   *Done.*  All five variants (STRUCT, ARRAY, ENUM, SUBRANGE, ALIAS)
   landed in `il/types.py` with full ST + PLCopen XML emission,
   XSD-validated against the official TC6 v2.01 schema.

7. ✅ ~~**FBD topology + FBD→ST lowering**.~~ *Done.*
   ``il/fbd.py`` defines ``FbdNetwork`` and seven element kinds
   (``FbBlock``, ``InVariable``, ``OutVariable``,
   ``InOutVariable``, ``FbdLabel``, ``FbdJump``, ``FbdReturn``)
   with sink-side ``Connection(source_id, source_pin)`` wires
   matching the PLCopen XSD's connection model.
   ``Subroutine.fbd_body`` and ``Method.fbd_body`` carry FBD
   bodies (mutex with ``rungs`` / ``sfc`` / ``st_body``); builder
   DSL, JSON round-trip, validation (``local_id`` uniqueness,
   connection resolution, known source pins, known jump labels),
   and PLCopen XML emission (with auto-layout positions) all
   land.  ``lowering/fbd_to_st.py`` translates an ``FbdNetwork``
   into an equivalent list of ST ``Statement``s (topological
   sort + producer-expression resolution + temp-var allocation):
   stateless 2-input blocks lower inline as ``BinaryExpr``, FB
   calls emit IEC named-arg syntax with dot-access for outputs,
   other functions go through synthesised temp vars.  Lets any
   backend that speaks ST execute FBD-authored programs even
   without native FBD support.  XSD-validated against bundled
   TC6 v2.01 schema.

8. **Full standard library coverage**.  The ~100 IEC §2.5.2
   functions; mechanical to add, but bulk work.  Each new function
   joins `STD_FUNCTION_NAMES` and (optionally) gets a builder DSL
   convenience helper.

---

## Verification posture

The IL is structurally aligned with IEC where it is covered, but
**certification means more than structural alignment** -- it means
producing artifacts (PLCopen XML files) that pass an accredited
test suite and demonstrating that the toolchain's behaviour matches
the spec.  See [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) on the
testing posture: emulator + hardware-in-the-loop is the right
verification path for any conformance claim.

A future commit will add a `docs/CONFORMANCE_TEST_PLAN.md` that
maps each conformance row above to a concrete test fixture +
expected output -- the seed of a public PLCopen conformance corpus.
