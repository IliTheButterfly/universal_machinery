# Contributing to universal_machinery

Thanks for considering a contribution. universal_machinery is a
vendor-neutral PLC engineering toolchain; everything here is licensed
**AGPL-3.0** (see [LICENSE](LICENSE)) and we plan to keep it that way.

## Project values

- **Vendor-neutral by design.** The IL is the universal intermediate
  language; backends compile to / from vendor-specific formats.
  Anything you contribute should preserve that boundary.
- **Round-trip fidelity is sacred.** Read a vendor's project, do
  nothing, write it back -- the output should be byte-identical (or
  semantically identical, with explicit diagnostics for anything
  that couldn't round-trip). Silent loss is the worst bug.
- **Universal compilation, not lowest-common-denominator.** Any
  feature in any PLC can be made available on any other target with
  enough memory. Backends are compilers; missing-feature gaps are
  closed by lowering passes, not by removing the feature from the IL.

## How to contribute

1. **Open an issue first** for non-trivial changes -- it saves work
   and lets us flag design conflicts early. Trivial fixes (typos,
   docs, small bug fixes) can go straight to a PR.

2. **Sign off your commits** with the
   [Developer Certificate of Origin](https://developercertificate.org/).
   Add `Signed-off-by: Your Name <you@example.com>` to each commit
   (use `git commit -s`); this certifies you have the right to
   contribute the code under the project's licence. We do not use a
   CLA; signed-off-by + DCO is the contributor contract.

3. **One concern per PR.** Refactors, new features, bug fixes, and
   doc updates each belong in their own PR. Easier to review, easier
   to revert.

4. **Tests are required for code changes.** Pure-IL changes belong
   in `tests/il/`; backend changes in `tests/<vendor>/`; lowering
   passes in `tests/lowering/`. New ops, new POU shapes, new
   lowering passes -- all need behavioural tests.

5. **No silent capability loss.** If your backend can't lower an op,
   raise `UnsupportedOpError` with a specific message. Never produce
   a half-correct file.

## Code style

- Python 3.10+. Type-annotate everything. Frozen dataclasses for
  AST nodes.
- No comments explaining *what* code does -- well-named identifiers
  do that. Reserve comments for *why* a non-obvious choice was made.
- Match the docstring conventions in `universal_machinery/il/ast.py`
  for new public types.
- Run `pytest` before submitting. Run it again before merging.

## Backend contributions

A new backend usually lives in its own git repo and is added to the
monorepo as a submodule under `backends/<vendor>/`. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
backend-authoring contract. The agent-harness work (in flight) will
eventually provide a starter template + verification loop; until
then, the CLICK backend (`backends/click/`) is the reference
implementation.

If you're adding support for a vendor whose format requires
reverse-engineering: document what you discovered in
`backends/<vendor>/REVERSE_ENGINEERING.md` so the next person
doesn't have to redo it.

## Licensing of contributions

By signing off your commits with DCO, you confirm:

- The contribution is yours (or you have the right to submit it).
- The contribution will be licensed under the project's AGPL-3.0.
- The contribution may be redistributed under that licence.

We will not accept code that requires relicensing or that carries
incompatible terms (CC-BY-NC, proprietary, etc.).

## Reporting security issues

For security-sensitive bug reports (especially anything that affects
output correctness on real PLC hardware), email the maintainer
privately before opening a public issue. Wrong PLC code can crash
machinery; we'd rather coordinate a fix.
