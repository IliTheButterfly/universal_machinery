# `universal_machinery.emitters`

IL → external text/binary formats.

## Structured Text (IEC §3)

::: universal_machinery.emitters.st
    options:
      members:
        - emit_program
        - emit_pou
        - emit_st_body
        - emit_statement
        - emit_rung

## PLCopen TC6 XML

::: universal_machinery.emitters.plcopen_xml
    options:
      members:
        - emit_xml
        - validate_plcopen_xml
        - XMLSchemaError
