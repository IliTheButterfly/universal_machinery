# `universal_machinery.cli`

The `um` console entry point.  Thin Typer wrapper over the library's existing functions.

Verbs:

| Verb | What it does |
|---|---|
| `um inspect <file>` | Structural summary of an IL Program |
| `um validate <file>` | Run the validator; exit non-zero on errors |
| `um emit <file> -f st\|xml\|json` | Lower to ST / PLCopen XML / canonical JSON |
| `um diff <a> <b>` | Unified diff of two IL programs in canonical JSON form |
| `um import <file.xml>` | Parse PLCopen TC6 XML into IL JSON |
| `um lint <file> -f text\|json` | CI-friendly validation; accepts JSON or XML |
| `um convert <in> <out>` | Any-format-to-any-format (JSON / XML / ST) via the IL |

::: universal_machinery.cli
    options:
      members:
        - app
        - main
        - inspect
        - validate_
        - emit
        - diff
        - import_
        - lint
        - convert
