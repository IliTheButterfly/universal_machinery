# Bundled XSD schemas

## `tc6_xml_v201.xsd` -- PLCopen TC6 XML for IEC 61131-3, v2.01

The official interchange schema for IEC 61131-3 / PLCopen TC6 v2.01.
Used by `universal_machinery.emitters.plcopen_xml.validate_plcopen_xml()`
to validate emitted XML against the canonical structure expected by
conformant tools (matiec, Beremiz, OpenPLC, vendor IDEs).

### Provenance

Sourced from the Beremiz project's public GitHub mirror at
`https://github.com/beremiz/beremiz/blob/master/plcopen/tc6_xml_v201.xsd`,
which has bundled this schema with their GPL-licensed PLC IDE for
years.  The schema itself is a standards-body specification authored
by PLCopen TC6; PLCopen distributes member-gated copies of the same
schema content.

### Licensing note

The schema's redistribution terms are not embedded in the file
itself.  Multiple open-source PLC projects bundle and redistribute
the v2.01 XSD on the understanding that standards-body schemas like
this one are reference specifications, not proprietary works.  If
you have a more authoritative source (e.g. via a PLCopen
membership), you can replace this file or pass a different schema
path to `validate_plcopen_xml(xml, xsd_path=...)`.

### Updating

To replace with a newer schema version (e.g. TC6 v3.0):

1. Drop the new XSD file into this directory.
2. Update `_BUNDLED_XSD_NAME` in `plcopen_xml.py`.
3. Re-run `tests/emitters/test_plcopen_xsd_validation.py` -- any
   conformance gaps in the emitter relative to the new schema will
   surface as test failures.

The bundled XSD is shipped as package data (see
`pyproject.toml: [tool.setuptools.package-data]`).
