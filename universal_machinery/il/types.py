"""User-defined types per IEC 61131-3 §2.3.3.

The IL's elementary types (BOOL, INT, REAL, TIME, STRING, ...) are
captured by the ``TagType`` enum in ``ast.py``.  User-defined types
(UDTs) -- STRUCT, ARRAY, ENUM, and simple aliases -- live here as
frozen dataclasses, separately so the IL stays IEC-shaped and the
two type families compose cleanly via the ``DataType`` union.

  ElementaryType  =  ``TagType``    (the enum in ast.py)
  UserType        =  StructType | ArrayType | EnumType | AliasType
  DataType        =  ElementaryType | UserType

Anywhere ``Var.data_type`` accepted ``TagType``, it now accepts the
wider ``DataType``.  ``Program.user_types`` is the program-level
declaration table (the IEC ``TYPE ... END_TYPE`` block).

References between user types use names, not direct object refs
(matches IEC's textual scoping rules).  A member of one struct can
have another struct's name as its type::

    Point = StructType("Point", members=(
        Var("x", TagType.INT), Var("y", TagType.INT)))
    Line  = StructType("Line",  members=(
        Var("start", NamedType("Point")), Var("end", NamedType("Point"))))

The resolver in a future validation pass will check that every
``NamedType`` resolves to a declared ``UserType``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Union

from .ast import TagType

if TYPE_CHECKING:
    from .ast import Var


# -----------------------------------------------------------------------------
# Type references
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class NamedType:
    """Reference to a user-defined type by name.

    Used inside another type's definition or inside ``Var.data_type``
    to point at a UDT without holding a direct object reference.  The
    name resolves against ``Program.user_types`` at lower / emit
    time.
    """
    name: str


# -----------------------------------------------------------------------------
# User-defined type variants
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class StructType:
    """IEC 61131-3 §2.3.3.1 STRUCT type.

    A named record of typed fields::

        TYPE Point :
            STRUCT
                x : INT;
                y : INT;
            END_STRUCT;
        END_TYPE

    Each member is a ``Var`` (declarations carry name, type, initial
    value, and comment).  Member ``data_type`` may be elementary
    (``TagType``) or a ``NamedType`` referencing another UDT.

    Field access from ops uses dot-notation TagRefs --
    ``TagRef("axis.position")`` -- which the ST emitter and PLCopen
    XML emitter pass through as ``axis.position`` in IEC syntax.
    A future pass can introduce a dedicated ``FieldRef(base, field)``
    op for stronger typing.
    """
    name: str
    members: tuple["Var", ...] = ()
    comment: str = ""


@dataclass(frozen=True)
class ArrayType:
    """IEC 61131-3 §2.3.3.2 ARRAY type.

    A fixed-bounds, fixed-type sequence::

        TYPE Vector10 :
            ARRAY [0..9] OF INT;
        END_TYPE

        TYPE Matrix3x3 :
            ARRAY [0..2, 0..2] OF REAL;
        END_TYPE

    ``bounds`` is a tuple of ``(lo, hi)`` pairs -- one per dimension.
    IEC allows multi-dimensional arrays via the comma form above.

    Element access uses bracket-notation TagRefs --
    ``TagRef("buf[3]")`` or ``TagRef("matrix[1,2]")`` -- which the
    ST emitter passes through to IEC syntax.
    """
    name: str
    element_type: "DataType"
    bounds: tuple[tuple[int, int], ...]
    comment: str = ""


@dataclass(frozen=True)
class EnumType:
    """IEC 61131-3 §2.3.3.3 ENUM type.

    A named set of discrete values::

        TYPE Color :
            (RED, GREEN, BLUE);
        END_TYPE

    Each value is implicitly numbered by declaration order starting
    at 0.  PLCopen XML supports explicit values; this representation
    keeps them implicit until needed.
    """
    name: str
    values: tuple[str, ...]
    comment: str = ""


@dataclass(frozen=True)
class SubrangeType:
    """IEC 61131-3 §2.3.3.1 subrange type.

    A user-defined type that restricts an integer type's value range::

        TYPE SmallInt : INT (-100..100); END_TYPE
        TYPE Percent  : UINT (0..100);   END_TYPE
        TYPE Index    : USINT (0..15);   END_TYPE

    ``base`` is the underlying integer ``TagType`` (or NamedType
    pointing at one).  ``lower`` and ``upper`` bound the legal value
    range, inclusive.  PLCopen TC6 XML emits these as
    ``<subrangeSigned>`` or ``<subrangeUnsigned>`` depending on
    whether ``base`` is a signed or unsigned IEC integer type
    (see ``is_signed_subrange``).

    Subranges are conceptually aliases with range-restriction
    metadata -- backends that don't enforce the bounds at runtime
    may treat them as plain aliases for code-gen purposes; tooling
    can still use the bounds for validation.
    """
    name: str
    base: "DataType"
    lower: int
    upper: int
    comment: str = ""


@dataclass(frozen=True)
class AliasType:
    """IEC 61131-3 §2.3.3.4 simple / alias type.

    A renamed elementary or derived type::

        TYPE Distance : INT; END_TYPE
        TYPE Velocity : REAL; END_TYPE
        TYPE BigPoint : Point; END_TYPE     (* alias of a struct *)

    ``base`` is the underlying ``DataType`` -- elementary or
    NamedType.  Aliases give domain-meaningful names without changing
    runtime representation.
    """
    name: str
    base: "DataType"
    comment: str = ""


# -----------------------------------------------------------------------------
# Type unions
# -----------------------------------------------------------------------------


#: Union of every user-defined type variant.
UserType = Union[StructType, ArrayType, EnumType, SubrangeType, AliasType]


#: Anything that can appear as a variable's declared type:
#: an elementary ``TagType`` value, a ``NamedType`` reference to a
#: user-defined type, or a ``UserType`` instance inline (rare; usually
#: types are declared at program scope and referenced by name).
DataType = Union[TagType, NamedType, StructType, ArrayType, EnumType,
                 SubrangeType, AliasType]


#: The IEC signed-integer elementary types.  Used to discriminate
#: ``<subrangeSigned>`` vs ``<subrangeUnsigned>`` when emitting
#: PLCopen XML.
_SIGNED_INTEGER_TYPES = frozenset({
    TagType.SINT, TagType.INT, TagType.DINT, TagType.LINT,
})

#: The IEC unsigned-integer elementary types.
_UNSIGNED_INTEGER_TYPES = frozenset({
    TagType.USINT, TagType.UINT, TagType.UDINT, TagType.ULINT,
})


def is_signed_subrange(sub: SubrangeType) -> bool:
    """True iff the subrange's base is a signed IEC integer type.

    Resolves the base through one level of NamedType / AliasType when
    needed; backends call this to pick ``<subrangeSigned>`` vs
    ``<subrangeUnsigned>`` in PLCopen XML emission.
    """
    base = sub.base
    if isinstance(base, TagType):
        return base in _SIGNED_INTEGER_TYPES
    # NamedType references resolve at the Program level; default to
    # signed (the more permissive choice, matching IEC's INT default).
    return True


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def type_name(t: DataType) -> str:
    """Return the IEC textual name of a ``DataType``.

    For elementary types (``TagType``): the IEC keyword
    (``INT``, ``BOOL``, ``REAL``, ...).  For ``NamedType``: the
    referenced name.  For inline ``UserType`` instances: the type's
    own ``name`` attribute.  Used by every emitter when writing
    out type references.
    """
    if isinstance(t, TagType):
        return t.value
    if isinstance(t, NamedType):
        return t.name
    if isinstance(t, (StructType, ArrayType, EnumType,
                      SubrangeType, AliasType)):
        return t.name
    raise TypeError(f"not a DataType: {t!r}")


def is_user_type(t: DataType) -> bool:
    """True iff ``t`` references a user-defined type (named or inline)."""
    return isinstance(t, (NamedType, StructType, ArrayType,
                          EnumType, SubrangeType, AliasType))


def is_elementary(t: DataType) -> bool:
    """True iff ``t`` is one of the IEC §6.4 elementary types."""
    return isinstance(t, TagType)
