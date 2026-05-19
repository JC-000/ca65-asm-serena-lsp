; label_style.s -- mirrors the routine convention used by c64-https and
; other ca65 codebases that don't wrap routines in `.proc ... .endproc`.
;
; Three top-level routines defined by plain labels:
;   * extract_byte:        ends at the next sibling label
;   * extract_word:        has cheap locals (@retry, @done)
;   * extract_buffer:      has an anonymous label (:) used by :+/:- arithmetic
; Plus one data label `extract_scratch:` in BSS that should NOT consume any
; cheap-locals.

.include "zp.inc"

.export extract_byte
.export extract_word
.export extract_buffer
.export extract_scratch

.segment "BSS"
extract_scratch:
        .res    32

.segment "CODE"

extract_byte:
        lda     ptr1
        rts

extract_word:
@retry:
        lda     ptr1
        bne     @retry
@done:
        sta     ptr2
        rts

extract_buffer:
:       lda     ptr2
        beq     :+
        sta     ptr1
        jmp     :-
:       rts
