---
name: verify-cert
description: Verify the project's PLCopen IEC 61131-3 conformance posture. Runs the cert-relevant pytest subset, validates a representative emit corpus against the bundled TC6 v2.01 XSD, audits docs/CONFORMANCE_TEST_PLAN.md fixture pointers, and reports remaining ⚠️ / ❌ rows. Use when the user asks "are we still cert-ready?", "how does cert stand?", or before cutting a release that claims conformance.
---

Runs the certification-readiness check loop for `universal_machinery`.

Cert posture (per `docs/IEC_CONFORMANCE.md` and the certification-strategy memory) is tiered:

- **Tier 1**: PLCopen TC6 XML conformance (the practical target). XSD validity + reference-tool round-trip.
- **Tier 2**: Full IEC 61131-3 language conformance. Phased.
- **Tier 3**: IEC 61508 functional safety / SIL. Out of scope.

This skill exercises Tier 1's verification harness end-to-end. It doesn't grant cert — only an accredited body does that. But it gives an honest "would-we-pass-our-own-bar" snapshot.

## When to Use

- User asks about cert status, conformance posture, "are we still cert-ready"
- Before bumping the PLCopen-conformance claim in `docs/IEC_CONFORMANCE.md`
- After any PR that touches `emitters/plcopen_xml.py` / `parsers/plcopen_xml.py` / `validation.py`
- Before cutting a release tag

## When NOT to Use

- Routine PRs that don't touch the cert path (CLI tweaks, doc-only changes outside the conformance plan). The standard `pytest` run already covers those.
- Hardware-in-the-loop verification — that's a separate engagement; this skill only runs the static / emulator-level checks.

## Workflow

Run the checks below **in this order** and aggregate the findings into a final report. Don't bail on the first failure — keep going so the user sees the full picture.

### Step 1 — Run the cert-relevant pytest subset

The conformance plan rows all point at fixtures under `tests/`; running this subset confirms every cert-claim row is still backed by a green test.

```bash
python -m pytest \
  tests/test_conformance_plan_pointers.py \
  tests/emitters/test_plcopen_xml.py \
  tests/emitters/test_plcopen_xml_ld.py \
  tests/emitters/test_plcopen_xml_sfc.py \
  tests/emitters/test_plcopen_xml_fbd.py \
  tests/parsers/test_plcopen_xml_reader.py \
  tests/parsers/test_plcopen_xml_reader_udt.py \
  tests/parsers/test_plcopen_xml_reader_fbd.py \
  tests/il/test_validation.py \
  tests/il/test_fbd_pin_type_check.py \
  tests/il/test_serialisation.py \
  tests/lowering/test_fbd_to_st.py \
  -q
```

Record:
- **Pass count** from the last line ("N passed").
- **Failures**, if any — quote the first ~5 to keep the report short.

### Step 2 — Compare actual pass count to the doc's snapshot

```bash
grep -E "current passing total is \*\*[0-9]+|status: \*\*[0-9]+" \
  docs/CONFORMANCE_TEST_PLAN.md
```

Both lines should report the same number, and it should match (or be ≤ by an explainable amount, since the cert subset isn't the full suite) the pytest run from Step 1. The conformance-doc auditor (PR #50) already enforces internal-snapshot consistency; this step catches drift between the snapshot and reality.

If the snapshot is stale, **don't silently bump it** — the drift is information the user should see.

### Step 3 — Spot-check XSD validity on a representative corpus

XSD validation runs inside the test suite, but a quick standalone confirmation against a synthesised program (POU + var blocks + LD body + SFC body + configuration) catches regressions in the emit shape that the test corpus might miss.

```bash
python - <<'PY'
from datetime import datetime, timezone
from universal_machinery.builders import (
    program, prog, rung, no, coil, ton, ctu, sr,
    var, jump, label_, ret,
)
from universal_machinery.il import TagType
from universal_machinery.il.sfc import SfcNetwork, Step, Transition, Action
from universal_machinery.il.ops import Compare, Move, BinaryMath
from universal_machinery.emitters.plcopen_xml import (
    emit_xml, validate_plcopen_xml,
)
sfc = SfcNetwork(
    steps=[Step("Init", initial=True,
                  actions=(Action(qualifier="N", target="run_pou"),)),
             Step("Done")],
    transitions=[Transition(from_steps=("Init",), to_steps=("Done",))],
)
p = program(subroutines=[
    prog("Main", main=True,
         local_vars=[var("counter", TagType.INT)],
         rungs=[
             rung(no("X1"), coil("Y1")),
             rung(ton("T1", 1000, done_bit="Q1")),
             rung(ctu("C1", 10)),
             rung(sr("Q2", "S2", "R2")),
             rung(Compare(op=">", lhs="counter", rhs="50"),
                  Move(src="counter", dst="last")),
             rung(BinaryMath(op="+", lhs="counter", rhs="1",
                                dst="counter")),
             rung(jump("END_OF_SCAN")),
             rung(label_("END_OF_SCAN"), ret()),
         ]),
    prog("Sequencer", main=False, sfc=sfc),
])
xml = emit_xml(p, time_now=datetime(2026, 5, 22, tzinfo=timezone.utc))
validate_plcopen_xml(xml)
print(f"XSD-VALID  emitted_bytes={len(xml)}  pous=2")
PY
```

This should print `XSD-VALID emitted_bytes=... pous=2`. If it raises, the cert posture is broken — investigate before reporting.

### Step 4 — Enumerate remaining ⚠️ / ❌ rows in the plan

```bash
grep -E "^\| (.*?\| (⚠️|❌))" docs/CONFORMANCE_TEST_PLAN.md
```

Group by reason:
- **XSD-blocked** (OOP at v2.01)
- **Out of scope** (IL deprecated, communication FBs)
- **Externally blocked** (PLCopen reference-tool round-trip — needs matiec / Beremiz subprocess)
- **Genuinely open** (anything else — these are real follow-up work)

### Step 5 — Check for external reference-tool readiness

The cert path's "real" deliverable is round-trip through accredited PLCopen tools. We don't run them in CI yet, but check whether they're installed locally so the user knows whether a manual round-trip is a quick `which matiec` away or needs a setup pass.

```bash
which matiec 2>/dev/null || echo "  matiec: NOT INSTALLED"
which beremiz 2>/dev/null || echo "  beremiz: NOT INSTALLED"
which openplc_editor 2>/dev/null || echo "  openplc_editor: NOT INSTALLED"
```

Note in the report which (if any) are present. **Don't actually pipe emitted XML through them in this skill** — that's a separate engagement.

### Step 6 — Print the structured summary

Format:

```
PLCopen TC6 v2.01 Cert Readiness — <YYYY-MM-DD>

Verification harness:
  ✅ Cert-subset pytest:        <N> passed, <M> failed
  ✅ Conformance-doc auditor:    PASS / FAIL
  ✅ XSD spot-check (sample):    VALID / INVALID
  ⚠️ Pass-count snapshot drift: <doc> doc vs <actual> actual

Outstanding rows in the plan:
  XSD-blocked (v2.01):
    - <list ⚠️ rows>
  Out of scope:
    - <list ❌ rows>
  Externally blocked (reference-tool round-trip):
    - PLCopen reference-tool round-trip (CONFORMANCE_TEST_PLAN.md "What's NOT covered" item 1)

External tools (for manual round-trip):
  - matiec: <PRESENT | MISSING>
  - beremiz: <PRESENT | MISSING>
  - openplc_editor: <PRESENT | MISSING>

Tier 1 (PLCopen XML) cert claim:
  - Structural alignment: <SOUND | DEGRADED>  (driven by failing tests + XSD spot-check)
  - Reference-tool round-trip: <UNVERIFIED>     (always — this skill doesn't run external tools)

Tier 2 (full IEC 61131-3 language) cert:
  - Phased; backend reach (CLICK / OpenPLC lowering) is the gating factor.

Tier 3 (IEC 61508 functional safety / SIL):
  - Explicitly out of scope per docs/ARCHITECTURE.md.
```

Keep it terse — the report is informational, not an audit log. Quote specific row names rather than counts so the user can grep the doc directly.

## Interpreting Results

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `fbd-pin-type-mismatch` in unrelated tests | Builtin signature DB drifted | Check `_BUILTIN_BLOCK_PIN_TYPES` |
| `xsd:validation` raises | Emit shape regressed | Check the recent PRs that touched `_emit_*_xml` helpers |
| Doc snapshot drift | Hand-tracking failure | Update the snapshot count in both docs |
| Conformance-doc auditor failure | Test renamed / moved without doc update | `tests/test_conformance_plan_pointers.py` will pinpoint the broken pointer |
| Cert-subset pytest passes; full `pytest` fails | Non-cert test broke | Out of cert scope; report it but don't gate the cert claim on it |

## Notes

- **This skill is a static check**, not certification. Tier 1 cert needs accredited testing; this skill confirms our claims would survive a first review.
- **The roadmap item "PLCopen reference-tool round-trip"** (`docs/CONFORMANCE_TEST_PLAN.md` § "What's NOT covered" item 1) is the next major cert-side slice. Building a matiec/Beremiz subprocess harness is the natural follow-up.
- **Don't silently fix issues found** — surface them. The user benefits from seeing what's drifting before they decide whether to fix or document.
- **Pass count alignment** matters more than absolute count: cert-side audits ask "do your tests pass?", not "do you have N tests?"

## Cross-references

- `docs/IEC_CONFORMANCE.md` — canonical IEC mapping table
- `docs/CONFORMANCE_TEST_PLAN.md` — row-by-row test fixture pointers
- `docs/ARCHITECTURE.md` — verification posture (emulator + hardware-in-the-loop)
- `tests/test_conformance_plan_pointers.py` — the conformance-doc auditor itself
- Memory `[[certification-strategy]]` — the project's cert posture
