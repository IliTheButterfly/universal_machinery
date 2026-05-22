# `universal_machinery.parsers`

External formats → IL.

## PLCopen TC6 XML

::: universal_machinery.parsers.plcopen_xml
    options:
      members:
        - parse_plcopen_xml
        - parse_plcopen_xml_file
        - PlcopenParseError

## ST text (body / expression only)

!!! warning "Experimental"
    `parse_st_body` and `parse_st_expression` cover ST *statement* and *expression* parsing only.  There is no full-program ST parser yet — the openplc / rusty backends' `read(.st)` raises `NotImplementedError` because of this gap.

::: universal_machinery.parsers.st_text
    options:
      members:
        - parse_st_body
        - parse_st_expression
        - StParseError
