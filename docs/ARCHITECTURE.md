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
[`il/sfc.py`](../universal_machinery/il/sfc.py).

For how all of this lowers onto a vendor target that has neither
parameters nor nested calls, see
[click_calling_convention.md](click_calling_convention.md).

### Op categories

See [`universal_machinery/il/ops.py`](../universal_machinery/il/ops.py)
for the canonical list.  Categories:

  - **Bit input**: `ContactNO`, `ContactNC`, `ContactRisingEdge`,
    `ContactFallingEdge`
  - **Bit output**: `OutCoil`, `OutSet`, `OutReset`
  - **Timers**: `TON` (on-delay), `TOF` (off-delay), `TP` (pulse)
  - **Counters**: `CTU`, `CTD`, `CTUD`
  - **Compare**: `Compare(op, lhs, rhs)` for ==, !=, <, <=, >, >=
  - **Math**: `Move`, `BinaryMath` for +, -, *, /, %
  - **Control flow**: `Call`, `Return`, `End`, `Jump`, `Label`
  - **Topology**: `ParallelGroup`

Every op is a frozen dataclass — the AST is hashable, comparable, and
trivially serialisable (via `dataclasses.asdict`).

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

Not every backend can express every IL op.  CLICK has no native
`TON` (it has its own timer block); OpenPLC has no `Copy` (use IEC's
`MOVE`).  Backends declare which capabilities they support; callers
check before lowering:

```python
b = get_backend("click")
if "timers" not in b.capabilities:
    raise NotImplementedError("CLICK backend doesn't support timers yet")
b.write(program, "out.ckp")
```

When a backend encounters an op it can't lower, it raises
``UnsupportedOpError`` rather than silently producing a broken file.

### Round-trip guarantee

A backend's `read` + `write` should produce a file the vendor tool
accepts.  Ideally for unedited input it's byte-identical (the CLICK
backend achieves this).

Cross-backend round-trips (read CLICK → IL → write OpenPLC) are
expected to be lossy in the IL → vendor lowering only, with explicit
diagnostics on lossy ops.

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
