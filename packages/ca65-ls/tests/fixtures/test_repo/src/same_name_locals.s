; same_name_locals.s -- regression fixture for scope-aware references.
;
; Mirrors c64-https style: routines are plain `label:` + `rts`, and same-named
; loop/done cheap locals appear inside multiple routines.  Without scope-aware
; filtering, find_referencing_symbols on routine_a's @loop would also surface
; routine_b's `bne @loop`, which is semantically wrong (different parent,
; different address).

.include "zp.inc"

.export routine_a
.export routine_b

.segment "CODE"

routine_a:
        ldx     #$00
@loop:                                  ; routine_a-scoped cheap local
        lda     ptr1
        sta     ptr2,x
        inx
        cpx     #$10
        bne     @loop                   ; jumps to routine_a's @loop
        beq     @done
@done:                                  ; routine_a-scoped cheap local
        rts

routine_b:
        ldy     #$00
@loop:                                  ; routine_b-scoped cheap local (distinct!)
        lda     ptr2
        sta     ptr1,y
        iny
        cpy     #$08
        bne     @loop                   ; jumps to routine_b's @loop
@done:                                  ; routine_b-scoped cheap local (distinct!)
        rts
