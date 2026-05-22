# `universal_machinery.il`

The vendor-neutral intermediate language.  Every node is a frozen dataclass.

::: universal_machinery.il
    options:
      show_submodules: false
      members:
        - Program
        - Subroutine
        - PouKind
        - Rung
        - Var
        - VarDirection
        - TagType
        - Tag
        - TagRef
        - Address
        - DataBlock

## LD ops

::: universal_machinery.il.ops
    options:
      show_root_heading: false
      members:
        - Value
        - Loc
        - SR
        - RS
        - RTrig
        - FTrig
        - StdFunc
        - STD_FUNCTION_NAMES
        - VendorOp
        - is_iec_std_function
        - iec_convertible_types

## SFC

::: universal_machinery.il.sfc
    options:
      show_root_heading: false
      members:
        - SfcNetwork
        - Step
        - Transition
        - Action

## Structured Text AST

::: universal_machinery.il.st
    options:
      show_root_heading: false
      members:
        - Expression
        - Literal
        - VarRef
        - FieldAccess
        - IndexAccess
        - UnaryExpr
        - UnaryOp
        - BinaryExpr
        - BinaryOp
        - FunctionCallExpr
        - Statement
        - Assignment
        - IfStatement
        - CaseClause
        - CaseStatement
        - ForStatement
        - WhileStatement
        - RepeatStatement
        - ReturnStatement
        - ExitStatement
        - ContinueStatement
        - CommentStatement
        - GotoStatement
        - LabelStatement
        - FunctionCallStatement
        - is_lvalue
        - walk_expressions

## FBD

::: universal_machinery.il.fbd
    options:
      show_root_heading: false
      members:
        - FbdNetwork
        - FbdElement
        - FbBlock
        - BlockPin
        - Connection
        - InVariable
        - OutVariable
        - InOutVariable
        - FbdJump
        - FbdLabel
        - FbdReturn
        - Position

## User-defined types

::: universal_machinery.il.types
    options:
      show_root_heading: false
      members:
        - DataType
        - UserType
        - NamedType
        - StructType
        - ArrayType
        - EnumType
        - SubrangeType
        - AliasType
        - type_name
        - is_elementary
        - is_user_type
        - is_signed_subrange

## Configuration / Resource / Task (§2.7)

::: universal_machinery.il.configuration
    options:
      show_root_heading: false
      members:
        - Configuration
        - Resource
        - TaskSpec
        - PouInstance
        - AccessVar
        - ConfigVar

## IEC 3rd-edition OOP

::: universal_machinery.il.oop
    options:
      show_root_heading: false
      members:
        - Method
        - Interface
        - AccessSpec
