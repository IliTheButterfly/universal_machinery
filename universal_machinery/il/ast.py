"""Vendor-neutral PLC program AST.

The structure mirrors IEC 61131-3 Part 3 (Programmable Languages):

  Program
    ├── DataBlock[]                  (typed global memory aggregates)
    └── Subroutine[]                 (a "POU" -- program organization unit)
          ├── inputs / outputs / in_outs / local_vars  (interface, IEC §2.4.3)
          └── body: Rung[]            (LD/IL)
                └── Op[]              (instructions; see il/ops.py)
              OR  SfcNetwork          (grafcet, see il/sfc.py)

A ``Tag`` is a named symbolic reference to a memory address; an
``Address`` is the raw vendor-style location (e.g., "X001", "Y001",
"DS20").  Tags hold display nicknames, data types, and initial values;
addresses are what the PLC actually reads/writes.

POUs come in four kinds: PROGRAM, FUNCTION, FUNCTION_BLOCK, and the
vendor-native SUBROUTINE kind (e.g. CLICK Call) which has no formal
interface.  FUNCTION_BLOCK instances carry per-instance state in a
``DataBlock``; FUNCTIONs are stateless and may return a single value.

Backends are responsible for mapping IL data types onto whatever the
target vendor uses (CLICK uses 0x6065 for discrete contacts, 0x6074
for 16-bit register sources, etc.; OpenPLC uses IEC types directly).
See ``docs/click_calling_convention.md`` for how parameterized POUs,
FB instances, and nested calls lower onto CLICK -- a target that has
neither function parameters nor nested CALL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .sfc import SfcNetwork
    from .st import Statement


# -----------------------------------------------------------------------------
# Memory addresses and tags
# -----------------------------------------------------------------------------


class TagType(Enum):
    """Data types per IEC 61131-3 Part 3 §6.4.

    Backends translate to vendor-native type identifiers.  CLICK's
    DISCRETE maps to BOOL; CLICK's SIGNED_INT_16 maps to INT, etc.
    """

    BOOL       = "BOOL"          # 1-bit discrete
    BYTE       = "BYTE"          # 8-bit, treated as bit string
    WORD       = "WORD"          # 16-bit bit string
    DWORD      = "DWORD"         # 32-bit bit string
    SINT       = "SINT"          # signed 8-bit
    INT        = "INT"           # signed 16-bit
    DINT       = "DINT"          # signed 32-bit
    LINT       = "LINT"          # signed 64-bit
    USINT      = "USINT"         # unsigned 8-bit
    UINT       = "UINT"          # unsigned 16-bit
    UDINT      = "UDINT"         # unsigned 32-bit
    ULINT      = "ULINT"         # unsigned 64-bit
    REAL       = "REAL"          # 32-bit float
    LREAL      = "LREAL"         # 64-bit float
    TIME       = "TIME"          # time duration
    STRING     = "STRING"        # variable-length string


@dataclass(frozen=True)
class Address:
    """A vendor-style memory address (e.g. 'X001', 'Y005', 'DS20', 'C100').

    The string is kept verbatim; backends parse and validate against
    their address-space conventions on lower.
    """

    raw: str

    def __str__(self) -> str:
        return self.raw


@dataclass(frozen=True)
class Tag:
    """A named, typed symbol declaration -- vendor-neutral.

    A ``Tag`` declares an addressable symbol by ``name`` + ``data_type``;
    the ``description`` is free-form documentation that backends export
    alongside the symbol (CLICK's nickname comment, IEC variable
    comment, etc.).

    Static vs. dynamic
    ------------------
    Tags come in two flavours:

      - **Static** (``address`` is set): the backend must honour the
        declared address verbatim.  Use for:
            * physical I/O points (X / Y / vendor-specific slot addrs)
            * HMI-referenced tags (HMI projects pin specific addresses;
              regenerating PLC code must not move them)
            * tags exposed on Modbus / EtherNet-IP / OPC UA

      - **Dynamic** (``address`` is ``None``): the backend's tag
        allocator picks a free address.  Use for internal scratch
        values whose location is irrelevant outside the program.

    Rung ops reference a tag symbolically via ``TagRef("name")``; a
    resolver pass run by the backend writer substitutes each TagRef
    with its bound ``Address`` before emitting the file.

    Multi-PLC scope
    ---------------
    The static/dynamic distinction is currently per-Program (one PLC).
    In a future multi-PLC ``Project`` model, scope becomes per-PLC:
    the same tag may be pinned on the device that owns it and dynamic
    elsewhere.  Out of scope until we add a second PLC backend; see
    the corresponding Open Question in ``ARCHITECTURE.md``.
    """

    name: str
    data_type: TagType = TagType.BOOL
    description: str = ""
    address: Optional[Address] = None


@dataclass(frozen=True)
class TagRef:
    """A symbolic reference to a ``Tag`` from inside a rung op.

    A ``TagRef`` carries only the tag's ``name``; backends resolve it
    to an ``Address`` at lower time by looking the name up in
    ``Program.tags``.  Anywhere a rung op accepts ``Address``, it also
    accepts ``TagRef`` -- the two are interchangeable at construction
    time, and a resolver pass downstream rewrites ``TagRef``s into
    ``Address``es once allocation has picked concrete slots.

    Rationale: address-only programs continue to round-trip (the
    backend never sees a ``TagRef``), while name-first programs
    gain the ergonomics of symbolic references without forcing the
    user to pick addresses up front.
    """

    name: str


# -----------------------------------------------------------------------------
# POU interface: parameters, locals, and data blocks
# -----------------------------------------------------------------------------


class PouKind(Enum):
    """IEC 61131-3 Part 3 §2.2 POU classification.

    PROGRAM         Top-level executable; has parameters but no return.
    FUNCTION        Stateless callable; returns a single value (``return_type``).
    FUNCTION_BLOCK  Instance-based callable with internal state (held in
                    an instance ``DataBlock``); no return, multiple outputs.
    SUBROUTINE      Vendor-native unparameterized routine (CLICK's only
                    callable form).  Kept as the legacy default so AST
                    constructors that predate the POU split still work.
    """

    PROGRAM         = "PROGRAM"
    FUNCTION        = "FUNCTION"
    FUNCTION_BLOCK  = "FUNCTION_BLOCK"
    SUBROUTINE      = "SUBROUTINE"


class VarDirection(Enum):
    """How a variable participates in its POU's interface (IEC §2.4.3)."""

    INPUT     = "VAR_INPUT"        # read-only formal parameter
    OUTPUT    = "VAR_OUTPUT"       # written formal parameter
    IN_OUT    = "VAR_IN_OUT"       # passed by reference
    LOCAL     = "VAR"              # internal to the POU
    EXTERNAL  = "VAR_EXTERNAL"     # alias for a global / DataBlock member
    TEMP      = "VAR_TEMP"         # scratch, not persisted across scans


@dataclass(frozen=True)
class Var:
    """A typed variable declaration within a POU or DataBlock.

    Each backend translates a ``Var`` to its own storage:

      - CLICK lowering binds VAR_INPUT/OUTPUT to per-POU reserved DS
        register slots (see ``docs/click_calling_convention.md``).
      - IEC ST emits a ``<direction> <name> : <type>;`` declaration.

    ``address`` is normally left ``None`` so the backend picks a slot;
    set it explicitly when the variable must occupy a specific address
    (e.g. when re-exporting an existing CLICK project's symbols).
    """

    name: str
    # ``data_type`` is a ``DataType`` (elementary ``TagType`` OR a
    # reference / inline definition of a user-defined type from
    # ``il/types.py``).  Annotated as ``TagType`` here for back-compat
    # with existing code that imports only TagType; user-defined-type
    # values are accepted at runtime via the union.
    data_type: TagType = TagType.INT
    direction: VarDirection = VarDirection.LOCAL
    initial_value: str = ""
    address: Optional[Address] = None
    comment: str = ""


@dataclass
class DataBlock:
    """A named, typed collection of memory locations.

    Conceptually equivalent to:

      - Siemens S7 global DB (``DB1``) or instance DB (``DB10 of FB5``)
      - IEC global VAR block declared in a configuration
      - a C/C++ struct allocated at module scope

    A DataBlock owns NAMED MEMBERS, each a ``Var``.  Member addresses
    are either set explicitly (the backend honours them verbatim) or
    left ``None`` (the backend allocates them at lower time -- e.g.
    CLICK lays them out contiguously starting at ``base_address``).

    Instance DBs
    ------------
    When ``fb_template`` is set, the DataBlock is an "instance DB":
    it holds the state for a single instance of the named
    ``FUNCTION_BLOCK``.  Call sites pass the DB's name (or
    ``base_address``) in the ``instance`` field of ``Call``; the
    callee's VARs resolve relative to that instance.
    """

    name: str
    members: list[Var] = field(default_factory=list)
    base_address: Optional[Address] = None
    fb_template: Optional[str] = None
    comment: str = ""

    def find(self, member_name: str) -> Optional[Var]:
        for m in self.members:
            if m.name == member_name:
                return m
        return None


# -----------------------------------------------------------------------------
# Program organization
# -----------------------------------------------------------------------------


@dataclass
class Rung:
    """One horizontal logic row in Ladder Diagram.

    The ``ops`` list is read left-to-right: contacts in series form an
    AND; multiple contacts in parallel form an OR (represented via the
    ``ParallelGroup`` op below).  The right-most op is the rung's output
    -- typically a coil, function-block, or jump.
    """

    ops: list[object]              # list[Op]; using object to break import cycle
    comment: str = ""

    def __len__(self) -> int:
        return len(self.ops)


@dataclass
class Subroutine:
    """A Program Organization Unit (POU): a named, callable routine.

    Per IEC 61131-3 §2.2 a POU is one of PROGRAM / FUNCTION /
    FUNCTION_BLOCK; ``kind`` selects which (default SUBROUTINE for
    the vendor-native unparameterized form).  The ``main`` flag marks
    the entry point -- most projects have exactly one main routine;
    subordinate ones are reached via Call ops.

    Interface
    ---------
    ``inputs`` / ``outputs`` / ``in_outs`` are the formal parameters
    (VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT); ``local_vars`` are scratch
    declarations.  All four are empty for legacy SUBROUTINE POUs.
    For a FUNCTION, ``return_type`` is set and the implicit return
    value lives in the first VAR_OUTPUT slot.  For a FUNCTION_BLOCK,
    each call site supplies an instance ``DataBlock`` -- internal
    ``local_vars`` resolve against the active instance.

    Body
    ----
    A POU's body is exactly one of:

      - ``rungs``    -- Ladder Diagram / IL (list of ``Rung``)
      - ``sfc``      -- Sequential Function Chart (``SfcNetwork``)
      - ``st_body``  -- Structured Text (list of ``Statement``)

    The three are mutually exclusive.  Backends that don't speak a
    given body kind either lower it to one they do speak (ST -> LD
    via rung synthesis, SFC -> LD via step-bit assignment) or refuse
    with an ``Unsupported`` error.
    """

    name: str
    rungs: list[Rung] = field(default_factory=list)
    main: bool = False
    comment: str = ""
    kind: PouKind = PouKind.SUBROUTINE
    inputs:     list[Var] = field(default_factory=list)
    outputs:    list[Var] = field(default_factory=list)
    in_outs:    list[Var] = field(default_factory=list)
    local_vars: list[Var] = field(default_factory=list)
    return_type: Optional[TagType] = None
    sfc: Optional["SfcNetwork"] = None
    st_body: Optional[list["Statement"]] = None
    # IEC 61131-3 3rd-edition OOP additions (apply to FUNCTION_BLOCK
    # POUs only; ignored for other kinds).
    methods:    list[object] = field(default_factory=list)
    extends:    Optional[str] = None
    implements: list[str] = field(default_factory=list)
    abstract:   bool = False
    # ``methods`` is annotated list[object] to keep ``il.ast`` free of
    # a circular import on ``il.oop``; the runtime element type is
    # ``il.oop.Method``.

    def append(self, rung: Rung) -> None:
        self.rungs.append(rung)

    def find_method(self, name: str):
        """Look up a Method by name on a FUNCTION_BLOCK POU."""
        for m in self.methods:
            if getattr(m, "name", None) == name:
                return m
        return None

    def find_var(self, name: str) -> Optional[Var]:
        for bucket in (self.inputs, self.outputs, self.in_outs, self.local_vars):
            for v in bucket:
                if v.name == name:
                    return v
        return None


@dataclass
class Program:
    """The full PLC program: POUs, data blocks, user-defined types,
    and the tag table.

    ``tags`` is the symbol table keyed by ``Tag.name``.  Tags may be
    static (``Tag.address`` set, the backend honours it verbatim) or
    dynamic (``Tag.address`` None, the backend's allocator picks).
    Rung ops reference tags symbolically via ``TagRef("name")``; a
    resolver pass run by the backend writer substitutes each TagRef
    with its bound ``Address`` before emitting.

    ``data_blocks`` holds typed memory aggregates (Siemens-style DBs /
    IEC global VAR blocks).  Each FUNCTION_BLOCK instance lives in its
    own ``DataBlock`` with ``fb_template`` set to the FB's name.

    ``user_types`` is the program-level user-defined-type declaration
    table -- IEC's ``TYPE ... END_TYPE`` block.  Holds ``StructType``,
    ``ArrayType``, ``EnumType``, and ``AliasType`` declarations; ``Var``
    instances and other ``UserType`` definitions reference them by name
    via ``NamedType``.  See ``universal_machinery.il.types`` for the
    type variants.
    """

    subroutines: list[Subroutine] = field(default_factory=list)
    tags: dict[str, Tag] = field(default_factory=dict)
    data_blocks: list[DataBlock] = field(default_factory=list)
    user_types: list[object] = field(default_factory=list)
    configurations: list[object] = field(default_factory=list)
    interfaces: list[object] = field(default_factory=list)
    # ``user_types`` / ``configurations`` / ``interfaces`` are
    # annotated ``list[object]`` to avoid circular imports (the
    # ``types`` / ``configuration`` / ``oop`` modules import from
    # this one).  Runtime element types are ``il.types.UserType``,
    # ``il.configuration.Configuration``, and ``il.oop.Interface``.

    # Optional metadata that some backends consume
    cpu_model: str = ""            # e.g. "C2-01CPU" for CLICK
    project_name: str = ""
    comment: str = ""

    # ----- Lookups -----

    def find_subroutine(self, name: str) -> Optional[Subroutine]:
        for s in self.subroutines:
            if s.name == name:
                return s
        return None

    def main_subroutine(self) -> Optional[Subroutine]:
        for s in self.subroutines:
            if s.main:
                return s
        return None

    def find_data_block(self, name: str) -> Optional[DataBlock]:
        for db in self.data_blocks:
            if db.name == name:
                return db
        return None

    def find_user_type(self, name: str):
        """Look up a user-defined type by name.

        Returns the matching ``StructType`` / ``ArrayType`` /
        ``EnumType`` / ``AliasType``, or ``None`` if no UDT with that
        name has been declared.  Used by the resolver pass that
        validates ``NamedType`` references.
        """
        for ut in self.user_types:
            if getattr(ut, "name", None) == name:
                return ut
        return None

    def find_configuration(self, name: str):
        """Look up a Configuration by name."""
        for cfg in self.configurations:
            if getattr(cfg, "name", None) == name:
                return cfg
        return None

    def find_interface(self, name: str):
        """Look up an Interface by name (IEC 61131-3 3rd ed. §2.5.1.5)."""
        for iface in self.interfaces:
            if getattr(iface, "name", None) == name:
                return iface
        return None

    def find_tag(self, name: str) -> Optional[Tag]:
        return self.tags.get(name)

    def static_tags(self) -> list[Tag]:
        """Tags with a pinned address (must not move on regeneration)."""
        return [t for t in self.tags.values() if t.address is not None]

    def dynamic_tags(self) -> list[Tag]:
        """Tags whose address the allocator may choose."""
        return [t for t in self.tags.values() if t.address is None]

    def fb_instances_of(self, fb_name: str) -> list[DataBlock]:
        """All instance DBs that belong to the named FUNCTION_BLOCK."""
        return [db for db in self.data_blocks if db.fb_template == fb_name]

    def referenced_addresses(self) -> set[Address]:
        """Walk every rung (or SFC transition) and collect Addresses used.

        ``TagRef``s are not included -- they're unresolved symbolic
        references.  Use ``referenced_tags`` for those.
        """
        from . import ops as _ops
        out: set[Address] = set()
        for sub in self.subroutines:
            for rung in sub.rungs:
                for op in rung.ops:
                    out.update(_ops.addresses_of(op))
            if sub.sfc is not None:
                for tr in sub.sfc.transitions:
                    for op in tr.condition:
                        out.update(_ops.addresses_of(op))
                for st in sub.sfc.steps:
                    for act in st.actions:
                        if isinstance(act.target, Address):
                            out.add(act.target)
        return out

    def referenced_tags(self) -> set[str]:
        """Walk every rung (or SFC transition) and collect TagRef names.

        Backends use this to verify that every symbolic reference has a
        matching ``Tag`` declaration in ``self.tags`` before running the
        TagRef → Address resolver.
        """
        from . import ops as _ops
        out: set[str] = set()
        for sub in self.subroutines:
            for rung in sub.rungs:
                for op in rung.ops:
                    out.update(_ops.tags_of(op))
            if sub.sfc is not None:
                for tr in sub.sfc.transitions:
                    for op in tr.condition:
                        out.update(_ops.tags_of(op))
                for st in sub.sfc.steps:
                    for act in st.actions:
                        if isinstance(act.target, TagRef):
                            out.add(act.target.name)
        return out
