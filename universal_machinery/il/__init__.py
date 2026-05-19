"""universal_machinery.il -- vendor-neutral intermediate language for PLC programs.

This package defines an AST that captures Ladder Diagram (LD) and a small
subset of Structured Text (ST) per IEC 61131-3 Part 3.  Vendor backends
in `universal_machinery.backends.<vendor>` lower IL down to vendor file
formats (CKP, PLCopen XML, Structured Text, ...) and parse those formats
back into IL.

Design goals:

  - Cover what real PLC programs need: contacts, coils, timers, counters,
    compare ops, math ops, function blocks, calls, jumps.  Anything an
    AutomationDirect CLICK or Allen-Bradley MicroLogix or OpenPLC project
    typically uses.
  - Be vendor-neutral: no CKP-specific bytes leak into the IL.
  - Round-trip: `decode -> IL -> encode` should be lossless for the same
    backend, lossy-but-meaningful across backends (some constructs may
    not exist on all targets and trigger an explicit ``Unsupported``).
  - Static-typeable: every AST node is a frozen dataclass.

Read the high-level ``Program``, the ``Subroutine`` collection of named
ladder routines, the ``Rung`` linear logic row, and the union ``Op``
that captures every supported instruction.

Example::

    from universal_machinery.il import Program, Subroutine, Rung, Address
    from universal_machinery.il.ops import ContactNO, OutCoil, Call, End

    prog = Program(
        subroutines=[
            Subroutine(
                name="Main",
                rungs=[
                    Rung([Call(target="Sub1")]),
                    Rung([End()]),
                ],
            ),
            Subroutine(
                name="Sub1",
                rungs=[
                    Rung([ContactNO(Address("X001")), OutCoil(Address("Y001"))]),
                ],
            ),
        ],
    )

    # Compile to a vendor target
    from universal_machinery.backends.click import ClickBackend  # via submodule
    ClickBackend().write(prog, "out.ckp")
"""
from .ast import (
    Address,
    DataBlock,
    PouKind,
    Program,
    Rung,
    Subroutine,
    Tag,
    TagRef,
    TagType,
    Var,
    VarDirection,
)
from . import fbd, ops, sfc, st
from .fbd import (
    BlockPin, Connection, FbBlock, FbdElement, FbdJump, FbdLabel,
    FbdNetwork, FbdReturn, InOutVariable, InVariable, OutVariable,
    Position,
)
from .ops import (
    FTrig, Loc, RS, RTrig, SR, STD_FUNCTION_NAMES, StdFunc, Value, VendorOp,
)
from .sfc import Action, SfcNetwork, Step, Transition
from .st import (
    Assignment, BinaryExpr, BinaryOp, CaseClause, CaseStatement,
    CommentStatement, ContinueStatement, ExitStatement, Expression,
    FieldAccess, ForStatement, FunctionCallExpr, FunctionCallStatement,
    GotoStatement, IfStatement, IndexAccess, LabelStatement, Literal,
    RepeatStatement, ReturnStatement, Statement, UnaryExpr, UnaryOp,
    VarRef, WhileStatement, is_lvalue, walk_expressions,
)
from .types import (
    AliasType, ArrayType, DataType, EnumType, NamedType, StructType,
    SubrangeType, UserType, is_elementary, is_signed_subrange,
    is_user_type, type_name,
)
from .configuration import (
    Configuration, PouInstance, Resource, TaskSpec,
)
from .oop import AccessSpec, Interface, Method

__all__ = [
    "AccessSpec",
    "Action",
    "Address",
    "AliasType",
    "ArrayType",
    "Assignment",
    "BinaryExpr",
    "BinaryOp",
    "BlockPin",
    "CaseClause",
    "CaseStatement",
    "CommentStatement",
    "Configuration",
    "Connection",
    "ContinueStatement",
    "DataBlock",
    "DataType",
    "EnumType",
    "ExitStatement",
    "Expression",
    "FTrig",
    "FbBlock",
    "FbdElement",
    "FbdJump",
    "FbdLabel",
    "FbdNetwork",
    "FbdReturn",
    "FieldAccess",
    "ForStatement",
    "FunctionCallExpr",
    "FunctionCallStatement",
    "GotoStatement",
    "IfStatement",
    "InOutVariable",
    "InVariable",
    "IndexAccess",
    "Interface",
    "LabelStatement",
    "Literal",
    "Loc",
    "Method",
    "NamedType",
    "OutVariable",
    "PouInstance",
    "PouKind",
    "Position",
    "Program",
    "RS",
    "RTrig",
    "RepeatStatement",
    "Resource",
    "ReturnStatement",
    "Rung",
    "SR",
    "SfcNetwork",
    "STD_FUNCTION_NAMES",
    "Statement",
    "StdFunc",
    "Step",
    "StructType",
    "SubrangeType",
    "Subroutine",
    "Tag",
    "TagRef",
    "TagType",
    "TaskSpec",
    "Transition",
    "UnaryExpr",
    "UnaryOp",
    "UserType",
    "Value",
    "Var",
    "VarDirection",
    "VarRef",
    "VendorOp",
    "WhileStatement",
    "fbd",
    "is_elementary",
    "is_lvalue",
    "is_signed_subrange",
    "is_user_type",
    "ops",
    "sfc",
    "st",
    "type_name",
    "walk_expressions",
]

__version__ = "0.10.0"
