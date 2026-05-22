# API reference

Auto-generated from docstrings via [mkdocstrings].  The stability tier for each name is documented in [API stability](../API_STABILITY.md); when in doubt, **stick to what's listed Stable**.

| Module | What lives here |
|---|---|
| [`il`](il.md) | Intermediate language: every AST node — Program / Subroutine / Rung / Var / TagType, the LD ops, SFC, ST, FBD, UDTs, Configuration, OOP. |
| [`builders`](builders.md) | Convenience constructors for authoring IL programs (`prog`, `fn`, `fb`, `rung`, `no`, `coil`, `ton`, `assign`, `if_`, ...). |
| [`emitters`](emitters.md) | IL → external format: `emit_program` (ST) and `emit_xml` (PLCopen TC6 XML), plus the XSD validator. |
| [`parsers`](parsers.md) | External format → IL: PLCopen XML reader (full-program); ST text parser (body / expression only). |
| [`validation`](validation.md) | Structural validator. Returns a list of `ValidationError`s (one per issue); never raises. |
| [`serialisation`](serialisation.md) | Canonical IL JSON: `to_json` / `from_json` round-trip the full IL. |
| [`backends`](backends.md) | Backend ABC, `@register("name")` decorator, registry helpers. |
| [`exceptions`](exceptions.md) | Structured exception hierarchy: `UniversalMachineryError` + its concrete subclasses. |
| [`lowering`](lowering.md) | IL → IL passes: FBD → ST, CLICK calling convention. |
| [`cli`](cli.md) | `um` console entry point (Typer app). |

[mkdocstrings]: https://mkdocstrings.github.io/
