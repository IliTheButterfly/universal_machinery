"""Tests for IEC §2.4.3 VAR_ACCESS and VAR_CONFIG.

Covers four layers:

  - Dataclass construction + lookups
  - Builder DSL (``access_var``, ``config_var``, ``configuration``
    extension)
  - ST emission (``CONFIGURATION ... VAR_ACCESS ... VAR_CONFIG``)
  - PLCopen XML emission (``<accessVars>`` / ``<configVars>``,
    XSD-validated)
  - JSON round-trip
  - Validation (direction enum, alias uniqueness, instance-path
    syntax, duplicate config bindings)
"""
import pytest

from universal_machinery.builders import (
    access_var, config_var, configuration, pou_instance, prog, program,
    resource, task_spec, var_in,
)
from universal_machinery.emitters.st import _fmt_configuration
from universal_machinery.il import (
    AccessVar, ConfigVar, Configuration, TagType,
)
from universal_machinery.serialisation import from_json, to_json
from universal_machinery.validation import is_valid, validate


# -----------------------------------------------------------------------------
# Dataclass construction + lookups
# -----------------------------------------------------------------------------


def test_access_var_defaults_direction_to_read_write():
    v = AccessVar(alias="x",
                   instance_path="R1.Main.x",
                   data_type=TagType.INT)
    assert v.direction == "READ_WRITE"


def test_config_var_default_initial_is_empty():
    v = ConfigVar(instance_path="R1.Main.threshold",
                   data_type=TagType.INT)
    assert v.initial_value == ""


def test_configuration_find_access_var_by_alias():
    a = access_var("hmi_a", "R1.Main.a", TagType.INT)
    b = access_var("hmi_b", "R1.Main.b", TagType.BOOL)
    cfg = configuration("Default", access_vars=[a, b])
    assert cfg.find_access_var("hmi_a") is a
    assert cfg.find_access_var("hmi_b") is b
    assert cfg.find_access_var("missing") is None


def test_configuration_find_config_var_by_path():
    c = config_var("R1.Main.threshold", TagType.INT, initial="100")
    cfg = configuration("Default", config_vars=[c])
    assert cfg.find_config_var("R1.Main.threshold") is c
    assert cfg.find_config_var("nonexistent.path") is None


# -----------------------------------------------------------------------------
# Builder DSL
# -----------------------------------------------------------------------------


def test_access_var_builder_propagates_direction():
    v = access_var("hmi_x", "R1.Main.x", TagType.INT,
                    direction="READ_ONLY")
    assert v.direction == "READ_ONLY"


def test_config_var_builder_with_initial():
    v = config_var("R1.Main.threshold", TagType.INT, initial="100",
                    comment="default threshold")
    assert v.initial_value == "100"
    assert v.comment == "default threshold"


def test_configuration_accepts_both_lists():
    cfg = configuration("Default",
        access_vars=[access_var("a", "R.M.x", TagType.INT)],
        config_vars=[config_var("R.M.y", TagType.INT, initial="0")],
    )
    assert len(cfg.access_vars) == 1
    assert len(cfg.config_vars) == 1


# -----------------------------------------------------------------------------
# ST emission
# -----------------------------------------------------------------------------


def test_st_emits_var_access_block_with_iec_syntax():
    cfg = configuration("Default", access_vars=[
        access_var("hmi_speed", "R1.Main.speed", TagType.INT),
        access_var("hmi_state", "R1.Main.state", TagType.BOOL,
                    direction="READ_ONLY"),
    ])
    txt = _fmt_configuration(cfg)
    assert "VAR_ACCESS" in txt
    # IEC §2.7.1: ``alias : instance_path : type direction;``
    assert ("hmi_speed : R1.Main.speed : INT READ_WRITE;") in txt
    assert ("hmi_state : R1.Main.state : BOOL READ_ONLY;") in txt
    assert "END_VAR" in txt


def test_st_emits_var_config_block():
    cfg = configuration("Default", config_vars=[
        config_var("R1.MainProg.threshold", TagType.INT, initial="100"),
        config_var("R1.MainProg.deadband",  TagType.REAL, initial="0.5"),
    ])
    txt = _fmt_configuration(cfg)
    assert "VAR_CONFIG" in txt
    assert "R1.MainProg.threshold : INT := 100;" in txt
    assert "R1.MainProg.deadband : REAL := 0.5;" in txt


def test_st_omits_blocks_when_lists_empty():
    cfg = configuration("Default")
    txt = _fmt_configuration(cfg)
    assert "VAR_ACCESS" not in txt
    assert "VAR_CONFIG" not in txt


# -----------------------------------------------------------------------------
# PLCopen XML emission + XSD validation
# -----------------------------------------------------------------------------


xmlschema = pytest.importorskip("xmlschema")
from universal_machinery.emitters.plcopen_xml import (
    emit_xml, validate_plcopen_xml,
)


def _emit(cfg):
    p = program(
        subroutines=[prog("Main", main=True,
                            inputs=[var_in("speed", TagType.INT)])],
        configurations=[cfg],
    )
    return emit_xml(p)


def test_xml_access_vars_emit_alias_path_direction():
    cfg = configuration("Default",
        resources=[resource("R1", pou_instances=[
            pou_instance("MainProg", type_name="Main")])],
        access_vars=[
            access_var("hmi_speed", "R1.MainProg.speed", TagType.INT,
                        direction="READ_ONLY"),
            access_var("hmi_state", "R1.MainProg.state", TagType.BOOL,
                        direction="READ_WRITE"),
        ],
    )
    xml = _emit(cfg)
    validate_plcopen_xml(xml)
    assert 'alias="hmi_speed"' in xml
    assert 'instancePathAndName="R1.MainProg.speed"' in xml
    assert 'direction="readOnly"' in xml
    assert 'direction="readWrite"' in xml


def test_xml_config_vars_emit_initial_value():
    cfg = configuration("Default",
        resources=[resource("R1", pou_instances=[
            pou_instance("MainProg", type_name="Main")])],
        config_vars=[
            config_var("R1.MainProg.threshold", TagType.INT,
                        initial="100"),
        ],
    )
    xml = _emit(cfg)
    validate_plcopen_xml(xml)
    assert "<configVars>" in xml
    assert ('<configVariable instancePathAndName='
            '"R1.MainProg.threshold">') in xml
    assert '<simpleValue value="100"/>' in xml


def test_xml_config_var_without_initial_skips_initialValue_element():
    cfg = configuration("Default",
        resources=[resource("R1", pou_instances=[
            pou_instance("MainProg", type_name="Main")])],
        config_vars=[
            config_var("R1.MainProg.thresh", TagType.INT),
        ],
    )
    xml = _emit(cfg)
    validate_plcopen_xml(xml)
    assert "<configVars>" in xml
    assert "<initialValue>" not in xml


# -----------------------------------------------------------------------------
# JSON round-trip
# -----------------------------------------------------------------------------


def test_access_var_round_trips_through_json():
    cfg = configuration("Default",
        access_vars=[
            access_var("a", "R1.M.a", TagType.INT, direction="READ_ONLY",
                        comment="speed sensor"),
        ],
        config_vars=[
            config_var("R1.M.threshold", TagType.INT, initial="100"),
        ],
    )
    p = program(configurations=[cfg])
    js = to_json(p)
    p2 = from_json(js)
    cfg2 = p2.configurations[0]
    assert cfg2.access_vars[0].alias == "a"
    assert cfg2.access_vars[0].direction == "READ_ONLY"
    assert cfg2.access_vars[0].comment == "speed sensor"
    assert cfg2.config_vars[0].instance_path == "R1.M.threshold"
    assert cfg2.config_vars[0].initial_value == "100"


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def _validate_with_cfg(cfg) -> list:
    p = program(configurations=[cfg])
    return [e.code for e in validate(p)]


def test_clean_access_and_config_vars_validate():
    cfg = configuration("Default",
        access_vars=[access_var("a", "R1.M.x", TagType.INT)],
        config_vars=[config_var("R1.M.y", TagType.INT, initial="0")],
    )
    p = program(configurations=[cfg])
    assert is_valid(p)


def test_access_var_bad_direction_flagged():
    cfg = configuration("Default", access_vars=[
        AccessVar(alias="a", instance_path="R1.M.x",
                   data_type=TagType.INT, direction="WRITE_ONLY"),
    ])
    codes = _validate_with_cfg(cfg)
    assert "access-var-bad-direction" in codes


def test_access_var_duplicate_alias_flagged():
    cfg = configuration("Default", access_vars=[
        access_var("a", "R1.M.x", TagType.INT),
        access_var("a", "R1.M.y", TagType.INT),
    ])
    codes = _validate_with_cfg(cfg)
    assert "access-var-duplicate-alias" in codes


def test_access_var_bad_path_flagged():
    cfg = configuration("Default", access_vars=[
        access_var("a", "notAnAccessPath", TagType.INT),
    ])
    codes = _validate_with_cfg(cfg)
    assert "access-var-bad-path" in codes


def test_config_var_bad_path_flagged():
    cfg = configuration("Default", config_vars=[
        config_var("just_one_segment", TagType.INT),
    ])
    codes = _validate_with_cfg(cfg)
    assert "config-var-bad-path" in codes


def test_config_var_duplicate_path_flagged():
    cfg = configuration("Default", config_vars=[
        config_var("R1.M.x", TagType.INT, initial="100"),
        config_var("R1.M.x", TagType.INT, initial="200"),
    ])
    codes = _validate_with_cfg(cfg)
    assert "config-var-duplicate-path" in codes


def test_path_with_array_index_accepted():
    cfg = configuration("Default", access_vars=[
        access_var("a", "R1.Main.buffer[0]", TagType.INT),
    ])
    codes = _validate_with_cfg(cfg)
    assert "access-var-bad-path" not in codes
