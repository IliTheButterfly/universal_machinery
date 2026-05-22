# Public API stability

`universal_machinery` is pre-1.0 (`__version__ = "0.1.0"`).  Until 1.0, the *whole* surface is technically allowed to move.  In practice we treat the table below as the working contract: things marked **Stable** won't break across minor versions unless we publish a deprecation notice first; things marked **Experimental** may move without notice; things marked **Internal** are private — importing them is your problem.

The split here matters for three audiences:

- **Library users** scripting against the IL — should stick to **Stable**.
- **Backend authors** building submodules (like `openplc_backend`, `rusty_backend`) — can use everything in the **Stable** + **Backend-author** rows; **Experimental** at their own risk.
- **Contributors** working inside the codebase — anything goes, but breaking a Stable contract requires a deprecation cycle.

## Stability tiers

| Tier | Meaning | Removal policy |
|---|---|---|
| **Stable** | Public contract.  Importable from `universal_machinery.<name>` or `universal_machinery.<subpkg>.<name>`. | Breaking change requires one minor-version deprecation cycle. |
| **Backend-author** | Stable, but addressed at backend authors specifically.  Library users normally don't need it. | Same deprecation policy as Stable. |
| **Experimental** | Listed in `__all__` so it's reachable; the *shape* may move between minor versions. | Renames / removals without a deprecation cycle. |
| **Internal** | Not in any `__all__`; underscored module/name; or in a `_*` helper.  Don't import. | We will break these whenever convenient. |

## Top-level package: `universal_machinery`

| Name | Tier | Notes |
|---|---|---|
| `il` (subpackage) | **Stable** | Vendor-neutral intermediate language.  Every IL node + builder DSL lives here. |
| `builders` (subpackage) | **Stable** | Convenience constructors (`prog`, `fn`, `fb`, `rung`, `no`, `coil`, `ton`, `ctu`, `assign`, `if_`, `case_`, `for_`, ...).  De-facto required for any non-trivial program-authoring; pinned by every test + backend submodule. |
| `emitters` (subpackage) | **Stable** | `emit_program(prog)` (ST) and `emit_xml(prog)` (PLCopen TC6).  Outputs validated by the matiec + rusty round-trip harnesses. |
| `parsers` (subpackage) | **Stable** | `parse_plcopen_xml(xml)` / `parse_plcopen_xml_file(path)`.  `parsers.st_text` is **Experimental** — see its row below. |
| `validation` (module) | **Stable** | `validate(program) -> list[ValidationError]`.  `ValidationError` is a *dataclass* (one issue per item), not an exception. |
| `serialisation` (module) | **Stable** | `to_json` / `from_json` / `to_dict` for canonical IL JSON. |
| `backends` (subpackage) | **Backend-author** | `Backend` ABC, `@register("name")` decorator, `get_backend("name")`, `registered_names()`. |
| `exceptions` (module) | **Stable** | `UniversalMachineryError` base + the structured hierarchy (PR #74). |
| `cli` (module) | **Stable** | `um` console entry point.  Verbs (`inspect` / `validate` / `emit` / `diff` / `import` / `lint` / `convert`) covered by `tests/test_cli.py`. |
| `lowering` (subpackage) | **Experimental** | `fbd_to_st.lower_fbd_to_st` is stable; `click_calling` is partial (scheduler trampoline + inside-subroutine-call rewriter are still TODO per the module docstring). |

## `universal_machinery.il`

Everything in `il.__all__` is **Stable** except:

| Name | Tier | Notes |
|---|---|---|
| `il.oop` (`Method`, `Interface`, `AccessSpec`) | **Experimental** | IEC 3rd-edition OOP.  Cert posture: compiler axis unlocked via rusty (`backends/rusty`), XSD axis still blocked at PLCopen TC6 v2.01.  Shape may evolve when the v2.02+ schema lands. |
| `il.ops.VendorOp` | **Experimental** | Non-IEC by design.  Carries vendor-specific instruction identity for round-trip; not portable across backends. |
| `il.DataBlock` | **Experimental** | S7-style global / instance DB.  Vendor-extension (CLICK uses it for contiguous memory layouts; not IEC).  ST emit preserves all fields per PR #69; XML emit is via a synthetic POU. |

Everything else (`Program`, `Subroutine`, `Var`, `TagType`, ops, ST AST, SFC AST, FBD AST, UDTs, Configuration model) is **Stable**.

## `universal_machinery.builders`

The builder DSL.  Every public function is **Stable** with the exception of recently-added kwargs that may need more bake-in time:

- `_make_pou(external_vars=, temp_vars=, global_vars=)` (added in PR #64) — Stable.
- The OOP builders (`method`, `abstract_method`, `interface`) — **Experimental**, same caveat as `il.oop`.

## `universal_machinery.emitters`

| Name | Tier | Notes |
|---|---|---|
| `emit_program(program)` (ST) | **Stable** | matiec + rusty round-trip validated. |
| `emit_xml(program, time_now=None)` (PLCopen TC6) | **Stable** | XSD-validated against bundled v2.01 schema. |
| `validate_plcopen_xml(xml)` | **Stable** | Raises `XMLSchemaError` on schema violations. |

## `universal_machinery.parsers`

| Name | Tier | Notes |
|---|---|---|
| `parse_plcopen_xml(xml)` | **Stable** | Returns `il.Program`. |
| `parse_plcopen_xml_file(path)` | **Stable** | Path-taking wrapper. |
| `PlcopenParseError` | **Stable** | Raised by both. |
| `parse_st_body(src)` | **Experimental** | Parses a *body* (statement list).  No full-program parser exists yet — see the `um convert prog.st X` rejection and the openplc/rusty `read(.st)` `NotImplementedError`. |
| `parse_st_expression(src)` | **Experimental** | Single-expression parser. |
| `StParseError` | **Stable** | Raised by both ST parsers. |

## `universal_machinery.backends`

| Name | Tier | Notes |
|---|---|---|
| `Backend` (ABC) | **Backend-author** | Backend classes inherit from this. |
| `@register("name")` | **Backend-author** | Class decorator that adds to the registry on import. |
| `get_backend("name")` | **Stable** | Resolves a registered backend by string name. |
| `registered_names()` | **Stable** | Returns the list of registered backend names. |
| `UnsupportedOpError` | **Stable** | Raised by backend lowering passes when an IL op isn't lowerable. |

Backends themselves live in submodules:

- `click_plc` (vendor: AutomationDirect CLICK) — `decode_ckp` is **Stable**; no `Backend` ABC implementation yet (the lowering / encoder side is a roadmap item).
- `openplc_backend.OpenPlcBackend` — **Stable**.  Capabilities advertised; ST + PLCopen XML write, PLCopen XML read.  `read(.st)` is `NotImplementedError` until a full-program ST parser lands.
- `rusty_backend.RustyBackend` — **Stable**.  ST-only (rusty doesn't take XML); includes IEC 3rd-edition OOP capabilities.

## `universal_machinery.exceptions`

| Name | Tier | Notes |
|---|---|---|
| `UniversalMachineryError` | **Stable** | Catch-all base for every library-originated exception. |
| `LoweringError` | **Stable** | Re-exported from `.lowering.click_calling` and `.lowering.fbd_to_st` so `except LoweringError` works regardless of import path (fixed in PR #74). |
| `RoundTripError` | **Stable** | Not raised by the library yet; defined so tooling asserting loss-free round-trip has a canonical exception class. |
| `SerialisationError` (via `serialisation`) | **Stable** | |
| `PlcopenParseError` (via `parsers.plcopen_xml`) | **Stable** | |
| `StParseError` (via `parsers.st_text`) | **Stable** | |
| `XMLSchemaError` (via `emitters.plcopen_xml`) | **Stable** | |
| `UnsupportedOpError` (via `backends`) | **Stable** | |

All inherit from `UniversalMachineryError`.  Pinned by `tests/test_exception_hierarchy.py`.

## `universal_machinery.validation`

| Name | Tier | Notes |
|---|---|---|
| `validate(program) -> list[ValidationError]` | **Stable** | |
| `ValidationError` (dataclass) | **Stable** | One validation issue.  Not an exception -- `validate()` returns a list rather than raising so callers see every issue. |
| Specific error codes (e.g. `"st-unresolved-goto"`) | **Stable** | The code strings are part of the contract (`um lint -f json` records them in machine-readable output). |

## `universal_machinery.serialisation`

| Name | Tier | Notes |
|---|---|---|
| `to_json(program, indent=2, sort_keys=False)` | **Stable** | Canonical IL JSON.  Used by `um emit -f json` / `um convert *.json`. |
| `from_json(text)` | **Stable** | Round-trips with `to_json`. |
| `to_dict(program)` / `from_dict(d)` | **Stable** | Without the JSON wrapping. |
| `SerialisationError` | **Stable** | Inherits from `UniversalMachineryError`. |

## Internal / `_` prefixed

Anything that starts with an underscore.  Same convention as the rest of Python: the leading underscore is the marker.  Concretely:

- Modules like `universal_machinery.emitters.plcopen_xml._emit_*` helpers.
- Constants like `_BUILTIN_BLOCK_PIN_TYPES`, `_POU_KEYWORD`, etc.
- Private helpers inside `lowering/`.
- The `tests/` package itself — pinning behaviour for ourselves, not a public contract.

We will rename, restructure, or remove these without warning.

## Version policy

| Component | Versioned | Bumped by |
|---|---|---|
| `universal_machinery.__version__` | yes | Library breaking changes (Stable surface). |
| `universal_machinery.il.__version__` | yes | IL schema breaking changes (e.g., dataclass field rename in a Stable type). |
| Each backend submodule's `__version__` | yes | Independent of the parent — each submodule has its own release cadence. |

Until 1.0, minor bumps (`0.x.0`) signal breaking changes within the Stable surface; patch bumps (`0.x.y`) signal fixes; the version field tracks intent, not strict semver.

## Surface map cheat sheet

For quick reference, the imports that count as **using the public API**:

```python
# IL authoring + introspection
from universal_machinery.builders import prog, fn, rung, no, coil, ton, assign
from universal_machinery import il               # full IL surface
from universal_machinery.il import Program, Subroutine, TagType, Var

# Vendor-format I/O
from universal_machinery.emitters.st import emit_program
from universal_machinery.emitters.plcopen_xml import emit_xml, validate_plcopen_xml
from universal_machinery.parsers.plcopen_xml import parse_plcopen_xml_file

# Validation + serialisation
from universal_machinery.validation import validate, ValidationError
from universal_machinery.serialisation import to_json, from_json

# Errors
from universal_machinery import UniversalMachineryError
from universal_machinery.exceptions import LoweringError, RoundTripError

# Backend registry
from universal_machinery.backends import get_backend, registered_names

# Importing a backend package (side effect: registers it)
import openplc_backend   # noqa
import rusty_backend     # noqa
```
