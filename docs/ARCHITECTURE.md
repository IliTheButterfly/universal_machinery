# Architecture

This document explains how `universal_machinery` is structured and how to extend it.

## Layers

```
                       Application code / notebooks
                                  │
                                  ▼
                    universal_machinery.il  (AST)
                                  │
                  ┌───────────────┼───────────────┐
                  ▼               ▼               ▼
          click_plc        openplc_backend     <future>
          (CKP file)      (PLCopen XML, ST)    (S7, RSLogix, ...)
```

Layer 1 is a Python library: `universal_machinery.il` defines a
vendor-neutral AST per IEC 61131-3 Part 3.

Layer 2 is the backend registry: each vendor backend lives in its own
git repo (submoduled under `backends/<vendor>/`), implements the
`Backend` ABC, and registers itself by short name (e.g. `"click"`,
`"openplc"`).

Layer 3 is what you run: `program.read("foo.ckp")` returns an
`il.Program`; `backend.write(program, "out.xml")` lowers it.

## The IL

### Why IEC 61131-3?

Every commercial PLC supports a subset of IEC 61131-3 Part 3 (LD / IL /
ST / FBD / SFC).  Modeling the IL on it means:

  - We can borrow the standard's data types (BOOL, INT, REAL, TIME, ...)
    and op semantics (TON timer, CTU counter, comparison ops) without
    inventing new conventions.
  - Backends mostly map 1:1 onto vendor-specific names (CLICK's
    `ContactNO` → IEC's `LD` / `Contact`; CLICK's `Copy` → IEC's `MOVE`;
    OpenPLC speaks the standard natively).

### The ``Program`` AST

```
Program
├── subroutines: list[Subroutine]              (POUs)
│   ├── name: str
│   ├── main: bool             (entry-point flag)
│   ├── kind: PouKind          (PROGRAM | FUNCTION | FUNCTION_BLOCK | SUBROUTINE)
│   ├── inputs / outputs / in_outs / local_vars: list[Var]
│   ├── return_type: TagType?  (for FUNCTION)
│   ├── rungs: list[Rung]      (LD/IL body) ── OR ──
│   └── sfc:   SfcNetwork?     (grafcet body)
│        ├── steps:       list[Step] (+ actions)
│        └── transitions: list[Transition]
├── data_blocks: list[DataBlock]               (typed memory aggregates)
│   └── members: list[Var]     (+ fb_template for instance DBs)
├── tags: dict[Address → Tag]   (symbol table)
└── metadata: cpu_model, project_name, comment
```

A ``Rung``'s ``ops`` list is read left-to-right.  Contacts in series
form an AND; ``ParallelGroup`` represents an OR (multiple branches in
LD).  The right-most op is the rung's output (typically a coil, a
function-block call, or a flow-control op).

### POU kinds, DataBlocks, and SFC

POUs follow IEC 61131-3 §2.2:

  - **PROGRAM**         top-level executable with parameters but no return
  - **FUNCTION**        stateless; one return value (`return_type`)
  - **FUNCTION_BLOCK**  stateful; state lives in an instance DataBlock
  - **SUBROUTINE**      vendor-native unparameterized routine (CLICK)

A **DataBlock** is a named, typed collection of memory locations
(Siemens-style global DB or instance DB).  When `fb_template` is set,
it is the per-instance state container for a FUNCTION_BLOCK; the call
site supplies the DB to `Call.instance`.

**SFC** (grafcet) is an alternative POU body composed of `Step`s with
`Action`s, joined by guarded `Transition`s -- see
[`il/sfc.py`](https://github.com/IliTheButterfly/universal_machinery/blob/main/universal_machinery/il/sfc.py).

For how all of this lowers onto a vendor target that has neither
parameters nor nested calls, see
[click_calling_convention.md](click_calling_convention.md).

### Op categories

See [`universal_machinery/il/ops.py`](https://github.com/IliTheButterfly/universal_machinery/blob/main/universal_machinery/il/ops.py)
for the canonical list.  Categories:

  - **Bit input**: `ContactNO`, `ContactNC`, `ContactRisingEdge`,
    `ContactFallingEdge`
  - **Bit output**: `OutCoil`, `OutSet`, `OutReset`
  - **Timers**: `TON` (on-delay), `TOF` (off-delay), `TP` (pulse)
  - **Counters**: `CTU`, `CTD`, `CTUD`
  - **Compare**: `Compare(op, lhs, rhs)` for ==, !=, <, <=, >, >=
  - **Math**: `Move`, `BinaryMath` for +, -, *, /, %
  - **Control flow**: `Call` (parameterized), `Return`, `End`, `Jump`, `Label`
  - **Topology**: `ParallelGroup`
  - **Vendor extension**: `VendorOp` -- escape hatch for
    vendor-specific instructions preserved through the IL verbatim

Every op is a frozen dataclass — the AST is hashable, comparable, and
trivially serialisable (via `dataclasses.asdict`).

### Universal-compilation philosophy

The IL is *not* a lowest-common-denominator of what every PLC
supports.  It is the **union** of features found in real PLCs, and
each backend is a compiler that lowers the IL onto its target's
runtime -- synthesising whatever the target doesn't support natively.

Example: CLICK has neither parameterized subroutines nor nested
calls.  The CLICK backend implements both via a cooperative
scheduler + reserved-region calling convention (see
[`click_calling_convention.md`](click_calling_convention.md)).  The
emitted ladder is ugly; the user never sees it.  This is the LLVM
model applied to PLCs.

Therefore:

  - **Anything any PLC supports, the IL supports.**  When a feature
    is added for one vendor, every backend gains a lowering pass for
    it (possibly an expensive one).
  - **Capabilities are a cost model, not a feasibility model.**
    Instead of "this op is unsupported," a backend declares "this op
    costs N bytes of memory and adds M scans of latency on this
    target."  Users pick features knowing the trade-offs.
  - **The diagnostic shifts from "we couldn't emit this" to "this
    will burn X bytes / Y scans on your PLC."**  Refusal is
    reserved for things genuinely impossible on the target (e.g.
    floating-point math on a PLC that lacks the memory budget).

### Vendor extensions via `VendorOp`

`VendorOp` is the open-extension hatch for vendor-specific
instructions whose *identity* should be preserved (a CLICK `DRUM`,
a Siemens `SCL_S_LOOP`, an Allen-Bradley `PIDE`).  It is not a
mechanism for "the IL can't express this"; the IL targets
universal compilation.  It is specifically for:

  - Round-trip preservation (decoded vendor instructions survive
    re-emit unchanged)
  - Performance hand-optimisation (use a vendor's native FB
    instead of synthesising the equivalent)
  - Hardware-tied operations (motion-control, drive parameters)

Backends raise `UnsupportedOpError` for a `VendorOp` whose
`vendor` doesn't match -- never silently drop, never substitute.

## Backends

A backend is a Python package that:

  1. Subclasses `universal_machinery.backends.Backend`
  2. Implements `read(path) -> Program` and `write(program, path)`
  3. Declares its `capabilities` (which IL ops it can faithfully emit)
  4. Registers itself via `@register("name")`

Backends live in separate git repos so they can be developed and
versioned independently — and so a downstream user only needs to clone
the backends they actually use.  In this monorepo they're git
submodules under `backends/<vendor>/`.

### Capability negotiation

Every IL op should be lowerable to every backend given enough memory
(see "Universal-compilation philosophy" above).  Capabilities exist
to declare **how** -- natively or via synthesis -- and at what cost.

The standard capability strings on `Backend.capabilities` (see
[`base.py`](https://github.com/IliTheButterfly/universal_machinery/blob/main/universal_machinery/backends/base.py)) cover op
categories; per-op cost models are TBD as a future addition.

When a backend genuinely cannot lower an op (e.g. a `VendorOp` from a
different vendor, or a feature the target has insufficient memory to
synthesise), it raises ``UnsupportedOpError`` -- not silent drop.

### Round-trip guarantee

A backend's `read` + `write` should produce a file the vendor tool
accepts.  For unedited input it should be byte-identical (the CLICK
backend achieves this).

Cross-backend round-trips (read CLICK → IL → write OpenPLC) are
*semantically* equivalent: the same logical program lowered to a
different target.  Vendor-specific `VendorOp`s do not survive
cross-vendor round-trips (they raise `UnsupportedOpError`); a user
who needs cross-vendor portability avoids `VendorOp` and uses IL
primitives whose lowering is universal.

## Adding a backend

1. Create a new directory under `backends/<vendor>/`, init a git repo,
   add it as a submodule.

2. Author the package:
   ```
   backends/<vendor>/
   ├── pyproject.toml
   ├── README.md
   └── <vendor>_backend/
       ├── __init__.py
       └── backend.py        # the Backend subclass
   ```

3. Implement:
   ```python
   from universal_machinery.backends import Backend, register
   from universal_machinery.il import Program

   @register("vendor")
   class VendorBackend(Backend):
       capabilities = frozenset({"ld", "call", "compare"})

       def read(self, path: str) -> Program: ...
       def write(self, program: Program, path: str) -> None: ...
   ```

4. Tests in `tests/<vendor>/`.

## Open questions

- **Visual-fidelity round-tripping**: a CLICK rung is laid out on a
  fixed-width grid (3 cells wide, 32 cells tall).  The IL doesn't model
  pixel positions; backends synthesise reasonable layouts.  When we
  read CLICK → IL → CLICK we may not preserve the original grid
  positions of cells across rungs that have been edited.

- ~~**Function blocks vs. subroutines**: IEC distinguishes PROGRAM /
  FUNCTION / FUNCTION_BLOCK.  We currently collapse all into
  `Subroutine`.  When we add a backend that needs the distinction,
  promote.~~ *Resolved:* `Subroutine.kind` discriminates among the four
  POU kinds (PROGRAM / FUNCTION / FUNCTION_BLOCK / SUBROUTINE).  The
  CLICK lowering of FUNCTION / FUNCTION_BLOCK / DataBlock / nested
  calls is specified in
  [click_calling_convention.md](click_calling_convention.md).

- **Comments**: CLICK stores rung comments separately from the SC-SCR
  section; we don't yet read them.  Add a "comments" sidecar to
  `Rung.comment`.

- **Project.ini-style metadata**: each backend has vendor-specific
  config (CPU model, network setup, HMI bindings).  We surface a few
  fields on `Program` (`cpu_model`, `project_name`); the rest is
  passed through opaquely on read and re-emitted on write.

- **Static vs. dynamic tag scope in multi-PLC systems**: A `Tag` is
  *static* when its ``address`` is set (backend must honour it
  verbatim) and *dynamic* when ``address=None`` (a tag allocator
  picks a free slot).  Static is the right default for physical I/O,
  HMI-referenced tags (regenerating PLC code must not move them),
  and tags exposed on a fieldbus.  Today the IL models a single PLC,
  so static/dynamic is a per-Program flag.  Once we add a second
  backend, static/dynamic becomes *per-PLC*: the same logical tag
  may be pinned on the device that owns it and dynamic on devices
  that only consume it.  Representing that needs a higher-level
  ``Project`` container holding multiple ``Program``s plus a
  tag-scoping policy.  Out of scope until we ship a second backend.

- **Cross-device tag sharing**: Tags exposed on Modbus, EtherNet/IP,
  or vendor-specific shared-memory protocols cross PLC boundaries.
  The IL today has no notion of "this tag is published or subscribed
  to over the network."  When we add a second backend we'll need a
  sharing manifest -- one ``SharedTagBinding`` table per Project
  mapping ``(producer_plc, tag_name) -> (consumer_plc, tag_name)``
  plus the transport.  Until then, every tag is PLC-local.
