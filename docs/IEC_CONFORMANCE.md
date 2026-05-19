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
| §2.5.1.5 | METHOD (3rd ed.) | ❌ | -- | Required for full 3rd-edition conformance |
| §2.5.1.5 | INTERFACE (3rd ed.) | ❌ | -- | Pairs with METHOD |
| (vendor) | SUBROUTINE | ✅ | `Subroutine(kind=PouKind.SUBROUTINE)` | Vendor-extension kind for CLICK-style unparameterized routines; outside IEC, but coexists |

## §2.4  Variables

| Section | Construct | Status | IL representation | Notes |
| --- | --- | --- | --- | --- |
| §2.4.1 | Direct representation (`%I`, `%Q`, `%M`, `%IX`, `%QX`, `%MX`, ...) | ❌ | -- | Currently use vendor-style addresses (`X001`, `DS9000`); a parallel IEC parser is a follow-up |
| §2.4.3 | VAR_INPUT | ✅ | `Var(direction=VarDirection.INPUT)` | |
| §2.4.3 | VAR_OUTPUT | ✅ | `Var(direction=VarDirection.OUTPUT)` | |
| §2.4.3 | VAR_IN_OUT | ✅ | `Var(direction=VarDirection.IN_OUT)` | |
| §2.4.3 | VAR (local) | ✅ | `Var(direction=VarDirection.LOCAL)` | |
| §2.4.3 | VAR_EXTERNAL | ✅ | `Var(direction=VarDirection.EXTERNAL)` | |
| §2.4.3 | VAR_TEMP | ✅ | `Var(direction=VarDirection.TEMP)` | |
| §2.4.3 | VAR_ACCESS | ❌ | -- | Resource-level access; pairs with §2.7 |
| §2.4.3 | VAR_GLOBAL | ⚠️ | `Tag` + (locked) `address` | Modeled as program-level Tag rather than IEC's VAR_GLOBAL block; semantics line up |
| §2.4.3 | VAR_CONFIG | ❌ | -- | Configuration-level vars; pairs with §2.7 |

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

User-defined types (STRUCT, ARRAY, ENUM, subrange): ❌ not yet
modeled.  Single-level structs are partially expressible via
`DataBlock` (typed members) but the IEC `TYPE ... END_TYPE`
declaration form is missing.

## §2.5.2  Standard library functions

| Section | Family | Status | IL representation | Notes |
| --- | --- | --- | --- | --- |
| §2.5.2.1 | Type conversion (BOOL_TO_INT, INT_TO_REAL, ...) | ⚠️ | `StdFunc(name="INT_TO_REAL", ...)` | Common conversions registered in `STD_FUNCTION_NAMES`; full IEC set is 40+ pairs |
| §2.5.2.4 | Numerical (ABS, SQRT, LN, LOG, EXP, SIN/COS/TAN, ASIN/ACOS/ATAN) | ✅ | `StdFunc(name="ABS", ...)` | All standard names registered; backend support varies |
| §2.5.2.5 | Arithmetic (ADD, SUB, MUL, DIV, MOD) | ✅ | `BinaryMath(op="+", ...)` | Modeled as dedicated op since `dst = lhs OP rhs` is universal |
| §2.5.2.6 | Bit-string (SHL, SHR, ROR, ROL) | ✅ | `StdFunc(name="SHL", ...)` | |
| §2.5.2.7 | Logical / bitwise (AND, OR, XOR, NOT) | ✅ | `StdFunc(name="AND", ...)` | Applies to BOOL or bit-string per IEC |
| §2.5.2.8 | Selection (SEL, MAX, MIN, LIMIT, MUX) | ✅ | `StdFunc(name="SEL", ...)` | |
| §2.5.2.8 | Comparison (GT, GE, EQ, LE, LT, NE) | ✅ | `Compare(op=">", ...)` | Dedicated op (returns a boolean for rung gating) |
| §2.5.2.9 | Character-string (LEN, LEFT, RIGHT, MID, CONCAT, INSERT, DELETE, REPLACE, FIND) | ⚠️ | `StdFunc(name="LEN", ...)` | Names registered; backend STRING support depends on target |
| §2.5.2.10 | Time / date (ADD_DT_TIME, SUB_DT_TIME, ...) | ⚠️ | `StdFunc(name="ADD_DT_TIME", ...)` | Common ones registered |

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
| LD (Ladder Diagram) | ✅ | Modeled via `Rung` + LD-flavoured ops (Contact/Coil/Compare/etc.) |
| SFC (Sequential Function Chart) | ✅ | `SfcNetwork`, `Step`, `Transition`, `Action` -- see `il/sfc.py` |
| ST (Structured Text) | ❌ | No AST yet.  Roadmap item; needs IEC-style statement modeling (assignment, IF/CASE/WHILE/FOR, function call, ...) |
| IL (Instruction List, aka STL) | ❌ | Deprecated in IEC 3rd ed. but still common in older systems |
| FBD (Function Block Diagram) | ❌ | Could lower to LD; explicit FBD topology missing |

## §2.7  Configuration / Resource / Task

| Section | Construct | Status | Notes |
| --- | --- | --- | --- |
| §2.7.1 | CONFIGURATION | ❌ | Pairs with the future multi-PLC Project container ([`docs/ARCHITECTURE.md`](ARCHITECTURE.md) Open Questions) |
| §2.7.1 | RESOURCE | ❌ | |
| §2.7.2 | TASK | ❌ | |

## §3  Common elements (extension hatches)

| Construct | IL representation | Notes |
| --- | --- | --- |
| Vendor-specific instructions (CLICK DRUM, Siemens SCL_S_LOOP, AB PIDE) | `VendorOp(vendor, name, ...)` | Preserves vendor instruction identity for round-trip; not certifiable -- a conformance-mode backend rejects `VendorOp` for vendors other than its own |
| Vendor-specific calling conventions | `Call` + IL primitives, lowered per backend | The CLICK scheduler ([`click_calling_convention.md`](click_calling_convention.md)) shows the model |

---

## Roadmap

Concrete slices to close the larger conformance gaps, in priority order:

1. **PLCopen TC6 XML emitter**.  ✅ *Validated against the
   official PLCopen TC6 v2.01 XSD.*
   ``universal_machinery.emitters.plcopen_xml`` emits TC6 v2.01 XML
   with POU declarations, variable interfaces, return types, and ST
   bodies (built on the ST emitter).  Tags are exported as a
   synthetic ``GlobalsHolder`` POU's ``<localVars>`` until
   ``<configurations><globalVars>`` lands.

   ``validate_plcopen_xml(xml)`` validates emitted output against
   the bundled XSD (sourced from Beremiz's public mirror).
   Schema-level conformance verified for: empty programs, single
   POUs, FUNCTION with return type, FUNCTION_BLOCK with VAR_IN_OUT
   + locals + initial values, programs with multi-op rungs
   (contacts / coils / set/reset / parallel / compare / math /
   call / stdlib / ret), and globals-tag export.

   Next: round-trip against PLCopen reference tools (matiec,
   Beremiz, OpenPLC editor) -- XSD validity is necessary but not
   sufficient for full cert.

2. **ST AST**.  Add an alternative body type to `Subroutine` (next to
   `rungs` and `sfc`): an ordered list of ST statements.  Needed for
   any IEC FUNCTION/FB that's authored in ST rather than LD.
   *Note*: the ST emitter (``universal_machinery.emitters.st``)
   already translates rung bodies to ST text; a follow-up makes ST
   first-class so users can author in ST directly.

3. **Direct representation parser**.  Accept `%I0.0`, `%Q0.0`, `%MX5`,
   etc. as input to `loc(...)` alongside vendor-style addresses.
   Backends translate between the two as needed.

4. **METHOD / INTERFACE**.  IEC 3rd-edition OOP additions on FBs.
   Add `Subroutine.methods: list[Subroutine]` and an `Interface`
   declaration.  Required for 3rd-edition conformance.

5. **CONFIGURATION / RESOURCE / TASK**.  Top-level project model
   for multi-PLC, multi-task systems.  Aligns with the multi-PLC
   Project container we documented in
   [`ARCHITECTURE.md`](ARCHITECTURE.md).

6. **User-defined types**.  STRUCT, ARRAY, ENUM, subrange.  TYPE...
   END_TYPE declaration form.  Required for non-trivial IEC programs.

7. **FBD topology**.  Explicit FBD body type with named connections
   between FB instances.  Lowerable to LD where the backend lacks
   native FBD.

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
