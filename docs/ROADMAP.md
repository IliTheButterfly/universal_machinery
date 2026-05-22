# Roadmap

This document tracks the plan for turning `universal_machinery` from a Python library into a fully-fleshed-out, cross-platform application.

## Direction

Keep the Python core. Expose it through **three faces** that share the same underlying library:

1. **Library** — the existing `universal_machinery` / `click_plc` Python API, for users who want to script against PLC projects.
2. **CLI** — a headless command-line tool for scripting, CI, and power users.
3. **GUI** — a desktop application for control engineers who don't want to write code.

Performance is not the bottleneck for this kind of tool (PLC project files are small, edits are millisecond-scale). The real challenges are **distribution** and **end-user UX** — both of which are addressable without leaving Python.

A Rust port of the binary codecs (with PyO3 bindings) remains a longer-term option, but is explicitly **out of scope** until we hit a real performance or distribution ceiling that Python tooling can't clear.

## Cross-compatibility is a hard requirement

Windows and Linux are both **first-class targets** from day one. Every feature added must work on both, and CI must verify both.

- Engineers using CLICK on the factory floor are almost always on Windows.
- The open-source / OpenPLC side of the community lives on Linux.
- macOS is a nice-to-have but not a launch requirement.

Concretely this means:
- No platform-specific paths, shell-outs, or assumptions about line endings, case sensitivity, or installed runtimes.
- Test matrix covers at least Windows latest and a current Ubuntu LTS.
- All file I/O goes through `pathlib`; all binary I/O is explicit about endianness (already true in the CLICK decoder).

## TODOs

### Library (`universal_machinery`, `backends/click`)
- [ ] Audit the public API surface and document what is stable vs. experimental.
- [ ] Settle the IL ↔ CLICK lowering so `Program` can round-trip through `.ckp`.
- [ ] Finish the OpenPLC emitter so we have a second backend exercising the IL.
- [ ] Add structured errors (`UnsupportedOpError`, `RoundTripError`, etc.) that the CLI and GUI can surface nicely.

### CLI
- [ ] Pick a framework — **Typer** recommended (argparse-compatible, type-hint-driven, good `--help` UX).
- [ ] Verbs to implement, at minimum:
  - `um inspect <file>` — dump program structure, tags, rung count, etc.
  - `um convert <in> <out>` — translate between vendor formats via the IL.
  - `um diff <a> <b>` — semantic diff of two PLC projects (more useful than `git diff` on binary `.ckp`).
  - `um validate <file>` — check round-trip integrity and IL conformance.
- [ ] Make sure every CLI verb is a thin wrapper over a library function — no logic in the CLI layer.

### GUI
- [ ] Framework: **PySide6 (Qt)**. Native look on Windows + Linux, mature, and `QGraphicsView` is well-suited to drawing ladder diagrams.
- [ ] Minimum viable feature set:
  - Open / save `.ckp` files.
  - Project tree (subroutines, tags, data blocks).
  - Ladder diagram canvas — read-only first, editable second.
  - Tag/address editor.
  - Error panel that surfaces backend `UnsupportedOpError` / lowering issues.
- [ ] Keep the GUI a pure consumer of the library — no business logic in widgets.
- [ ] Theming should respect OS dark/light mode on both platforms.

### Packaging & distribution
- [ ] Choose **Briefcase** (or PyInstaller as a fallback) to produce:
  - A single-file `.exe` + MSI installer for Windows.
  - An AppImage (or `.deb` + Flatpak) for Linux.
- [ ] Set up GitHub Actions to build and publish release artifacts for both platforms on every tag.
- [ ] Make sure backends-as-submodules still work from a packaged build (no `pip install -e` at runtime).
- [ ] Code-signing: investigate cost/feasibility for Windows; Linux signing is a non-issue.

### Cross-platform CI
- [ ] CI matrix: `{ubuntu-latest, windows-latest} × {supported Python versions}`.
- [ ] Run the existing test suite on both, plus a smoke test that the packaged binary launches.
- [ ] A round-trip integration test: read a known `.ckp`, write it, diff bytes — on both OSes.

### Documentation
- [ ] User guide for the GUI (screenshots from both Windows and Linux).
- [ ] CLI reference generated from Typer.
- [ ] Library API reference (Sphinx or mkdocs — pick one).
- [ ] Per-backend capability matrix so users know what round-trips cleanly.

## Future / out of scope for now

- macOS builds.
- Rust port of the binary codecs with PyO3 bindings (revisit only if Python distribution or performance becomes a real blocker).
- Web-based editor.
- Live communication with PLC hardware (upload/download, online monitoring).
