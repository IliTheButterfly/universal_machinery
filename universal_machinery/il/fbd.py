"""Function Block Diagram (FBD) body per IEC 61131-3 §6.7.

An FBD body is a directed graph of *elements* (function blocks,
variable connectors, jumps, labels, returns) connected by *wires*
that carry values from producer output pins to consumer input pins.
Semantically equivalent to a topologically-sorted list of
assignments / function calls; visually it's a network of named
boxes with wires between them.  Heavy use in Siemens (Step 7
FBD), Allen-Bradley (RSLogix FBD), and any IEC-conformant editor
that supports FBD source.

Model overview
--------------

We capture the same graph the PLCopen TC6 XSD captures, but with
vendor-neutral naming and *optional* position info::

    FbdNetwork
      ├── elements: list[FbdElement]
      └── comment: str

    FbdElement ::= FbBlock              -- function/FB call site
                 | InVariable           -- value producer (variable read / literal)
                 | OutVariable          -- value consumer (assignment target)
                 | InOutVariable        -- VAR_IN_OUT pattern (both)
                 | FbdLabel             -- jump target
                 | FbdJump              -- jump statement
                 | FbdReturn            -- early return

Every element carries a ``local_id: int`` that's unique within the
network.  Wires are stored *sink-side* (matching PLCopen): each
consumer pin has an optional ``Connection`` whose ``source_id``
points back at the producing element's ``local_id`` and (for blocks)
``source_pin`` names the producer's output formal parameter.

Position info
-------------

PLCopen XML requires an ``<position x= y=/>`` on every element.
We make positions *optional* on the IL side: vendor-neutral code
shouldn't need to think about pixel coordinates.  The PLCopen XML
emitter auto-lays-out elements that don't carry positions (simple
left-to-right, row-major sweep) so the output is XSD-valid; if a
backend wants to preserve authored layout it can supply positions
explicitly.

Body kind mutex
---------------

A POU body is now exactly one of ``rungs`` (LD), ``sfc`` (grafcet),
``st_body`` (ST AST), or ``fbd_body`` (FBD).  The validator enforces
the mutex.  Backends that don't speak FBD natively lower it to LD
or ST (deferred to a follow-up slice -- the topological-sort +
temp-variable-allocation pass).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


# -----------------------------------------------------------------------------
# Position
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """An (x, y) coordinate on the FBD canvas.

    Stored as floats to match the PLCopen ``xsd:decimal`` position
    type.  Origins / orientation follow PLCopen convention: +x is
    right, +y is down, top-left is (0, 0).
    """
    x: float
    y: float


# -----------------------------------------------------------------------------
# Wires (sink-side connection references)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    """One incoming wire on a sink pin.

    PLCopen stores connections on the *consumer* side: the input
    pin has a ``<connection refLocalId="..." formalParameter="..."/>``
    that points back at the producer.  We mirror that: ``source_id``
    names the producing element's ``local_id``, ``source_pin``
    names a formal-parameter output on a block (omitted for
    in-/out-/inOut-variable connectors, which have a single
    implicit output pin).
    """
    source_id: int
    source_pin: Optional[str] = None


# -----------------------------------------------------------------------------
# Block pins
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockPin:
    """One named input / output / inOut pin on an ``FbBlock``.

    ``formal_parameter`` is the IEC formal-parameter name on the
    block's interface (``IN1``, ``IN2``, ``PT``, ``CLK``, ``OUT``,
    ``Q``, ``ET``, ...).  For input and in_out pins, ``connection``
    optionally carries the incoming wire; for output pins,
    ``connection`` is always ``None`` (outputs produce, they don't
    consume).

    Modifier flags follow PLCopen attribute semantics:

      - ``negated``  : draws the small circle on the pin; logical
                       complement of the value crossing it
      - ``edge``     : ``"rising"`` / ``"falling"`` / ``""``
                       (the IEC edge-modifier; ``""`` = none)
      - ``storage``  : ``"set"`` / ``"reset"`` / ``""`` (the
                       latch / unlatch modifier on coil-like pins)
    """
    formal_parameter: str
    connection: Optional[Connection] = None
    negated: bool = False
    edge: str = ""
    storage: str = ""


# -----------------------------------------------------------------------------
# Elements
# -----------------------------------------------------------------------------


@dataclass
class FbBlock:
    """A function or function-block call site in the FBD network.

    ``type_name`` is the called POU's name (``"AND"``, ``"TON"``,
    ``"MyFB"``, ``"ADD"``, ...).  For stateful FB calls,
    ``instance_name`` names the per-instance state DataBlock (the
    name shown above the box in graphical editors); stateless
    function calls leave it ``None``.

    Pins are kept in three separate lists so the PLCopen XML
    emitter can map directly onto ``<inputVariables>`` /
    ``<inOutVariables>`` / ``<outputVariables>``.  Each pin's
    ``formal_parameter`` must match a declared parameter on the
    referenced POU; the validator checks this for IL-declared
    POUs and accepts anything for stdlib / vendor-extension
    blocks.
    """
    local_id: int
    type_name: str
    instance_name: Optional[str] = None
    inputs: list[BlockPin] = field(default_factory=list)
    outputs: list[BlockPin] = field(default_factory=list)
    in_outs: list[BlockPin] = field(default_factory=list)
    position: Optional[Position] = None
    execution_order: Optional[int] = None
    comment: str = ""


@dataclass
class InVariable:
    """A value-producer connector: variable read or literal.

    Renders as a small box on the left of the network with a
    single output pin.  ``expression`` is the IEC textual operand
    that PLCopen drops into ``<expression>`` -- typically a
    variable name (``"x"``, ``"axis.position"``) or a constant
    (``"3.14"``, ``"TRUE"``, ``"T#100ms"``).
    """
    local_id: int
    expression: str
    position: Optional[Position] = None
    execution_order: Optional[int] = None
    negated: bool = False
    edge: str = ""
    storage: str = ""
    comment: str = ""


@dataclass
class OutVariable:
    """A value-consumer connector: assignment target.

    Renders as a small box on the right with a single input pin.
    ``connection`` carries the incoming wire (the value to write);
    ``expression`` names the variable being assigned to.
    """
    local_id: int
    expression: str
    connection: Optional[Connection] = None
    position: Optional[Position] = None
    execution_order: Optional[int] = None
    negated: bool = False
    edge: str = ""
    storage: str = ""
    comment: str = ""


@dataclass
class InOutVariable:
    """A variable used as both producer and consumer.

    Maps to the VAR_IN_OUT pattern: the wire passes through the
    variable cell, modifying it.  Has both ``connection`` (input
    side) and an implicit single output pin (used by other
    elements that connect into this variable).  PLCopen carries
    separate negated/edge/storage modifiers per side via
    ``negatedIn`` / ``negatedOut`` / etc.
    """
    local_id: int
    expression: str
    connection: Optional[Connection] = None
    position: Optional[Position] = None
    execution_order: Optional[int] = None
    negated_in: bool = False
    negated_out: bool = False
    edge_in: str = ""
    edge_out: str = ""
    storage_in: str = ""
    storage_out: str = ""
    comment: str = ""


@dataclass
class FbdLabel:
    """Jump target: a named anchor for FbdJump/jumps from elsewhere."""
    local_id: int
    label: str
    position: Optional[Position] = None
    execution_order: Optional[int] = None
    comment: str = ""


@dataclass
class FbdJump:
    """Conditional jump to a labeled position.

    Fires when the wire on ``connection`` carries TRUE.  The label
    must be the name of an ``FbdLabel`` in the same network.
    """
    local_id: int
    label: str
    connection: Optional[Connection] = None
    position: Optional[Position] = None
    execution_order: Optional[int] = None
    comment: str = ""


@dataclass
class FbdReturn:
    """Early return from the enclosing POU.

    Fires when the wire on ``connection`` carries TRUE.  Equivalent
    to IEC ``RETURN;`` in ST, ``Return`` op in LD.
    """
    local_id: int
    connection: Optional[Connection] = None
    position: Optional[Position] = None
    execution_order: Optional[int] = None
    comment: str = ""


#: Union of every FBD element kind.
FbdElement = Union[
    FbBlock, InVariable, OutVariable, InOutVariable,
    FbdLabel, FbdJump, FbdReturn,
]


# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------


@dataclass
class FbdNetwork:
    """A complete FBD body: a list of elements with cross-references
    via ``local_id``.

    The network has no inherent execution order; the PLCopen schema
    optionally carries ``executionOrderId`` on each element, and
    backends interpret the topology to derive a runnable sequence.
    The validator checks structural consistency (unique IDs,
    resolved references, well-known pin names on declared POUs);
    semantic checks (cycle detection, type compatibility) are a
    separate pass.
    """
    elements: list[FbdElement] = field(default_factory=list)
    comment: str = ""

    def find(self, local_id: int) -> Optional[FbdElement]:
        for e in self.elements:
            if e.local_id == local_id:
                return e
        return None

    def find_label(self, name: str) -> Optional[FbdLabel]:
        for e in self.elements:
            if isinstance(e, FbdLabel) and e.label == name:
                return e
        return None

    def next_local_id(self) -> int:
        """Return one greater than the highest in-use ``local_id``,
        or 0 if the network is empty.  Useful when programmatically
        appending elements."""
        if not self.elements:
            return 0
        return max(e.local_id for e in self.elements) + 1
