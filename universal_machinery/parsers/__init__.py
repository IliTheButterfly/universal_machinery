"""Readers / parsers that turn external formats into IL ``Program``s.

The mirror image of ``universal_machinery.emitters``: each module
here reads one format and returns a vendor-neutral
``il.Program``.  Together with the emitter side this closes the
round-trip loop a cross-vendor migration tool needs:

    foreign-format file
        -> parsers.<format>.parse(...)
        -> il.Program
        -> emitters.<format>.emit(...)
        -> foreign-format file

Modules
-------

``plcopen_xml``
    Read PLCopen TC6 v2.01 XML documents (``<project>``) into IL.
    V1 covers POU declarations + variable interfaces +
    Configuration model + ST bodies as raw text.  Graphical
    bodies (LD / FBD / SFC) and user-defined-type declarations
    are explicit follow-up slices.
"""
from . import plcopen_xml, st_text
from .plcopen_xml import (
    PlcopenParseError, parse_plcopen_xml, parse_plcopen_xml_file,
)
from .st_text import (
    StParseError, parse_program, parse_st_body, parse_st_expression,
)

__all__ = [
    "PlcopenParseError",
    "StParseError",
    "parse_plcopen_xml",
    "parse_plcopen_xml_file",
    "parse_program",
    "parse_st_body",
    "parse_st_expression",
    "plcopen_xml",
    "st_text",
]
