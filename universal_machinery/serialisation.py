"""JSON serialisation for the IL.

A round-trip JSON encoder / decoder for ``Program``.  Produces a
schema-versioned, self-describing dict that mirrors the IL's
dataclass tree; the reverse pass reconstructs the original
dataclasses byte-for-byte from the dict.

Why
---

  - **Git-diffable IL.**  Raw Python dataclasses round-trip through
    pickle but don't diff cleanly.  JSON is human-readable and plays
    well with ``git diff`` / ``git merge``.
  - **AI-agent I/O.**  Agents producing or consuming IL programs
    can read/write JSON without touching Python directly.  Pairs
    with the project's agent-built-backends arc.
  - **CI artifacts.**  Pre-lowering + post-lowering IL can be
    snapshotted as JSON and asserted byte-equal across builds.

Schema
------

  - Top-level: ``{"_schema": 1, "_type": "Program", ...fields}``
  - Every dataclass: ``{"_type": "ClassName", ...fields}``
  - Every enum: ``{"_type": "EnumClassName", "_value": "MEMBER_NAME"}``
  - Tuples in source dataclasses round-trip through JSON arrays and
    are coerced back to tuples on decode (frozen-dataclass hashability
    depends on this).
  - ``None`` -> JSON null; lists / dicts / primitives pass through.

Schema version 1 covers every IL primitive and emitter-relevant
dataclass: Program, Subroutine, Var, Tag, TagRef, Address,
DataBlock, every UserType variant, every Op variant (including
VendorOp + the IEC stdlib FBs), SFC types, Configuration /
Resource / TaskSpec / PouInstance.  Adding a new IL dataclass
means registering it in ``_DATACLASSES``; adding a new enum means
``_ENUMS``.
"""
from __future__ import annotations

import dataclasses
import json
import typing
from enum import Enum
from functools import lru_cache
from typing import Any, Union, get_args, get_origin

from . import il
from .il import (
    Action, Address, AliasType, ArrayType, Configuration, DataBlock,
    EnumType, NamedType, PouInstance, PouKind, Program, Resource, Rung,
    SfcNetwork, Step, StructType, SubrangeType, Subroutine, Tag, TagRef,
    TagType, TaskSpec, Transition, Var, VarDirection, VendorOp,
)
from .il.ops import (
    BinaryMath, Call, Compare, ContactFallingEdge, ContactNC, ContactNO,
    ContactRisingEdge, CTD, CTU, CTUD, End, FTrig, Jump, Label, Move,
    OutCoil, OutReset, OutSet, ParallelGroup, RS, RTrig, Return, SR,
    StdFunc, TOF, TON, TP,
)


#: Bump when the serialised shape gains an incompatible change.
#: A future migration layer can branch on this to read older files.
SCHEMA_VERSION = 1


# -----------------------------------------------------------------------------
# Type registry
# -----------------------------------------------------------------------------


#: All dataclass types the serializer recognises by name.  Adding a
#: new IL dataclass means adding it here.
_DATACLASSES: dict[str, type] = {
    cls.__name__: cls for cls in (
        # AST scalars
        Address, TagRef, Tag, Var,
        # Program organization
        Program, Subroutine, Rung, DataBlock,
        # User-defined types
        StructType, ArrayType, EnumType, SubrangeType, AliasType, NamedType,
        # Configuration model
        Configuration, Resource, TaskSpec, PouInstance,
        # SFC
        SfcNetwork, Step, Transition, Action,
        # Ops
        ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge,
        OutCoil, OutSet, OutReset,
        TON, TOF, TP, CTU, CTD, CTUD,
        RTrig, FTrig, SR, RS,
        StdFunc, Compare, Move, BinaryMath,
        Call, Return, End, Jump, Label,
        ParallelGroup, VendorOp,
    )
}


#: All enum types the serializer recognises.
_ENUMS: dict[str, type[Enum]] = {
    cls.__name__: cls for cls in (TagType, PouKind, VarDirection)
}


class SerialisationError(Exception):
    """Raised when an unsupported value is encountered during
    encode/decode -- e.g. an object that's neither a recognised
    dataclass nor a JSON-compatible primitive."""


# -----------------------------------------------------------------------------
# Encode: IL -> dict
# -----------------------------------------------------------------------------


def to_dict(obj: Any) -> Any:
    """Convert an IL value (Program or any inner dataclass) to a
    JSON-compatible nested dict / list / primitive structure.

    Top-level callers typically pass a ``Program``; the result has a
    ``_schema`` field so the decoder can fail fast on version skew.
    Nested calls (inner dataclasses / collections) omit ``_schema``.
    """
    encoded = _encode(obj)
    if isinstance(obj, Program):
        # Stamp the schema version at the top level only.
        encoded = {"_schema": SCHEMA_VERSION, **encoded}
    return encoded


def _encode(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Enum):
        return {"_type": type(obj).__name__, "_value": obj.name}
    if isinstance(obj, (list, tuple)):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj):
        out: dict[str, Any] = {"_type": type(obj).__name__}
        for f in dataclasses.fields(obj):
            out[f.name] = _encode(getattr(obj, f.name))
        return out
    raise SerialisationError(
        f"don't know how to serialise {type(obj).__name__}: {obj!r}"
    )


# -----------------------------------------------------------------------------
# Decode: dict -> IL
# -----------------------------------------------------------------------------


def from_dict(d: Any) -> Any:
    """Reverse of ``to_dict``: reconstruct the original IL value tree
    from a JSON-compatible dict.

    The top-level decode checks the schema version; a mismatch raises
    ``SerialisationError``.  Nested decodes recurse without re-checking
    -- the schema is a document-level property.
    """
    if isinstance(d, dict) and "_schema" in d:
        version = d["_schema"]
        if version != SCHEMA_VERSION:
            raise SerialisationError(
                f"unsupported schema version: {version} "
                f"(this implementation reads version {SCHEMA_VERSION})"
            )
        d = {k: v for k, v in d.items() if k != "_schema"}
    return _decode(d)


def _decode(d: Any) -> Any:
    if d is None:
        return None
    if isinstance(d, (bool, int, float, str)):
        return d
    if isinstance(d, list):
        return [_decode(x) for x in d]
    if not isinstance(d, dict):
        raise SerialisationError(f"unexpected JSON value: {d!r}")

    type_name = d.get("_type")
    if type_name is None:
        # Plain dict (e.g. Program.tags is dict[str, Tag]) -- decode values.
        return {k: _decode(v) for k, v in d.items()}

    # Tagged enum
    if type_name in _ENUMS:
        enum_cls = _ENUMS[type_name]
        return enum_cls[d["_value"]]

    # Tagged dataclass
    if type_name in _DATACLASSES:
        cls = _DATACLASSES[type_name]
        return _reconstruct_dataclass(cls, d)

    raise SerialisationError(f"unknown _type: {type_name!r}")


@lru_cache(maxsize=64)
def _resolved_hints(cls: type) -> dict[str, Any]:
    """Cached ``typing.get_type_hints`` for one dataclass.

    Resolves the string annotations created by
    ``from __future__ import annotations`` into real type objects
    (``tuple[...]``, ``Optional[X]``, etc.) so ``get_origin`` /
    ``get_args`` work.  We need this because ``dataclasses.fields(cls)``
    returns ``f.type`` as the *string* form when forward references are
    in play.

    The IL types module isn't always visible from this module's scope
    at hint-resolution time, so we pass an explicit globalns / localns
    populated with the IL public API to handle forward refs like
    ``"SfcNetwork"`` and ``"DataType"``.
    """
    ns = {**vars(il), **vars(il.ops), **vars(il.sfc), **vars(il.types),
          **vars(il.configuration)}
    return typing.get_type_hints(cls, globalns=ns, localns=ns)


def _reconstruct_dataclass(cls: type, d: dict) -> Any:
    """Build an instance of ``cls`` from a decoded ``{_type, ...}``
    dict.  Field values are decoded recursively, then coerced to
    tuple where the field's declared type is tuple-typed.

    Tuple coercion matters because:
      - JSON has no tuple type; tuples round-trip as JSON arrays
      - Frozen dataclasses with tuple fields rely on tuple
        hashability (set / dict-key membership)
      - Equality between two reconstructed instances would fail
        if one had list and the other had tuple in the same slot
    """
    try:
        hints = _resolved_hints(cls)
    except Exception:
        # Best-effort: if hints can't be resolved (e.g. forward ref
        # we don't know about), fall back to no-coercion.
        hints = {}
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in d:
            continue
        decoded = _decode(d[f.name])
        type_hint = hints.get(f.name, f.type)
        kwargs[f.name] = _coerce_tuples(decoded, type_hint)
    return cls(**kwargs)


def _coerce_tuples(value: Any, type_hint: Any) -> Any:
    """If ``type_hint`` declares a tuple, coerce ``value`` (a list
    after JSON round-trip) to a tuple, recursing into the element
    type so ``tuple[tuple[int, int], ...]`` works correctly."""
    if value is None:
        return None
    # ``type_hint`` may be a string (PEP 563 deferred eval), a generic
    # form, or a plain class.  Try to discriminate.
    origin = get_origin(type_hint)
    args = get_args(type_hint)

    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            return value
        # tuple[X, ...] -> homogeneous; tuple[X, Y, Z] -> heterogeneous
        if len(args) == 2 and args[1] is Ellipsis:
            inner = args[0]
            return tuple(_coerce_tuples(v, inner) for v in value)
        # Fixed-arity tuple
        if args:
            return tuple(_coerce_tuples(v, a) for v, a in zip(value, args))
        return tuple(value)

    if origin is Union:
        # Optional[X] = Union[X, None] etc.  Try each non-None arg.
        for a in args:
            if a is type(None):
                continue
            coerced = _coerce_tuples(value, a)
            # If the coercion produced a tuple, prefer that result.
            if isinstance(coerced, tuple):
                return coerced
        return value

    if origin is list:
        if not isinstance(value, list):
            return value
        return [_coerce_tuples(v, args[0]) for v in value] if args else value

    if origin is dict:
        if not isinstance(value, dict):
            return value
        # Dict keys are strings in our IL; values may need coercion.
        v_type = args[1] if len(args) >= 2 else Any
        return {k: _coerce_tuples(v, v_type) for k, v in value.items()}

    return value


# -----------------------------------------------------------------------------
# Convenience: to/from JSON string
# -----------------------------------------------------------------------------


def to_json(obj: Any, *, indent: int = 2, sort_keys: bool = False) -> str:
    """Encode an IL value as a JSON string.

    ``indent=2`` is the default; pass ``indent=None`` for compact
    single-line output.  ``sort_keys=True`` gives a deterministic
    key order (useful for diff stability across emit timestamps).
    """
    return json.dumps(to_dict(obj), indent=indent, sort_keys=sort_keys)


def from_json(s: str) -> Any:
    """Decode a JSON string back into an IL value (typically a Program)."""
    return from_dict(json.loads(s))
