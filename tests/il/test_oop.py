"""Tests for IEC 61131-3 3rd-edition OOP additions.

Covers METHOD, INTERFACE, EXTENDS, IMPLEMENTS, and ABSTRACT
(``il.oop``) at four layers:

  - dataclass construction + lookups
  - builder DSL (``method``, ``abstract_method``, ``interface``)
  - ST emission (``emit_pou`` + ``emit_program``)
  - validation rules (EXTENDS/IMPLEMENTS resolution, abstract
    consistency, interface-method shape)
"""
import pytest

from universal_machinery.builders import (
    abstract_method, coil, fb, interface, method, move, no, prog, program,
    rung, tag, tag_decl, var_in, var_out,
)
from universal_machinery.emitters.st import emit_pou, emit_program
from universal_machinery.il import (
    AccessSpec, Interface, Method, PouKind, TagType,
)
from universal_machinery.serialisation import from_json, to_json
from universal_machinery.validation import is_valid, validate


# -----------------------------------------------------------------------------
# Dataclass construction + lookups
# -----------------------------------------------------------------------------


def test_method_signature_only():
    m = Method(name="Step", abstract=True)
    assert m.is_signature_only()

    m2 = Method(name="Step")  # no body, no abstract
    assert m2.is_signature_only()


def test_method_with_body_is_not_signature_only():
    from universal_machinery.il import Rung
    m = Method(name="Step", rungs=[Rung(ops=[])])
    assert not m.is_signature_only()


def test_interface_find_method():
    m1 = Method(name="A", abstract=True)
    m2 = Method(name="B", abstract=True)
    iface = Interface(name="IThing", methods=[m1, m2])
    assert iface.find_method("A") is m1
    assert iface.find_method("B") is m2
    assert iface.find_method("Missing") is None


def test_program_find_interface():
    iface = interface("IDrive", methods=[abstract_method("Start")])
    p = program(interfaces=[iface])
    assert p.find_interface("IDrive") is iface
    assert p.find_interface("Missing") is None


def test_subroutine_find_method():
    m = method("DoIt")
    sub = fb("MyFB", methods=[m])
    assert sub.find_method("DoIt") is m
    assert sub.find_method("Missing") is None


# -----------------------------------------------------------------------------
# Builder DSL
# -----------------------------------------------------------------------------


def test_method_builder_defaults_to_public_concrete():
    m = method("DoIt")
    assert m.access is AccessSpec.PUBLIC
    assert not m.abstract
    assert not m.override


def test_abstract_method_builder_is_abstract():
    m = abstract_method("DoIt")
    assert m.abstract
    assert m.rungs == []


def test_interface_builder_collects_methods():
    iface = interface("IDrive", methods=[
        abstract_method("Start"),
        abstract_method("Stop"),
    ])
    assert iface.name == "IDrive"
    assert len(iface.methods) == 2
    assert all(m.abstract for m in iface.methods)


def test_fb_with_oop_options():
    sub = fb("Motor",
             extends="MotorBase",
             implements=["IDrive", "IBraking"],
             abstract=True,
             methods=[abstract_method("Engage")])
    assert sub.kind is PouKind.FUNCTION_BLOCK
    assert sub.extends == "MotorBase"
    assert sub.implements == ["IDrive", "IBraking"]
    assert sub.abstract
    assert len(sub.methods) == 1


# -----------------------------------------------------------------------------
# ST emission
# -----------------------------------------------------------------------------


def test_st_emits_interface_block():
    iface = interface("IDrive", methods=[
        abstract_method("Start"),
        abstract_method("Stop"),
    ])
    p = program(interfaces=[iface])
    txt = emit_program(p)
    assert "INTERFACE IDrive" in txt
    assert "METHOD PUBLIC ABSTRACT Start" in txt
    assert "METHOD PUBLIC ABSTRACT Stop" in txt
    assert "END_INTERFACE" in txt


def test_st_emits_fb_extends_implements_abstract():
    sub = fb("MotorBase",
             abstract=True,
             implements=["IDrive"],
             methods=[abstract_method("Engage")])
    txt = emit_pou(sub)
    assert "FUNCTION_BLOCK ABSTRACT MotorBase" in txt
    assert "IMPLEMENTS IDrive" in txt
    assert "METHOD PUBLIC ABSTRACT Engage" in txt
    assert "END_METHOD" in txt
    assert "END_FUNCTION_BLOCK" in txt


def test_st_emits_fb_extends_chain():
    sub = fb("AcMotor", extends="MotorBase")
    txt = emit_pou(sub)
    assert "FUNCTION_BLOCK AcMotor EXTENDS MotorBase" in txt


def test_st_emits_concrete_method_with_body():
    m = method("Engage",
               inputs=[var_in("speed", TagType.INT)],
               rungs=[rung(no("X001"), coil("Y001"))])
    sub = fb("Motor", methods=[m])
    txt = emit_pou(sub)
    assert "METHOD PUBLIC Engage" in txt
    assert "VAR_INPUT" in txt
    assert "speed : INT" in txt
    assert "Y001 :=" in txt  # body emitted
    assert "END_METHOD" in txt


def test_st_emits_method_with_return_type():
    m = method("ReadValue", return_type=TagType.INT)
    sub = fb("Reader", methods=[m])
    txt = emit_pou(sub)
    assert "METHOD PUBLIC ReadValue : INT" in txt


def test_st_emits_method_access_specifiers():
    m_priv = method("Helper", access=AccessSpec.PRIVATE)
    m_prot = method("Inner", access=AccessSpec.PROTECTED)
    sub = fb("Owner", methods=[m_priv, m_prot])
    txt = emit_pou(sub)
    assert "METHOD PRIVATE Helper" in txt
    assert "METHOD PROTECTED Inner" in txt


def test_st_emits_override_qualifier():
    m = method("Engage", override=True)
    sub = fb("DcMotor", extends="MotorBase", methods=[m])
    txt = emit_pou(sub)
    assert "METHOD PUBLIC OVERRIDE Engage" in txt


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def test_extends_unknown_fb_flagged():
    p = program(subroutines=[
        fb("Child", extends="NotDeclared"),
    ])
    errors = validate(p)
    codes = [e.code for e in errors]
    assert "extends-unknown-fb" in codes


def test_extends_known_fb_passes():
    p = program(subroutines=[
        fb("Parent"),
        fb("Child", extends="Parent"),
    ])
    assert is_valid(p)


def test_implements_unknown_interface_flagged():
    p = program(subroutines=[
        fb("Motor", implements=["IMissing"]),
    ])
    codes = [e.code for e in validate(p)]
    assert "implements-unknown-iface" in codes


def test_implements_known_interface_passes():
    iface = interface("IDrive", methods=[abstract_method("Start")])
    p = program(
        interfaces=[iface],
        subroutines=[fb("Motor", implements=["IDrive"])],
    )
    # The FB doesn't OVERRIDE Start yet, but we don't enforce that
    # in this slice -- that's an interface-conformance check, a
    # separate concern.
    codes = [e.code for e in validate(p)]
    assert "implements-unknown-iface" not in codes
    assert "extends-unknown-fb" not in codes


def test_abstract_method_on_concrete_fb_flagged():
    p = program(subroutines=[
        fb("Motor", abstract=False,
           methods=[abstract_method("Engage")]),
    ])
    codes = [e.code for e in validate(p)]
    assert "abstract-method-on-concrete-fb" in codes


def test_abstract_method_on_abstract_fb_passes():
    p = program(subroutines=[
        fb("MotorBase", abstract=True,
           methods=[abstract_method("Engage")]),
    ])
    codes = [e.code for e in validate(p)]
    assert "abstract-method-on-concrete-fb" not in codes
    assert "abstract-method-has-body" not in codes


def test_interface_method_with_body_flagged():
    bad_method = method("Start", rungs=[rung(coil("Y001"))])
    iface = interface("IDrive", methods=[bad_method])
    p = program(interfaces=[iface])
    codes = [e.code for e in validate(p)]
    assert "interface-method-not-abstract" in codes
    assert "interface-method-has-body" in codes


def test_interface_method_abstract_passes():
    iface = interface("IDrive", methods=[abstract_method("Start")])
    p = program(interfaces=[iface])
    codes = [e.code for e in validate(p)]
    assert "interface-method-not-abstract" not in codes
    assert "interface-method-has-body" not in codes


# -----------------------------------------------------------------------------
# JSON round-trip
# -----------------------------------------------------------------------------


def test_oop_program_round_trips_through_json():
    iface = interface("IDrive", methods=[abstract_method("Start")])
    motor = fb("Motor",
               implements=["IDrive"],
               methods=[method("Start", override=True,
                               inputs=[var_in("speed", TagType.INT)],
                               rungs=[rung(coil("Y001"))])])
    p = program(interfaces=[iface], subroutines=[motor])

    js = to_json(p)
    p2 = from_json(js)

    assert len(p2.interfaces) == 1
    assert p2.interfaces[0].name == "IDrive"
    assert p2.interfaces[0].methods[0].abstract

    motor2 = p2.find_subroutine("Motor")
    assert motor2.implements == ["IDrive"]
    assert motor2.methods[0].name == "Start"
    assert motor2.methods[0].override
    assert motor2.methods[0].access is AccessSpec.PUBLIC
