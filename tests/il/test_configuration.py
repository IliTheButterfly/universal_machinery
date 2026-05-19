"""Tests for IEC 61131-3 §2.7 Configuration / Resource / Task model.

Covers the IL dataclasses and the matching builder DSL helpers.
The emitters (ST, PLCopen XML) are exercised in
``tests/emitters/`` in follow-up commits.
"""
import pytest

from universal_machinery.builders import (
    configuration, pou_instance, program, resource, task_spec, var,
)
from universal_machinery.il import (
    Configuration, PouInstance, Program, Resource, TagType, TaskSpec, Var,
)


# -----------------------------------------------------------------------------
# TaskSpec
# -----------------------------------------------------------------------------


def test_task_spec_cyclic():
    t = task_spec("Fast", priority=1, interval="T#10ms")
    assert t == TaskSpec(name="Fast", priority=1, interval="T#10ms")
    assert t.single is None and t.interrupt is None


def test_task_spec_single_shot():
    t = task_spec("OnButton", priority=1, single="start_btn")
    assert t.single == "start_btn"
    assert t.interval is None


def test_task_spec_interrupt_driven():
    t = task_spec("Counter", priority=2, interrupt="INT_TIMER0")
    assert t.interrupt == "INT_TIMER0"


def test_task_spec_priority_default():
    """Priority defaults to 1 (highest)."""
    t = task_spec("Default", interval="T#1s")
    assert t.priority == 1


def test_task_spec_is_frozen():
    import dataclasses
    t = task_spec("F", interval="T#10ms")
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.name = "G"     # type: ignore[misc]


# -----------------------------------------------------------------------------
# PouInstance
# -----------------------------------------------------------------------------


def test_pou_instance_construction():
    inst = pou_instance("MainProg1", type_name="Main", task="Fast")
    assert inst == PouInstance(
        name="MainProg1", type_name="Main", task="Fast",
    )


def test_pou_instance_without_task():
    """An instance with no task is unscheduled (resource's default)."""
    inst = pou_instance("Helper", type_name="Helper")
    assert inst.task is None


# -----------------------------------------------------------------------------
# Resource
# -----------------------------------------------------------------------------


def test_resource_with_tasks_and_instances():
    r = resource(
        "CPU1",
        tasks=[
            task_spec("Fast", priority=1, interval="T#10ms"),
            task_spec("Slow", priority=2, interval="T#100ms"),
        ],
        pou_instances=[
            pou_instance("Main1", type_name="Main", task="Fast"),
            pou_instance("Diag1", type_name="Diagnostics", task="Slow"),
        ],
        global_vars=[var("counter", TagType.INT)],
    )
    assert isinstance(r, Resource)
    assert r.find_task("Fast") is not None
    assert r.find_task("Fast").interval == "T#10ms"
    assert r.find_task("Ghost") is None
    assert r.find_pou_instance("Main1").type_name == "Main"
    assert r.find_pou_instance("Ghost") is None
    assert len(r.global_vars) == 1


def test_resource_defaults_empty():
    r = resource("CPU1")
    assert r.tasks == []
    assert r.pou_instances == []
    assert r.global_vars == []


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


def test_configuration_with_single_resource():
    cfg = configuration("Default", resources=[resource("CPU1")])
    assert cfg.find_resource("CPU1") is not None
    assert cfg.find_resource("Ghost") is None


def test_configuration_with_multiple_resources():
    """Multi-PLC: multiple resources inside one configuration."""
    cfg = configuration("Cell",
                        resources=[resource("PLC_A"), resource("PLC_B")])
    assert {r.name for r in cfg.resources} == {"PLC_A", "PLC_B"}


def test_configuration_global_vars_distinct_from_resource_global_vars():
    """Configuration-scope globals are system-wide; resource-scope
    globals are local to one resource."""
    cfg = configuration(
        "Default",
        resources=[
            resource("CPU1", global_vars=[var("cpu_local", TagType.INT)]),
        ],
        global_vars=[var("system_wide", TagType.INT)],
    )
    assert [v.name for v in cfg.global_vars] == ["system_wide"]
    assert [v.name for v in cfg.resources[0].global_vars] == ["cpu_local"]


def test_configuration_access_vars():
    from universal_machinery.builders import access_var
    cfg = configuration(
        "Default",
        access_vars=[access_var("hmi_tag",
                                 "Resource1.Main.value", TagType.INT)],
    )
    assert len(cfg.access_vars) == 1
    assert cfg.access_vars[0].alias == "hmi_tag"
    assert cfg.access_vars[0].instance_path == "Resource1.Main.value"


# -----------------------------------------------------------------------------
# Program-level wiring
# -----------------------------------------------------------------------------


def test_program_configurations_empty_by_default():
    p = program()
    assert p.configurations == []


def test_program_collects_configurations():
    cfg1 = configuration("Default", resources=[resource("CPU1")])
    cfg2 = configuration("Backup",  resources=[resource("CPU2")])
    p = program(configurations=[cfg1, cfg2])
    assert p.configurations == [cfg1, cfg2]
    assert p.find_configuration("Default") is cfg1
    assert p.find_configuration("Backup") is cfg2
    assert p.find_configuration("Ghost") is None


# -----------------------------------------------------------------------------
# Realistic combination
# -----------------------------------------------------------------------------


def test_realistic_configuration_with_multiple_tasks_and_instances():
    cfg = configuration(
        "ProductionLine",
        global_vars=[
            var("recipe_id",  TagType.INT),
            var("system_ok",  TagType.BOOL),
        ],
        resources=[
            resource(
                "MainController",
                tasks=[
                    task_spec("FastControl",  priority=1, interval="T#10ms"),
                    task_spec("MediumControl", priority=2, interval="T#100ms"),
                    task_spec("SlowControl",  priority=3, interval="T#1s"),
                    task_spec("OnEmergency", priority=0, single="emergency_btn"),
                ],
                pou_instances=[
                    pou_instance("FastLoop", type_name="MotionControl",
                                 task="FastControl"),
                    pou_instance("MediumLoop", type_name="ProcessControl",
                                 task="MediumControl"),
                    pou_instance("SlowLoop", type_name="Diagnostics",
                                 task="SlowControl"),
                    pou_instance("EStop", type_name="EmergencyStop",
                                 task="OnEmergency"),
                ],
                global_vars=[
                    var("cpu_temperature", TagType.REAL),
                    var("scan_time_ms",    TagType.INT),
                ],
            ),
        ],
    )
    p = program(configurations=[cfg])

    # Top-down navigation works
    found_cfg = p.find_configuration("ProductionLine")
    assert found_cfg is cfg
    res = found_cfg.find_resource("MainController")
    assert res is not None and len(res.tasks) == 4
    estop_task = res.find_task("OnEmergency")
    assert estop_task is not None and estop_task.single == "emergency_btn"
    assert estop_task.priority == 0
    estop_inst = res.find_pou_instance("EStop")
    assert estop_inst.type_name == "EmergencyStop"
    assert estop_inst.task == "OnEmergency"
