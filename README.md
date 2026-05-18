# universal_machinery

Open-source toolkit for programming PLCs across vendors via a vendor-neutral intermediate language.

PLC programming is dominated by closed, proprietary, single-vendor stacks: AutomationDirect's CLICK, Allen-Bradley's RSLogix, Siemens' TIA Portal — each with its own undocumented file format, its own dialect of ladder logic, and no clean way to migrate code between them or version it sensibly.  This project aims to fix that.

The plan: a single intermediate language that captures Ladder Diagram and Structured Text programs, plus a registry of vendor backends that read/write each manufacturer's file format and the open-source [OpenPLC runtime](https://openplcproject.com/).  Write your control logic once; deploy anywhere supported.

## Status — alpha

| Layer | What works |
|---|---|
| Vendor-neutral IL (`universal_machinery.il`) | AST defined per IEC 61131-3 Part 3: contacts, coils, timers, counters, compare, math, call/return, jump/label, parallel groups |
| CLICK backend (`backends/click/`) | Read AND write `.ckp` files end-to-end; round-trips byte-identical for unedited projects; edited files load cleanly in CLICK Programming Software v3.43 |
| OpenPLC backend (`backends/openplc/`) | Skeleton only — no emitter yet |
| CLI | Not yet implemented |
| GUI | Not yet implemented — see [`docs/ROADMAP.md`](docs/ROADMAP.md) |

## Layout

```
universal_machinery/
├── universal_machinery/            # the library
│   ├── il/                         # vendor-neutral AST + LD ops
│   └── backends/                   # Backend ABC + registry
├── backends/                       # vendor backends (each is a git submodule)
│   ├── click/                      # AutomationDirect CLICK PLC (.ckp)
│   └── openplc/                    # OpenPLC runtime (PLCopen XML / ST)
├── tests/
├── examples/
└── docs/
    └── ARCHITECTURE.md
```

## Install

The repo uses git submodules for vendor backends:

```sh
git clone --recurse-submodules https://github.com/iliana/universal_machinery
cd universal_machinery
pip install -e .
pip install -e backends/click       # or any other backend you need
```

If you already cloned without `--recurse-submodules`:

```sh
git submodule update --init --recursive
```

## Quickstart

Read a CLICK project, inspect it, edit it, save it back:

```python
from click_plc import decode_ckp, Rung

project = decode_ckp(open('Project.ckp', 'rb').read())
print(project.render_program())

project.add_rung(sub_id=2, rung=Rung.no_out("C40", "C41"))
project.add_rung(sub_id=2, rung=Rung.no_nc_out("C50", "X005", "C051"))
project.add_rung(sub_id=2, rung=Rung.copy("DS30", "DS31"))
project.save('out.ckp')
```

Build a vendor-neutral program from scratch in IL (does not yet emit to any backend):

```python
from universal_machinery.il import Address, Program, Rung, Subroutine
from universal_machinery.il.ops import Call, ContactNC, ContactNO, End, OutCoil, Return

prog = Program(
    subroutines=[
        Subroutine(name="Main", main=True, rungs=[
            Rung([Call(target="MyFn")]),
            Rung([End()]),
        ]),
        Subroutine(name="MyFn", rungs=[
            Rung([
                ContactNO(Address("X001")),
                ContactNC(Address("X002")),
                OutCoil(Address("Y001")),
            ]),
            Rung([Return()]),
        ]),
    ],
)
```

## Project goals (longer-form)

1. **A useful intermediate language.**  Faithful to IEC 61131-3 Part 3 so anyone with a textbook can read it, but Pythonic enough that you can build and inspect programs from notebooks.

2. **A reliable vendor library.**  CLICK first (because we have it working), then OpenPLC (because it's open and is the natural reference target), then expanding.

3. **Round-trip honesty.**  A backend's `read` followed by `write` should produce an output the vendor tool accepts — and ideally byte-identical for unedited input.  The CLICK backend already does this.

4. **No silent lossiness.**  When IL can express something a backend can't (or vice-versa), the backend raises `UnsupportedOpError` instead of producing a quietly wrong file.

5. **Open-source-first.**  AGPL-3.0 to ensure derivatives stay open --
   including SaaS / hosted IDEs built on top of this project (which a
   plain GPL-3.0 would leave a loophole for).

## Adding a new backend

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the [`Backend`](universal_machinery/backends/base.py) ABC.  Steps:

1. Create a new git repo (or directory) for your backend; add it as a submodule under `backends/<vendor>/`.
2. Implement a `Backend` subclass with `read()` and `write()`.
3. Declare your `capabilities` (which IL ops you can faithfully round-trip).
4. Tests under `tests/<vendor>/`.

## License

AGPL-3.0-or-later.  See [`LICENSE`](LICENSE).

Contributions require a `Signed-off-by` line per the
[Developer Certificate of Origin](https://developercertificate.org/).
See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Status of the CLICK reverse engineering

All findings are in [`backends/click/click_plc/ckp_decoder.py`](backends/click/click_plc/ckp_decoder.py) — see the module docstring and inline comments.  Highlights:

- **File magic** is `XOR16` of u16 LE words from offset 2 to end-of-file (reverse-engineered from `CLICK.exe`'s `FUN_004fe470`).
- **Section table** at file offsets 0x06+ holds `(offset, size)` pairs for the named sections.
- **Ladder logic** is encoded as a stream of length-prefixed UTF-16LE name strings + opcode bytes + 16-byte metadata + memory-tag pstrs, with rungs delimited by a 66-byte bitmap.
- **Project.ini security tokens** (`wsep`/`wseu`/`emsep`/`emseu`) rotate per save but don't seem to affect file validity, so we leave them untouched.

Many thanks to AutomationDirect for shipping a binary that's tractable to RE.
