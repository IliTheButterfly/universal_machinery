# universal_machinery

Open-source toolkit for programming PLCs across vendors via a vendor-neutral intermediate language (IL).

> **Status: alpha.**  IL + emitters + parsers are stable; backend reach is growing.  See [API stability](API_STABILITY.md) for the per-construct guarantees.

## The big idea

Write your control logic once in a vendor-neutral IL.  Emit to any supported PLC's project format.  Two reference compilers (matiec + rusty) validate the IL → ST emit path so the cert claim is grounded in real-tool acceptance, not just XSD validation.

```
                        AutomationDirect CLICK (.ckp)
                      ┌───────────────────────────────►
       universal      │
       machinery.il   ├─────► OpenPLC (.st / PLCopen XML)
       (the IL)       │
                      └─────► rusty (.st via plc -c)
```

## What it does today

- **Author IL** in Python via the `builders` DSL — LD / SFC / ST / FBD / FUNCTION / FUNCTION_BLOCK + IEC 3rd-edition OOP (`METHOD` / `INTERFACE` / `EXTENDS` / `IMPLEMENTS` / `ABSTRACT`).
- **Emit** to IEC §3 Structured Text or PLCopen TC6 v2.01 XML.  Output is XSD-validated; matiec round-trip passes 38/38 cases, rusty 29/38 (with 9 xfailed tracking rusty-side stdlib divergences).
- **Parse** PLCopen XML back into IL — lossless for everything the v2.01 schema covers (POU declarations, var blocks, configuration model, ST bodies as raw text, FBD/SFC bodies).
- **Validate** structurally with 31 distinct error codes (`um lint` consumes this).
- **Three vendor backends** registered: `openplc` (ST + XML), `rusty` (ST, validates 3rd-ed OOP), `click` (scaffold — encoder pending).
- **CLI**: `um inspect / validate / emit / diff / import / lint / convert`.

## Cert posture

Per [IEC_CONFORMANCE.md](IEC_CONFORMANCE.md):

- **Tier 1** (PLCopen TC6 XML): SOUND.  matiec + rusty acceptance matrix in [CONFORMANCE_TEST_PLAN.md § Reference-compiler acceptance matrix](CONFORMANCE_TEST_PLAN.md#reference-compiler-acceptance-matrix).  Intersection cert claim: 29 / 38 constructs.
- **Tier 2** (full IEC 61131-3): phased; backend reach (CLICK / OpenPLC lowering) is the gating factor.
- **Tier 3** (IEC 61508 functional safety): explicitly out of scope.

## Quick start

```python
from universal_machinery.builders import program, prog, rung, no, coil, ton, var
from universal_machinery.il import NamedType, TagType
from universal_machinery.il.ast import Var, VarDirection

# Author the IL
p = program(subroutines=[
    prog("Main", main=True,
         local_vars=[
             var("trigger", TagType.BOOL),
             var("done", TagType.BOOL),
             Var(name="t1", data_type=NamedType("TON"),
                 direction=VarDirection.LOCAL),
         ],
         rungs=[rung(no("trigger"), ton("t1", 1000, done_bit="done"))]),
])

# Emit to vendor formats
from universal_machinery.emitters.st import emit_program
from universal_machinery.emitters.plcopen_xml import emit_xml
print(emit_program(p))    # IEC §3 Structured Text
print(emit_xml(p))         # PLCopen TC6 v2.01 XML

# Or via the registered Backend ABCs
import openplc_backend  # registers as "openplc"
import rusty_backend    # registers as "rusty"
from universal_machinery.backends import get_backend
get_backend("openplc").write(p, "out.xml")
get_backend("rusty").write(p, "out.st")
```

## CLI

```sh
um inspect prog.json              # structural summary
um validate prog.json             # exit non-zero on validation errors
um emit prog.json -f st           # emit IEC ST
um emit prog.json -f xml          # emit PLCopen XML
um convert prog.xml prog.st       # any-format-to-any-format via the IL
um diff before.json after.json    # canonical-JSON diff
um lint prog.json -f json         # machine-readable validation output
```

## Roadmap

Headline pending items: finish the CLICK encoder so `Program → .ckp` round-trips; full-program ST parser; GUI; cross-platform packaging.  The full roadmap lives in `docs/ROADMAP.md` in the source tree.

## License

AGPL-3.0-or-later.  Backend submodules carry the same license.  Contributions require DCO sign-off (`git commit -s`).
