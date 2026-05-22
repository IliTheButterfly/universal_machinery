# `universal_machinery.lowering`

IL → IL passes.  Each module rewrites a `Program` into a form a specific backend can consume more easily.

## FBD → ST

::: universal_machinery.lowering.fbd_to_st
    options:
      members:
        - lower_fbd_to_st
        - LoweringResult
        - LoweringError

## CLICK calling convention

!!! warning "Experimental — partial"
    `click_calling` covers slot allocation, caller-side marshalling, body rewriting, and trampoline emission.  The scheduler trampoline + inside-subroutine-call rewriter still have TODOs.  Sufficient to lower most parameter-passing patterns, but not yet the gating piece for a CLICK encoder.

::: universal_machinery.lowering.click_calling
    options:
      members:
        - LoweringConfig
        - PouSlots
        - SlotAllocation
        - allocate_slots
        - marshal_call
        - lower_calls
        - rewrite_callee_body
        - lower_pou_bodies
        - emit_dispatch_rungs
        - emit_trampoline
        - prepend_trampoline_to_main
        - LoweringError
