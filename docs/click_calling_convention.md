# CLICK calling convention for parameterized POUs

CLICK PLC subroutines have **no parameters and no return values** -- a
CLICK `CALL` transfers control by `sub_id`, nothing more.  CLICK also
**forbids `CALL` inside a subroutine body**: only the Main Program
may issue a CALL.  To make `universal_machinery.il`'s parameterized
POUs (FUNCTION, FUNCTION_BLOCK, DataBlock, SFC, nested calls) work on
this target, the click backend uses two complementary mechanisms:

1.  A **reserved-region calling convention** that materialises
    VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT / instance state as fixed DS
    register slots.
2.  A **cooperative scheduler** in the Main routine that drives
    callee POUs across scans, allowing one callee to "invoke"
    another even though CLICK's CALL can't be nested.

This document describes the design.  It is the contract the IL → CKP
lowering will follow; the runtime side is just CLICK ladder logic.

---

## 1. Memory layout

The backend allocates four reserved ranges.  Defaults shown; each is
configurable per-project:

| Range               | Purpose                                          |
| ------------------- | ------------------------------------------------ |
| `DS9000 .. DS9099`  | **Argument area** -- callee VAR_INPUT slots      |
| `DS9100 .. DS9199`  | **Return area** -- callee VAR_OUTPUT slots       |
| `DS9200 .. DS9299`  | **Instance-DB base table** -- one entry per FB instance |
| `DS9800 .. DS9899`  | **Scheduler work area** (next-id, stack, frames) |
| `C2000  .. C2255`   | **POU-active flags** (1 bit per declared POU)    |
| `C2256  .. C2511`   | **Step-active flags** (1 bit per SFC step)       |

At compile time the backend assigns each POU a `slot_base` into the
argument area such that:

```
inputs[i].address  = DS9000 + slot_base + i
outputs[j].address = DS9100 + slot_base + j
```

Non-overlapping allocation is the lowering's responsibility.  For
DataBlocks with an explicit `base_address`, the backend honours it
verbatim and skips that range during slot allocation.

---

## 2. Caller protocol

A `Call(target=F, inputs=((a, src_a), (b, src_b)), outputs=((q, dst_q)))`
**from the Main routine** lowers to a sequence of CLICK rungs:

```
Rung:  Move(src_a -> F.inputs.a)        ; marshal IN
Rung:  Move(src_b -> F.inputs.b)
Rung:  Move(<instance.base> -> DS9200 + F.instance_slot)
                                        ; for FUNCTION_BLOCK only
Rung:  Call(sub_id = F.click_sub_id)    ; native CLICK CALL
Rung:  Move(F.outputs.q -> dst_q)       ; unmarshal OUT
```

For a `FUNCTION` with `return_to=Address("DS12")`, the implicit
return value lives in the first VAR_OUTPUT slot and is moved out
after CALL just like any other output.

---

## 3. Callee protocol

A POU compiled with this convention begins each scan-when-active by
implicitly:

  1. Reading its inputs from its reserved input slots into its
     LOCAL working scratch (or, more efficiently, referring to the
     reserved slots directly inside the body).
  2. **For FUNCTION_BLOCKs**: resolving its VAR (state) references
     via the instance DB base address in `DS9200 + slot`.  Each
     internal state reference is rewritten to `<instance_base> +
     <member_offset>`.

Before returning, the body writes its outputs into its reserved
output slots.  The final rung is the standard CLICK `RETURN`.

---

## 4. Nested-call mimicry: the scheduler

CLICK rejects `CALL` inside a subroutine body, so we can't simply
emit a CLICK CALL when a POU `F` wants to invoke `G`.  Instead the
backend rewrites those nested calls into a **cooperative continuation-passing
form** managed by a top-of-Main *trampoline*.

### Scheduler state

```
DS9800            next_pou_id          (0 = main idle)
DS9801 .. DS9831  return_id_stack      (32 entries deep)
DS9832            stack_pointer        (count of pushed frames)
DS9833 .. DS9863  resume_label_stack   (one per frame: where to resume in caller)
```

### Main trampoline

Main begins with a switch-style dispatcher:

```
IF DS9800 != 0:                         ; a POU is queued
  SET   C2000 + DS9800                  ; mark target POU active
  CALL  pou_table[DS9800]               ; native CLICK CALL, ONE level deep
  RST   C2000 + DS9800
  pop:  DS9800 = stack_top()            ; back to caller (or 0 if idle)
        sp = sp - 1
END IF
```

`pou_table` is a small jump table compiled per-program: a sequence of
`IF DS9800 == n THEN CALL Sub_n` rungs.

### Issuing a nested call from inside POU F

When F's body needs to invoke G with arguments, the lowering replaces
the conceptual `Call(target=G, ...)` with:

  1.  Marshal G's inputs (same Moves as the top-level caller protocol).
  2.  **Push** the current frame onto the scheduler stack:
      ```
      DS9801 + sp = F.click_sub_id        ; remember who to resume
      DS9833 + sp = F.resume_site_id      ; and where in F to resume
      sp = sp + 1
      ```
  3.  Set `DS9800 = G.click_sub_id`.
  4.  Issue CLICK `RETURN` -- F yields control to Main.
  5.  Main's trampoline dispatches G on the next scan.
  6.  When G finishes (its body's RETURN), the trampoline pops the
      stack and writes the previous id back into `DS9800`.  The next
      scan re-enters F, but F's body is now *gated by* a check on
      `resume_site` so it skips ahead to the rung labeled
      `resume_site_id`, picking up where it left off.

The cost is one scan per call hop.  For typical sequencing programs
(grafcet steps, recipe drivers, alarm aggregation) this is acceptable
-- modern CLICK CPUs run sub-millisecond scans.  Tight numeric loops
should be flattened into a single POU.

### Leaf-call optimisation

A POU that issues no calls of its own is a **leaf**.  For leaf POUs
the lowering may bypass the scheduler entirely and use a CLICK-native
CALL from Main; this is the common case for FUNCTIONs.  The scheduler
machinery is only needed when nesting depth > 0 is actually used.

### Tail-call optimisation

A `Call` that is the last op in a POU's body lowers to:

```
sp unchanged                    ; don't push our frame back
DS9800 = G.click_sub_id
RETURN
```

The trampoline resumes the caller's caller directly, saving one scan.

---

## 5. SFC (grafcet) lowering

Each `Step` gets:

  - a step-active bit in `C2256 + step_id`
  - a "step started this scan" rising-edge bit for `P` qualifier actions

Each `Transition` becomes a rung whose left-hand side is:

  `<all from-step bits active> AND <condition ops>`

and whose RHS performs:

```
RST  C2256 + from_step[0]
RST  C2256 + from_step[1]
...
SET  C2256 + to_step[0]
SET  C2256 + to_step[1]
```

(Atomic activation in one CLICK scan is provided by CLICK's "output
mirror" semantics: writes only take effect at end of scan, so a
transition that reads `from` and writes `to` in the same rung is
race-free.)

`Action` lowering:

| Qualifier | Lowering                                                |
| --------- | ------------------------------------------------------- |
| `N`       | `IF step THEN Out(target)` each scan                    |
| `S`       | `IF step THEN OutSet(target)`                           |
| `R`       | `IF step THEN OutReset(target)`                         |
| `P`       | `IF step.rising_edge THEN Out(target)` (one-shot)       |
| `L`       | TON with `preset=time_ms`, gated by step                |
| `D`       | TON-delayed turn-on                                     |
| FB target | desugars to a Call op on step activation                |

Initial steps get their bit pre-set in the project initialisation
block (compiled as a one-shot rung gated by `SC1` -- CLICK's
"first-scan" system bit).

---

## 6. Open lowering decisions

The following are intentionally left to implementation time and not
yet locked in:

  - **Address-region defaults**: the `DS9xxx` and `C2xxx` bases are
    high enough to avoid common user ranges but should be
    user-overridable via a `LoweringConfig`.
  - **Stack overflow handling**: if `sp` would exceed 32, set a
    diagnostic C-bit and abort the dispatch; the program can react
    via an alarm rung.  This corresponds to "recursion too deep".
  - **Mutual recursion**: allowed by the scheduler model (each call
    is just an enqueue), but burns a frame per hop -- avoid in
    user code unless intentional.
  - **String / array parameters**: CLICK has no native variable-length
    types; passing a string means passing a pointer (base DS address)
    plus a length.  Defer until we have a concrete use case.
