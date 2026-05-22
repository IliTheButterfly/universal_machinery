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
    `parse_program` is at v1: PROGRAM / FUNCTION / FUNCTION_BLOCK + VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT / VAR (LOCAL) + body.  Out of scope (raise `StParseError`): VAR_EXTERNAL / VAR_TEMP / VAR_GLOBAL, AT clauses, TYPE blocks, CONFIGURATION, METHOD / INTERFACE, SFC text.  `parse_st_body` and `parse_st_expression` cover the statement / expression layers.

::: universal_machinery.parsers.st_text
    options:
      members:
        - parse_program
        - parse_st_body
        - parse_st_expression
        - StParseError
