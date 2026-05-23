# `universal_machinery.parsers`

External formats → IL.

## PLCopen TC6 XML

::: universal_machinery.parsers.plcopen_xml
    options:
      members:
        - parse_plcopen_xml
        - parse_plcopen_xml_file
        - PlcopenParseError

## ST text

!!! warning "Experimental"
    `parse_program` is at v3: PROGRAM / FUNCTION / FUNCTION_BLOCK + all seven VAR_* directions + IEC §2.4.1.1 AT clauses (`%I*` / `%Q*` / `%M*`) + IEC §2.3.3 TYPE blocks (STRUCT / ARRAY / ENUM / SUBRANGE / ALIAS) + body.  Out of scope (raise `StParseError`): CONFIGURATION, METHOD / INTERFACE, SFC text.  `parse_st_body` and `parse_st_expression` cover the statement / expression layers.

::: universal_machinery.parsers.st_text
    options:
      members:
        - parse_program
        - parse_st_body
        - parse_st_expression
        - StParseError
