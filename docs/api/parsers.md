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
    `parse_program` is at v6: PROGRAM / FUNCTION / FUNCTION_BLOCK + all seven VAR_* directions + IEC §2.4.1.1 AT clauses (`%I*` / `%Q*` / `%M*`) + IEC §2.3.3 TYPE blocks (STRUCT / ARRAY / ENUM / SUBRANGE / ALIAS) + IEC §2.7 CONFIGURATION / RESOURCE / TASK + IEC 3rd-edition OOP (METHOD with PUBLIC / PRIVATE / PROTECTED / INTERNAL + ABSTRACT / OVERRIDE; INTERFACE blocks; FUNCTION_BLOCK EXTENDS / IMPLEMENTS / ABSTRACT) + IEC §6.7 SFC text (INITIAL_STEP / STEP / TRANSITION / ACTION with AND/OR/NOT condition lowering + `(a, b)` step lists for div/conv) + body.  Out of scope (raise `StParseError`): CLASS / class-level OOP.  `parse_st_body` and `parse_st_expression` cover the statement / expression layers.

::: universal_machinery.parsers.st_text
    options:
      members:
        - parse_program
        - parse_st_body
        - parse_st_expression
        - StParseError
