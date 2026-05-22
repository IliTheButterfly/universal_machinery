# `universal_machinery.exceptions`

Structured exception hierarchy.  Every library-originated exception inherits from `UniversalMachineryError` — catch it to handle any library failure in one branch.

```
UniversalMachineryError
  ├── SerialisationError      (.serialisation)
  ├── PlcopenParseError       (.parsers.plcopen_xml)
  ├── StParseError            (.parsers.st_text)
  ├── XMLSchemaError          (.emitters.plcopen_xml)
  ├── UnsupportedOpError      (.backends)
  ├── LoweringError           (.exceptions; re-exported from both lowering modules)
  └── RoundTripError          (.exceptions)
```

Pinned by `tests/test_exception_hierarchy.py` + `tests/test_public_api_surface.py::test_exception_hierarchy_consistency`.

::: universal_machinery.exceptions
    options:
      members:
        - UniversalMachineryError
        - LoweringError
        - RoundTripError
