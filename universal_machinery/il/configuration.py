"""CONFIGURATION / RESOURCE / TASK per IEC 61131-3 §2.7.

IEC's top-level system-organisation model: a ``Configuration`` is the
project's outermost executable container, holding one-or-more
``Resource``s (each typically maps to a single PLC CPU or runtime).
A ``Resource`` schedules POU instances via ``TaskSpec``s and may
declare its own global variables; the surrounding ``Configuration``
holds system-wide globals + access variables exposed to external
clients (HMI, OPC UA, fieldbus).

Conceptual mapping
------------------

    CONFIGURATION = the overall project
    RESOURCE      = one PLC / runtime (multi-resource = multi-PLC)
    TASK          = a scheduling rule (cyclic / single / interrupt)
    POU instance  = a runtime instance of a PROGRAM bound to a TASK

This pairs with the project-vision multi-PLC arc.  Today most
programs the IL handles are single-resource, single-task -- the
``Configuration`` model still works (one Configuration with one
Resource), but the structure is in place for the multi-PLC future.

PLCopen TC6 mapping
-------------------

    <instances>
      <configurations>
        <configuration name="Default">
          <resource name="Resource1">
            <task name="..." interval="..." priority="..."/>
            <pouInstance name="..." typeName="..." taskName="..."/>
            <globalVars>...</globalVars>
          </resource>
          <globalVars>...</globalVars>
          <accessVars>...</accessVars>
        </configuration>
      </configurations>
    </instances>
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .ast import Var


# -----------------------------------------------------------------------------
# Task scheduling
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSpec:
    """IEC §2.7.2 task scheduling specification.

    Tasks have one of three triggering modes (mutually exclusive):

      - **Cyclic**: ``interval`` set to an IEC TIME literal like
        ``"T#100ms"``; task runs every interval.
      - **Single-shot**: ``single`` set to a boolean event name
        (typically a memory bit); task runs once per rising edge.
      - **Interrupt**: ``interrupt`` set to a hardware-event name;
        task runs on the named interrupt.

    All three modes require ``priority`` (lower number = higher
    priority).  Cyclic is the default and by far the most common.
    """
    name: str
    priority: int = 1
    interval: Optional[str] = None       # e.g. "T#100ms"
    single: Optional[str] = None         # event bit name
    interrupt: Optional[str] = None      # interrupt source name
    comment: str = ""


@dataclass(frozen=True)
class PouInstance:
    """A runtime instance of a POU bound to a Task.

    ``name`` is the instance identifier (e.g. ``MainProgram1``).
    ``type_name`` is the POU's declared name (matches a Subroutine
    in ``Program.subroutines``).  ``task`` is the name of the
    ``TaskSpec`` that schedules this instance; ``None`` means the
    instance runs in the resource's default task slot.
    """
    name: str
    type_name: str
    task: Optional[str] = None
    comment: str = ""


# -----------------------------------------------------------------------------
# Resource
# -----------------------------------------------------------------------------


@dataclass
class Resource:
    """IEC §2.7.1 RESOURCE -- one PLC CPU / runtime.

    A resource owns the runtime instances of POUs (``pou_instances``)
    plus the tasks that schedule them.  Resource-scoped globals are
    held in ``global_vars``; they're visible to the POUs running
    inside this resource but not to other resources in the same
    Configuration.
    """
    name: str
    tasks: list[TaskSpec] = field(default_factory=list)
    pou_instances: list[PouInstance] = field(default_factory=list)
    global_vars: list["Var"] = field(default_factory=list)
    comment: str = ""

    def find_task(self, name: str) -> Optional[TaskSpec]:
        for t in self.tasks:
            if t.name == name:
                return t
        return None

    def find_pou_instance(self, name: str) -> Optional[PouInstance]:
        for inst in self.pou_instances:
            if inst.name == name:
                return inst
        return None


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


@dataclass
class Configuration:
    """IEC §2.7 CONFIGURATION -- top-level system organisation.

    Holds one or more resources, system-wide global variables, and
    access variables exposed to external clients (HMI, OPC UA,
    fieldbus).  A program typically has exactly one configuration;
    multi-PLC projects use the same configuration with multiple
    resources.

    Access variables (``access_vars``) describe published symbols
    with an access path -- e.g. an HMI tag that reads a specific
    POU instance variable.  Modeled as ``Var``s for uniformity;
    the writer-time emitter renders them in the PLCopen XML
    ``<accessVars>`` element.
    """
    name: str
    resources: list[Resource] = field(default_factory=list)
    global_vars: list["Var"] = field(default_factory=list)
    access_vars: list["Var"] = field(default_factory=list)
    comment: str = ""

    def find_resource(self, name: str) -> Optional[Resource]:
        for r in self.resources:
            if r.name == name:
                return r
        return None
