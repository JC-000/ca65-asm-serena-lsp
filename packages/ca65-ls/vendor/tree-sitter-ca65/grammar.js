/**
 * @file ca65 grammar for tree-sitter
 * @author pogyomo <pogyomo@gmail.com>
 * @license MIT
 */

/// <reference types="tree-sitter-cli/dsl" />
// @ts-check

const PREC = {
  OP: {
    UNARY: {
      POSITIVE: 8,
      NEGATIVE: 8,
      BITNOT: 8,
      LOBYTE: 8,
      HIBYTE: 8,
      BANKBYTE: 8,
      NOT: 2,
    },
    BINARY: {
      MUL: 7,
      DIV: 7,
      MOD: 7,
      BITAND: 7,
      BITXOR: 7,
      LSHIFT: 7,
      RSHIFT: 7,
      ADD: 6,
      SUB: 6,
      BITOR: 6,
      EQ: 5,
      NE: 5,
      LT: 5,
      GT: 5,
      LE: 5,
      GE: 5,
      AND: 4,
      XOR: 4,
      OR: 3,
    },
  },
  PRIMARY: {
    GROUP: 1,
    PSEUDO_VAR: 1,
    PSEUDO_FUN: 1,
    SYMBOL: 1,
    NUMBER: 1,
    STRING: 1,
    CHAR: 1,
    MEMBER: 1,
    MACRO_CALL: 1,
    LOCAL_LABEL: 1,
    UNNAMED_LABEL: 1,
  },
};

module.exports = grammar({
  name: "ca65",

  extras: ($) => [/\s*/, $.comment],

  rules: {
    source: ($) => repeat($.source_line),
    source_line: ($) =>
      seq(
        choice(
          field("symbol", $._symbol),
          field("label", $._label),
          field("inst", $._inst),
          seq(field("label", $._label), field("inst", $._inst)),
        ),
        token(prec(1, /\r?\n/)),
      ),

    // Symbols
    _symbol: ($) => choice($.symbol_eq, $.symbol_assign, $.symbol_set),
    symbol_eq: ($) =>
      seq(
        // Higher precedence than equal operator
        prec(100, seq(field("name", $.symbol), "=")),
        field("expr", $._expression),
      ),
    symbol_assign: ($) =>
      seq(field("name", $.symbol), ":=", field("expr", $._expression)),
    symbol_set: ($) =>
      seq(
        field("name", $.symbol),
        $.dot_keyword_set,
        field("expr", $._expression),
      ),

    // Labels
    _label: ($) => choice($.label, $.local_label, $.unnamed_label),
    label: ($) => seq($.label_body, ":"),
    local_label: ($) => seq($.local_label_body, ":"),
    unnamed_label: (_) => ":",
    label_body: ($) => $._identifier,
    local_label_body: ($) => seq("@", $._identifier),

    // Instruction
    _inst: ($) => choice($.macro_inst, $.actual_inst, $._pseudo_inst),

    // Macro instruction
    macro_inst: ($) =>
      seq(field("name", $.macro_inst_name), optional($._macro_inst_args)),
    macro_inst_name: ($) => prec(1, $._identifier), // Higher precedence than macro_call_name
    _macro_inst_args: ($) =>
      seq(
        field("arg", $._macro_inst_arg),
        optional(seq(",", $._macro_inst_args)),
      ),
    _macro_inst_arg: ($) =>
      choice(seq("{", $.macro_inst_arg_enclosed, "}"), $.macro_inst_arg_raw),
    macro_inst_arg_raw: (_) => token(prec(-1, /[^(\r?\n)\,]*/)),
    macro_inst_arg_enclosed: (_) => token(prec(-1, /[^(\r?\n)\}]*/)),

    // Actual instructions
    actual_inst: ($) => {
      const opcodes = [
        $.opcode_adc,
        $.opcode_and,
        $.opcode_asl,
        $.opcode_bcc,
        $.opcode_bcs,
        $.opcode_beq,
        $.opcode_bit,
        $.opcode_bmi,
        $.opcode_bne,
        $.opcode_bpl,
        $.opcode_brk,
        $.opcode_bvc,
        $.opcode_bvs,
        $.opcode_clc,
        $.opcode_cld,
        $.opcode_cli,
        $.opcode_clv,
        $.opcode_cmp,
        $.opcode_cpx,
        $.opcode_cpy,
        $.opcode_dec,
        $.opcode_dex,
        $.opcode_dey,
        $.opcode_eor,
        $.opcode_inc,
        $.opcode_inx,
        $.opcode_iny,
        $.opcode_jmp,
        $.opcode_jsr,
        $.opcode_lda,
        $.opcode_ldx,
        $.opcode_ldy,
        $.opcode_lsr,
        $.opcode_nop,
        $.opcode_ora,
        $.opcode_pha,
        $.opcode_php,
        $.opcode_pla,
        $.opcode_plp,
        $.opcode_rol,
        $.opcode_ror,
        $.opcode_rti,
        $.opcode_rts,
        $.opcode_sbc,
        $.opcode_sec,
        $.opcode_sed,
        $.opcode_sei,
        $.opcode_sta,
        $.opcode_stx,
        $.opcode_sty,
        $.opcode_tax,
        $.opcode_tay,
        $.opcode_tsx,
        $.opcode_txa,
        $.opcode_txs,
        $.opcode_tya,
      ];
      const operands = [
        $.operand_imm,
        $.operand_a,
        $.operand_addr,
        $.operand_addr_x,
        $.operand_addr_y,
        $.operand_indirect,
        $.operand_x_indirect,
        $.operand_indirect_y,
      ];
      return choice(
        ...product(opcodes, operands).map(([opcode, operand]) => {
          return seq(
            field("opcode", opcode),
            optional(field("operand", operand)),
          );
        }),
      );
    },

    // Operands
    operand_imm: ($) => seq("#", field("expr", $._expression)),
    operand_a: ($) => field("reg", $.register_a),
    operand_addr: ($) => field("addr", $._expression),
    operand_addr_x: ($) =>
      seq(field("addr", $._expression), ",", field("reg", $.register_x)),
    operand_addr_y: ($) =>
      seq(field("addr", $._expression), ",", field("reg", $.register_y)),
    operand_indirect: ($) =>
      prec(
        1, // Higher precedence thant (expression)
        seq("(", field("addr", $._expression), ")"),
      ),
    operand_x_indirect: ($) =>
      prec(
        1, // Higher precedence thant (expression)
        seq(
          "(",
          field("addr", $._expression),
          ",",
          field("reg", $.register_x),
          ")",
        ),
      ),
    operand_indirect_y: ($) =>
      prec(
        1, // Higher precedence thant (expression)
        seq(
          "(",
          field("addr", $._expression),
          ")",
          ",",
          field("reg", $.register_y),
        ),
      ),

    // Registers
    register_a: (_) => make_case_insentive("a"),
    register_x: (_) => make_case_insentive("x"),
    register_y: (_) => make_case_insentive("y"),

    // Opcodes
    opcode_adc: (_) => make_opcode("adc"),
    opcode_and: (_) => make_opcode("and"),
    opcode_asl: (_) => make_opcode("asl"),
    opcode_bcc: (_) => make_opcode("bcc"),
    opcode_bcs: (_) => make_opcode("bcs"),
    opcode_beq: (_) => make_opcode("beq"),
    opcode_bit: (_) => make_opcode("bit"),
    opcode_bmi: (_) => make_opcode("bmi"),
    opcode_bne: (_) => make_opcode("bne"),
    opcode_bpl: (_) => make_opcode("bpl"),
    opcode_brk: (_) => make_opcode("brk"),
    opcode_bvc: (_) => make_opcode("bvc"),
    opcode_bvs: (_) => make_opcode("bvs"),
    opcode_clc: (_) => make_opcode("clc"),
    opcode_cld: (_) => make_opcode("cld"),
    opcode_cli: (_) => make_opcode("cli"),
    opcode_clv: (_) => make_opcode("clv"),
    opcode_cmp: (_) => make_opcode("cmp"),
    opcode_cpx: (_) => make_opcode("cpx"),
    opcode_cpy: (_) => make_opcode("cpy"),
    opcode_dec: (_) => make_opcode("dec"),
    opcode_dex: (_) => make_opcode("dex"),
    opcode_dey: (_) => make_opcode("dey"),
    opcode_eor: (_) => make_opcode("eor"),
    opcode_inc: (_) => make_opcode("inc"),
    opcode_inx: (_) => make_opcode("inx"),
    opcode_iny: (_) => make_opcode("iny"),
    opcode_jmp: (_) => make_opcode("jmp"),
    opcode_jsr: (_) => make_opcode("jsr"),
    opcode_lda: (_) => make_opcode("lda"),
    opcode_ldx: (_) => make_opcode("ldx"),
    opcode_ldy: (_) => make_opcode("ldy"),
    opcode_lsr: (_) => make_opcode("lsr"),
    opcode_nop: (_) => make_opcode("nop"),
    opcode_ora: (_) => make_opcode("ora"),
    opcode_pha: (_) => make_opcode("pha"),
    opcode_php: (_) => make_opcode("php"),
    opcode_pla: (_) => make_opcode("pla"),
    opcode_plp: (_) => make_opcode("plp"),
    opcode_rol: (_) => make_opcode("rol"),
    opcode_ror: (_) => make_opcode("ror"),
    opcode_rti: (_) => make_opcode("rti"),
    opcode_rts: (_) => make_opcode("rts"),
    opcode_sbc: (_) => make_opcode("sbc"),
    opcode_sec: (_) => make_opcode("sec"),
    opcode_sed: (_) => make_opcode("sed"),
    opcode_sei: (_) => make_opcode("sei"),
    opcode_sta: (_) => make_opcode("sta"),
    opcode_stx: (_) => make_opcode("stx"),
    opcode_sty: (_) => make_opcode("sty"),
    opcode_tax: (_) => make_opcode("tax"),
    opcode_tay: (_) => make_opcode("tay"),
    opcode_tsx: (_) => make_opcode("tsx"),
    opcode_txa: (_) => make_opcode("txa"),
    opcode_txs: (_) => make_opcode("txs"),
    opcode_tya: (_) => make_opcode("tya"),

    // Pseudo instructions
    _pseudo_inst: ($) =>
      choice(
        $.pseudo_inst_a16,
        $.pseudo_inst_a8,
        $.pseudo_inst_addr,
        $.pseudo_inst_align,
        $.pseudo_inst_asciiz,
        $.pseudo_inst_assert,
        $.pseudo_inst_autoimport,
        $.pseudo_inst_bankbytes,
        $.pseudo_inst_bss,
        $.pseudo_inst_byte,
        $.pseudo_inst_case,
        $.pseudo_inst_charmap,
        $.pseudo_inst_code,
        $.pseudo_inst_condes,
        $.pseudo_inst_constructor,
        $.pseudo_inst_data,
        $.pseudo_inst_dbyt,
        $.pseudo_inst_debuginfo,
        $.pseudo_inst_define,
        $.pseudo_inst_delmacro,
        $.pseudo_inst_destructor,
        $.pseudo_inst_dword,
        $.pseudo_inst_else,
        $.pseudo_inst_elseif,
        $.pseudo_inst_end,
        $.pseudo_inst_enum,
        $.pseudo_inst_error,
        $.pseudo_inst_exitmacro,
        $.pseudo_inst_export,
        $.pseudo_inst_exportzp,
        $.pseudo_inst_faraddr,
        $.pseudo_inst_fatal,
        $.pseudo_inst_feature,
        $.pseudo_inst_fileopt,
        $.pseudo_inst_forceimport,
        $.pseudo_inst_global,
        $.pseudo_inst_globalzp,
        $.pseudo_inst_hibytes,
        $.pseudo_inst_i16,
        $.pseudo_inst_i8,
        $.pseudo_inst_if,
        $.pseudo_inst_ifblank,
        $.pseudo_inst_ifconst,
        $.pseudo_inst_ifdef,
        $.pseudo_inst_ifnblank,
        $.pseudo_inst_ifndef,
        $.pseudo_inst_ifnref,
        $.pseudo_inst_ifp02,
        $.pseudo_inst_ifp02x,
        $.pseudo_inst_ifp45gs02,
        $.pseudo_inst_ifp816,
        $.pseudo_inst_ifp4510,
        $.pseudo_inst_ifp6280,
        $.pseudo_inst_ifpc02,
        $.pseudo_inst_ifpce02,
        $.pseudo_inst_ifpdtv,
        $.pseudo_inst_ifpm740,
        $.pseudo_inst_ifpsc02,
        $.pseudo_inst_ifpsweet16,
        $.pseudo_inst_ifref,
        $.pseudo_inst_ifpwc02,
        $.pseudo_inst_import,
        $.pseudo_inst_importzp,
        $.pseudo_inst_incbin,
        $.pseudo_inst_include,
        $.pseudo_inst_interruptor,
        $.pseudo_inst_list,
        $.pseudo_inst_listbytes,
        $.pseudo_inst_literal,
        $.pseudo_inst_lobytes,
        $.pseudo_inst_local,
        $.pseudo_inst_localchar,
        $.pseudo_inst_macpack,
        $.pseudo_inst_macro,
        $.pseudo_inst_org,
        $.pseudo_inst_out,
        $.pseudo_inst_p02,
        $.pseudo_inst_p02x,
        $.pseudo_inst_p4510,
        $.pseudo_inst_p45gs02,
        $.pseudo_inst_p6280,
        $.pseudo_inst_p816,
        $.pseudo_inst_pagelength,
        $.pseudo_inst_pc02,
        $.pseudo_inst_pce02,
        $.pseudo_inst_pdtv,
        $.pseudo_inst_pm740,
        $.pseudo_inst_popcharmap,
        $.pseudo_inst_popcpu,
        $.pseudo_inst_popseg,
        $.pseudo_inst_proc,
        $.pseudo_inst_psc02,
        $.pseudo_inst_psweet16,
        $.pseudo_inst_pushcharmap,
        $.pseudo_inst_pushcpu,
        $.pseudo_inst_pushseg,
        $.pseudo_inst_pwc02,
        $.pseudo_inst_referto,
        $.pseudo_inst_reloc,
        $.pseudo_inst_repeat,
        $.pseudo_inst_res,
        $.pseudo_inst_rodata,
        $.pseudo_inst_scope,
        $.pseudo_inst_segment,
        $.pseudo_inst_setcpu,
        $.pseudo_inst_smart,
        $.pseudo_inst_struct,
        $.pseudo_inst_tag,
        $.pseudo_inst_undefine,
        $.pseudo_inst_union,
        $.pseudo_inst_warning,
        $.pseudo_inst_word,
        $.pseudo_inst_zeropage,
      ),

    // .a16
    pseudo_inst_a16: ($) => $.dot_keyword_a16,

    // .a8
    pseudo_inst_a8: ($) => $.dot_keyword_a8,

    // .addr
    pseudo_inst_addr: ($) => seq($.dot_keyword_addr, $._pseudo_inst_addr_args),
    _pseudo_inst_addr_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_addr_args)),
      ),

    // .align
    pseudo_inst_align: ($) =>
      seq(
        $.dot_keyword_align,
        field("align", $._expression),
        optional(seq(",", field("fill", $._expression))),
      ),

    // .asciiz
    pseudo_inst_asciiz: ($) =>
      seq($.dot_keyword_asciiz, field("value", $._expression)),

    // .assert
    pseudo_inst_assert: ($) =>
      seq($.dot_keyword_assert, $._pseudo_inst_assert_arg1),
    _pseudo_inst_assert_arg1: ($) =>
      seq(
        field("cond", $._expression),
        optional(seq(",", $._pseudo_inst_assert_arg2)),
      ),
    _pseudo_inst_assert_arg2: ($) =>
      seq(
        field("action", $.pseudo_inst_assert_action),
        optional(seq(",", $._pseudo_inst_assert_arg3)),
      ),
    _pseudo_inst_assert_arg3: ($) => field("message", $._expression),
    pseudo_inst_assert_action: (_) =>
      choice(
        make_case_insentive("warning"),
        make_case_insentive("error"),
        make_case_insentive("ldwarning"),
        make_case_insentive("lderror"),
      ),

    // .autoimport
    pseudo_inst_autoimport: ($) =>
      seq(
        $.dot_keyword_autoimport,
        field("option", $.pseudo_inst_on_off_option),
      ),

    // .bankbytes
    pseudo_inst_bankbytes: ($) =>
      seq($.dot_keyword_bankbytes, $._pseudo_inst_bankbytes_args),
    _pseudo_inst_bankbytes_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_bankbytes_args)),
      ),

    // .bss
    pseudo_inst_bss: ($) => $.dot_keyword_bss,

    // .byte
    pseudo_inst_byte: ($) => seq($.dot_keyword_byte, $._pseudo_inst_byte_args),
    _pseudo_inst_byte_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_byte_args)),
      ),

    // .case
    pseudo_inst_case: ($) =>
      seq($.dot_keyword_case, field("option", $.pseudo_inst_on_off_option)),

    // .charmap
    pseudo_inst_charmap: ($) =>
      seq(
        $.dot_keyword_charmap,
        field("src", $._expression),
        ",",
        field("dst", $._expression),
      ),

    // .code
    pseudo_inst_code: ($) => $.dot_keyword_code,

    // .condes
    pseudo_inst_condes: ($) =>
      seq(
        $.dot_keyword_condes,
        field("name", $.symbol),
        ",",
        field("type", $.pseudo_inst_condes_type),
        optional(seq(",", field("priority", $._expression))),
      ),
    pseudo_inst_condes_type: ($) =>
      choice(
        make_case_insentive("constructor"),
        make_case_insentive("destructor"),
        make_case_insentive("interruptor"),
        $._expression,
      ),

    // .constructor
    pseudo_inst_constructor: ($) =>
      seq(
        $.dot_keyword_constructor,
        field("name", $.symbol),
        optional(seq(",", field("priority", $._expression))),
      ),

    // .data
    pseudo_inst_data: ($) => $.dot_keyword_data,

    // .dbyt
    pseudo_inst_dbyt: ($) => seq($.dot_keyword_dbyt, $._pseudo_inst_dbyt_args),
    _pseudo_inst_dbyt_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_dbyt_args)),
      ),

    // .debuginfo
    pseudo_inst_debuginfo: ($) =>
      seq(
        $.dot_keyword_debuginfo,
        field("option", $.pseudo_inst_on_off_option),
      ),

    // .define
    pseudo_inst_define: ($) =>
      choice($._pseudo_inst_define_normal, $._pseudo_inst_define_func),
    _pseudo_inst_define_normal: ($) =>
      seq(
        $.dot_keyword_define,
        field("name", $.symbol),
        field("body", optional($.pseudo_inst_define_body)),
      ),
    _pseudo_inst_define_func: ($) =>
      seq(
        $.dot_keyword_define,
        field("name", $.symbol),
        token.immediate("("),
        $._pseudo_inst_define_params,
        ")",
        field("body", optional($.pseudo_inst_define_body)),
      ),
    _pseudo_inst_define_params: ($) =>
      seq(
        field("param", $.symbol),
        optional(seq(",", $._pseudo_inst_define_params)),
      ),
    pseudo_inst_define_body: (_) => token(prec(-1, /.*/)),

    // .delmacro
    pseudo_inst_delmacro: ($) =>
      seq($.dot_keyword_delmacro, field("name", $.symbol)),

    // .destructor
    pseudo_inst_destructor: ($) =>
      seq(
        $.dot_keyword_destructor,
        field("name", $.symbol),
        optional(seq(",", field("priority", $._expression))),
      ),

    // .dword
    pseudo_inst_dword: ($) =>
      seq($.dot_keyword_dword, $._pseudo_inst_dword_args),
    _pseudo_inst_dword_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_dword_args)),
      ),

    // .end
    pseudo_inst_end: ($) => $.dot_keyword_end,

    // .enum
    pseudo_inst_enum: ($) =>
      seq(
        $.dot_keyword_enum,
        field("name", $.symbol),
        token(prec(1, /\r?\n/)),
        optional($._pseudo_inst_enum_fields),
        $.dot_keyword_endenum,
      ),
    _pseudo_inst_enum_fields: ($) =>
      seq(
        field("field", $.pseudo_inst_enum_field),
        token(prec(1, /\r?\n/)),
        optional($._pseudo_inst_enum_fields),
      ),
    pseudo_inst_enum_field: ($) =>
      seq(
        field("name", $.symbol),
        optional(field("value", seq("=", $._expression))),
      ),

    // .error
    pseudo_inst_error: ($) =>
      seq($.dot_keyword_error, field("message", $._expression)),

    // .exitmacro
    pseudo_inst_exitmacro: ($) => $.dot_keyword_exitmacro,

    // .export
    pseudo_inst_export: ($) =>
      seq($.dot_keyword_export, $._pseudo_inst_export_symbols),
    _pseudo_inst_export_symbols: ($) =>
      seq(
        field("symbol", $.pseudo_inst_export_symbol),
        optional(seq(",", $._pseudo_inst_export_symbols)),
      ),
    pseudo_inst_export_symbol: ($) =>
      seq(
        field("name", $.symbol),
        optional($._pseudo_inst_export_symbol_opt1),
        optional($._pseudo_inst_export_symbol_opt2),
      ),
    _pseudo_inst_export_symbol_opt1: ($) =>
      seq(token(":"), field("spec", $.pseudo_inst_addr_spec)),
    _pseudo_inst_export_symbol_opt2: ($) =>
      seq(choice("=", ":="), field("expr", $._expression)),

    // .exportzp
    pseudo_inst_exportzp: ($) =>
      seq($.dot_keyword_exportzp, $._pseudo_inst_exportzp_symbols),
    _pseudo_inst_exportzp_symbols: ($) =>
      seq(
        field("symbol", $.pseudo_inst_exportzp_symbol),
        optional(seq(",", $._pseudo_inst_exportzp_symbols)),
      ),
    pseudo_inst_exportzp_symbol: ($) =>
      seq(
        field("name", $.symbol),
        optional($._pseudo_inst_exportzp_symbol_opt1),
        optional($._pseudo_inst_exportzp_symbol_opt2),
      ),
    _pseudo_inst_exportzp_symbol_opt1: ($) =>
      seq(token(":"), field("spec", $.pseudo_inst_addr_spec)),
    _pseudo_inst_exportzp_symbol_opt2: ($) =>
      seq(choice("=", ":="), field("expr", $._expression)),

    // .faraddr
    pseudo_inst_faraddr: ($) =>
      seq($.dot_keyword_faraddr, $._pseudo_inst_faraddr_args),
    _pseudo_inst_faraddr_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_faraddr_args)),
      ),

    // .fatal
    pseudo_inst_fatal: ($) =>
      seq($.dot_keyword_fatal, field("message", $._expression)),

    // .feature
    pseudo_inst_feature: ($) =>
      seq($.dot_keyword_feature, $._pseudo_inst_feature_items),
    _pseudo_inst_feature_items: ($) =>
      seq(
        field("item", $.pseudo_inst_feature_item),
        optional(seq(",", $._pseudo_inst_feature_items)),
      ),
    pseudo_inst_feature_item: ($) =>
      seq(
        field("name", $.pseudo_inst_feature_name),
        optional(field("option", $.pseudo_inst_on_off_option)),
      ),
    pseudo_inst_feature_name: (_) =>
      choice(
        make_case_insentive("at_in_identifiers"),
        make_case_insentive("bracket_as_indirect"),
        make_case_insentive("c_comment"),
        make_case_insentive("dollar_in_identifiers"),
        make_case_insentive("dollar_is_pc"),
        make_case_insentive("force_range"),
        make_case_insentive("labels_without_colons"),
        make_case_insentive("leading_dot_in_identifiers"),
        make_case_insentive("line_continuations"),
        make_case_insentive("long_jsr_jmp_rts"),
        make_case_insentive("loose_char_term"),
        make_case_insentive("loose_string_term"),
        make_case_insentive("missing_char_term"),
        make_case_insentive("org_per_seg"),
        make_case_insentive("pc_assignment"),
        make_case_insentive("string_escapes"),
        make_case_insentive("ubiquitous_idents"),
        make_case_insentive("underline_in_numbers"),
      ),

    // .fileopt
    pseudo_inst_fileopt: ($) =>
      seq(
        $.dot_keyword_fileopt,
        field("name", $.pseudo_inst_fileopt_name),
        ",",
        field("content", $._expression),
      ),
    pseudo_inst_fileopt_name: (_) =>
      choice(
        make_case_insentive("author"),
        make_case_insentive("comment"),
        make_case_insentive("compiler"),
      ),

    // .forceimport
    pseudo_inst_forceimport: ($) =>
      seq($.dot_keyword_forceimport, $._pseudo_inst_forceimport_args),
    _pseudo_inst_forceimport_args: ($) =>
      seq(
        field("symbol", $.pseudo_inst_forceimport_symbol),
        optional(seq(",", $._pseudo_inst_forceimport_args)),
      ),
    pseudo_inst_forceimport_symbol: ($) => field("name", $.symbol),

    // .global
    pseudo_inst_global: ($) =>
      seq($.dot_keyword_global, $._pseudo_inst_global_args),
    _pseudo_inst_global_args: ($) =>
      seq(
        field("symbol", $.symbol),
        optional(seq(",", $._pseudo_inst_global_args)),
      ),

    // .globalzp
    pseudo_inst_globalzp: ($) =>
      seq($.dot_keyword_globalzp, $._pseudo_inst_globalzp_args),
    _pseudo_inst_globalzp_args: ($) =>
      seq(
        field("symbol", $.symbol),
        optional(seq(",", $._pseudo_inst_globalzp_args)),
      ),

    // .hibytes
    pseudo_inst_hibytes: ($) =>
      seq($.dot_keyword_hibytes, $._pseudo_inst_hibytes_args),
    _pseudo_inst_hibytes_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_hibytes_args)),
      ),

    // .i16
    pseudo_inst_i16: ($) => $.dot_keyword_i16,

    // .i8
    pseudo_inst_i8: ($) => $.dot_keyword_i8,

    // common rule after if-like pseudo instruction
    _pseudo_inst_if_common: ($) =>
      seq(
        token(prec(1, /\r?\n/)),
        optional(field("block", $.pseudo_inst_block)),
        repeat(field("alt", choice($.pseudo_inst_else, $.pseudo_inst_elseif))),
        $.dot_keyword_endif,
      ),

    // .else
    pseudo_inst_else: ($) =>
      prec.left(
        seq(
          $.dot_keyword_else,
          token(prec(1, /\r?\n/)),
          optional(field("block", $.pseudo_inst_block)),
        ),
      ),

    // .elseif
    pseudo_inst_elseif: ($) =>
      prec.left(
        seq(
          $.dot_keyword_elseif,
          field("cond", $._expression),
          token(prec(1, /\r?\n/)),
          optional(field("block", $.pseudo_inst_block)),
        ),
      ),

    // .if
    pseudo_inst_if: ($) =>
      seq(
        $.dot_keyword_if,
        field("cond", $._expression),
        $._pseudo_inst_if_common,
      ),

    // .ifblank
    pseudo_inst_ifblank: ($) =>
      seq(
        $.dot_keyword_ifblank,
        field("tokens", optional($.pseudo_inst_ifblank_tokens)),
        $._pseudo_inst_if_common,
      ),
    pseudo_inst_ifblank_tokens: (_) => token(prec(-1, /.*/)),

    // .ifconst
    pseudo_inst_ifconst: ($) =>
      seq(
        $.dot_keyword_ifconst,
        field("expr", $._expression),
        $._pseudo_inst_if_common,
      ),

    // .ifdef
    pseudo_inst_ifdef: ($) =>
      seq(
        $.dot_keyword_ifdef,
        field("symbol", $.symbol),
        $._pseudo_inst_if_common,
      ),

    // .ifnblank
    pseudo_inst_ifnblank: ($) =>
      seq(
        $.dot_keyword_ifnblank,
        field("tokens", optional($.pseudo_inst_ifnblank_tokens)),
        $._pseudo_inst_if_common,
      ),
    pseudo_inst_ifnblank_tokens: (_) => token(prec(-1, /.*/)),

    // .ifndef
    pseudo_inst_ifndef: ($) =>
      seq(
        $.dot_keyword_ifndef,
        field("symbol", $.symbol),
        $._pseudo_inst_if_common,
      ),

    // .ifnref
    pseudo_inst_ifnref: ($) =>
      seq(
        $.dot_keyword_ifnref,
        field("symbol", $.symbol),
        $._pseudo_inst_if_common,
      ),

    // .ifp02
    pseudo_inst_ifp02: ($) =>
      seq($.dot_keyword_ifp02, $._pseudo_inst_if_common),

    // .ifp02x
    pseudo_inst_ifp02x: ($) =>
      seq($.dot_keyword_ifp02x, $._pseudo_inst_if_common),

    // .ifp45gs02
    pseudo_inst_ifp45gs02: ($) =>
      seq($.dot_keyword_ifp45gs02, $._pseudo_inst_if_common),

    // .ifp816
    pseudo_inst_ifp816: ($) =>
      seq($.dot_keyword_ifp816, $._pseudo_inst_if_common),

    // .ifp4510
    pseudo_inst_ifp4510: ($) =>
      seq($.dot_keyword_ifp4510, $._pseudo_inst_if_common),

    // .ifp6280
    pseudo_inst_ifp6280: ($) =>
      seq($.dot_keyword_ifp6280, $._pseudo_inst_if_common),

    // .ifpc02
    pseudo_inst_ifpc02: ($) =>
      seq($.dot_keyword_ifpc02, $._pseudo_inst_if_common),

    // .ifpce02
    pseudo_inst_ifpce02: ($) =>
      seq($.dot_keyword_ifpce02, $._pseudo_inst_if_common),

    // .ifpdtv
    pseudo_inst_ifpdtv: ($) =>
      seq($.dot_keyword_ifpdtv, $._pseudo_inst_if_common),

    // .ifpm740
    pseudo_inst_ifpm740: ($) =>
      seq($.dot_keyword_ifpm740, $._pseudo_inst_if_common),

    // .ifpsc02
    pseudo_inst_ifpsc02: ($) =>
      seq($.dot_keyword_ifpsc02, $._pseudo_inst_if_common),

    // .ifpsweet16
    pseudo_inst_ifpsweet16: ($) =>
      seq($.dot_keyword_ifpsweet16, $._pseudo_inst_if_common),

    // .ifref
    pseudo_inst_ifref: ($) =>
      seq(
        $.dot_keyword_ifref,
        field("symbol", $.symbol),
        $._pseudo_inst_if_common,
      ),

    // .ifpwc02
    pseudo_inst_ifpwc02: ($) =>
      seq($.dot_keyword_ifpwc02, $._pseudo_inst_if_common),

    // .import
    pseudo_inst_import: ($) =>
      seq($.dot_keyword_import, $._pseudo_inst_import_symbols),
    _pseudo_inst_import_symbols: ($) =>
      seq(
        field("symbol", $.pseudo_inst_import_symbol),
        optional(seq(",", $._pseudo_inst_import_symbols)),
      ),
    pseudo_inst_import_symbol: ($) =>
      seq(
        field("name", $.symbol),
        optional(seq(":", field("spec", $.pseudo_inst_addr_spec))),
      ),

    // .importzp
    pseudo_inst_importzp: ($) =>
      seq($.dot_keyword_importzp, $._pseudo_inst_importzp_symbols),
    _pseudo_inst_importzp_symbols: ($) =>
      seq(
        field("symbol", $.pseudo_inst_importzp_symbol),
        optional(seq(",", $._pseudo_inst_importzp_symbols)),
      ),
    pseudo_inst_importzp_symbol: ($) =>
      seq(
        field("name", $.symbol),
        optional(seq(":", field("spec", $.pseudo_inst_addr_spec))),
      ),

    // .incbin
    pseudo_inst_incbin: ($) =>
      seq(
        $.dot_keyword_incbin,
        field("file", $._expression),
        optional(seq(",", $._pseudo_inst_incbin_arg1)),
      ),
    _pseudo_inst_incbin_arg1: ($) =>
      seq(
        field("offset", $._expression),
        optional(seq(",", $._pseudo_inst_incbin_arg2)),
      ),
    _pseudo_inst_incbin_arg2: ($) => field("size", $._expression),

    // .include
    pseudo_inst_include: ($) =>
      seq($.dot_keyword_include, field("file", $._expression)),

    // .interruptor
    pseudo_inst_interruptor: ($) =>
      seq(
        $.dot_keyword_interruptor,
        field("name", $.symbol),
        optional(seq(",", field("priority", $._expression))),
      ),

    // .list
    pseudo_inst_list: ($) =>
      seq($.dot_keyword_list, field("option", $.pseudo_inst_on_off_option)),

    // .listbytes
    pseudo_inst_listbytes: ($) =>
      seq(
        $.dot_keyword_listbytes,
        field("bytes", choice($._expression, $.pseudo_inst_unlimited)),
      ),

    // .literal
    pseudo_inst_literal: ($) =>
      seq($.dot_keyword_literal, $._pseudo_inst_literal_args),
    _pseudo_inst_literal_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_literal_args)),
      ),

    // .lobytes
    pseudo_inst_lobytes: ($) =>
      seq($.dot_keyword_lobytes, $._pseudo_inst_lobytes_args),
    _pseudo_inst_lobytes_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_lobytes_args)),
      ),

    // .local
    pseudo_inst_local: ($) =>
      seq($.dot_keyword_local, $._pseudo_inst_local_args),
    _pseudo_inst_local_args: ($) =>
      seq(
        field("symbol", $.symbol),
        optional(seq(",", $._pseudo_inst_local_args)),
      ),

    // .localchar
    pseudo_inst_localchar: ($) =>
      seq($.dot_keyword_localchar, field("char", $.char)),

    // .macpack
    pseudo_inst_macpack: ($) =>
      seq(
        $.dot_keyword_macpack,
        field("package", $.pseudo_inst_macpack_package),
      ),
    pseudo_inst_macpack_package: (_) =>
      choice(
        make_case_insentive("atari"),
        make_case_insentive("cbm"),
        make_case_insentive("generic"),
        make_case_insentive("longbranch"),
        make_case_insentive("apple2"),
        make_case_insentive("module"),
      ),

    // .macro
    pseudo_inst_macro: ($) =>
      seq(
        $.dot_keyword_macro,
        field("name", $.symbol),
        optional($._pseudo_inst_macro_params),
        token(prec(1, /\r?\n/)),
        optional(field("block", $.pseudo_inst_block)),
        $.dot_keyword_endmacro,
      ),
    _pseudo_inst_macro_params: ($) =>
      seq(
        field("param", $.symbol),
        optional(seq(",", $._pseudo_inst_macro_params)),
      ),

    // .org
    pseudo_inst_org: ($) => seq($.dot_keyword_org, field("pc", $._expression)),

    // .out
    pseudo_inst_out: ($) =>
      seq($.dot_keyword_out, field("message", $._expression)),

    // .p02
    pseudo_inst_p02: ($) => $.dot_keyword_p02,

    // .p02x
    pseudo_inst_p02x: ($) => $.dot_keyword_p02x,

    // .p4510
    pseudo_inst_p4510: ($) => $.dot_keyword_p4510,

    // .p45gs02
    pseudo_inst_p45gs02: ($) => $.dot_keyword_p45gs02,

    // .p6280
    pseudo_inst_p6280: ($) => $.dot_keyword_p6280,

    // .p816
    pseudo_inst_p816: ($) => $.dot_keyword_p816,

    // .pagelength
    pseudo_inst_pagelength: ($) =>
      seq(
        $.dot_keyword_pagelength,
        field("length", choice($._expression, $.pseudo_inst_unlimited)),
      ),

    // .pc02
    pseudo_inst_pc02: ($) => $.dot_keyword_pc02,

    // .pce02
    pseudo_inst_pce02: ($) => $.dot_keyword_pce02,

    // .pdtv
    pseudo_inst_pdtv: ($) => $.dot_keyword_pdtv,

    // .pm740
    pseudo_inst_pm740: ($) => $.dot_keyword_pm740,

    // .popcharmap
    pseudo_inst_popcharmap: ($) => $.dot_keyword_popcharmap,

    // .popcpu
    pseudo_inst_popcpu: ($) => $.dot_keyword_popcpu,

    // .popseg
    pseudo_inst_popseg: ($) => $.dot_keyword_popseg,

    // .proc
    pseudo_inst_proc: ($) =>
      seq(
        $.dot_keyword_proc,
        field("symbol", $.pseudo_inst_proc_symbol),
        token(prec(1, /\r?\n/)),
        optional(field("block", $.pseudo_inst_block)),
        $.dot_keyword_endproc,
      ),
    pseudo_inst_proc_symbol: ($) =>
      seq(
        field("name", $.symbol),
        optional(seq(":", field("spec", $.pseudo_inst_addr_spec))),
      ),

    // .psc02
    pseudo_inst_psc02: ($) => $.dot_keyword_psc02,

    // .psweet16
    pseudo_inst_psweet16: ($) => $.dot_keyword_psweet16,

    // .pushcharmap
    pseudo_inst_pushcharmap: ($) => $.dot_keyword_pushcharmap,

    // .pushcpu
    pseudo_inst_pushcpu: ($) => $.dot_keyword_pushcpu,

    // .pushseg
    pseudo_inst_pushseg: ($) => $.dot_keyword_pushseg,

    // .pwc02
    pseudo_inst_pwc02: ($) => $.dot_keyword_pwc02,

    // .referto
    pseudo_inst_referto: ($) =>
      seq($.dot_keyword_referto, field("symbol", $.symbol)),

    // .reloc
    pseudo_inst_reloc: ($) => $.dot_keyword_reloc,

    // .repeat
    pseudo_inst_repeat: ($) =>
      seq(
        $.dot_keyword_repeat,
        field("count", $._expression),
        optional(seq(",", field("variable", $.symbol))),
        optional(field("block", $.pseudo_inst_block)),
        $.dot_keyword_endrepeat,
      ),

    // .res
    pseudo_inst_res: ($) =>
      seq(
        $.dot_keyword_res,
        field("bytes", $._expression),
        optional(seq(",", field("fill", $._expression))),
      ),

    // .rodata
    pseudo_inst_rodata: ($) => $.dot_keyword_rodata,

    // .scope
    pseudo_inst_scope: ($) =>
      seq(
        $.dot_keyword_scope,
        optional(field("symbol", $.pseudo_inst_scope_symbol)),
        token(prec(1, /\r?\n/)),
        optional(field("block", $.pseudo_inst_block)),
        $.dot_keyword_endscope,
      ),
    pseudo_inst_scope_symbol: ($) =>
      seq(
        field("name", $.symbol),
        optional(seq(":", field("spec", $.pseudo_inst_addr_spec))),
      ),

    // .segment
    pseudo_inst_segment: ($) =>
      seq(
        $.dot_keyword_segment,
        field("name", $._expression),
        optional(seq(":", field("spec", $.pseudo_inst_addr_spec))),
      ),

    // .setcpu
    pseudo_inst_setcpu: ($) =>
      seq($.dot_keyword_setcpu, field("cpu", $._expression)),

    // .smart
    pseudo_inst_smart: ($) =>
      seq($.dot_keyword_smart, field("option", $.pseudo_inst_on_off_option)),

    // .struct
    pseudo_inst_struct: ($) =>
      seq(
        $.dot_keyword_struct,
        field("name", $.symbol),
        token(prec(1, /\r?\n/)),
        optional($._pseudo_inst_struct_or_union_fields),
        $.dot_keyword_endstruct,
      ),

    // .tag
    pseudo_inst_tag: ($) => seq($.dot_keyword_tag, field("name", $.symbol)),

    // .undefine
    pseudo_inst_undefine: ($) =>
      seq($.dot_keyword_undefine, field("name", $.symbol)),

    // .union
    pseudo_inst_union: ($) =>
      seq(
        $.dot_keyword_union,
        field("name", $.symbol),
        token(prec(1, /\r?\n/)),
        optional($._pseudo_inst_struct_or_union_fields),
        $.dot_keyword_endunion,
      ),

    // .warning
    pseudo_inst_warning: ($) =>
      seq($.dot_keyword_warning, field("message", $._expression)),

    // .word
    pseudo_inst_word: ($) => seq($.dot_keyword_word, $._pseudo_inst_word_args),
    _pseudo_inst_word_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_inst_word_args)),
      ),

    // .zeropage
    pseudo_inst_zeropage: ($) => $.dot_keyword_zeropage,

    // Struct or Union fields
    _pseudo_inst_struct_or_union_fields: ($) =>
      seq(
        field("field", $.pseudo_inst_struct_or_union_field),
        token(prec(1, /\r?\n/)),
        optional($._pseudo_inst_struct_or_union_fields),
      ),
    pseudo_inst_struct_or_union_field: ($) =>
      seq(
        optional(field("name", $.symbol)),
        field("alloc", $._pseudo_inst_struct_or_union_field_alloc),
      ),
    _pseudo_inst_struct_or_union_field_alloc: ($) =>
      choice(
        $.pseudo_inst_struct_or_union_field_alloc_byte,
        $.pseudo_inst_struct_or_union_field_alloc_res,
        $.pseudo_inst_struct_or_union_field_alloc_dbyt,
        $.pseudo_inst_struct_or_union_field_alloc_word,
        $.pseudo_inst_struct_or_union_field_alloc_addr,
        $.pseudo_inst_struct_or_union_field_alloc_faraddr,
        $.pseudo_inst_struct_or_union_field_alloc_dword,
        $.pseudo_inst_struct_or_union_field_alloc_tag,
        $.pseudo_inst_struct_or_union_field_alloc_org,
        $.pseudo_inst_struct,
        $.pseudo_inst_union,
      ),
    pseudo_inst_struct_or_union_field_alloc_byte: ($) =>
      seq($.dot_keyword_byte, optional(field("size", $._expression))),
    pseudo_inst_struct_or_union_field_alloc_res: ($) =>
      seq($.dot_keyword_res, field("size", $._expression)),
    pseudo_inst_struct_or_union_field_alloc_dbyt: ($) =>
      seq($.dot_keyword_dbyt, optional(field("size", $._expression))),
    pseudo_inst_struct_or_union_field_alloc_word: ($) =>
      seq($.dot_keyword_word, optional(field("size", $._expression))),
    pseudo_inst_struct_or_union_field_alloc_addr: ($) =>
      seq($.dot_keyword_addr, optional(field("size", $._expression))),
    pseudo_inst_struct_or_union_field_alloc_faraddr: ($) =>
      seq($.dot_keyword_faraddr, optional(field("size", $._expression))),
    pseudo_inst_struct_or_union_field_alloc_dword: ($) =>
      seq($.dot_keyword_dword, optional(field("size", $._expression))),
    pseudo_inst_struct_or_union_field_alloc_tag: ($) =>
      seq($.dot_keyword_tag, optional(field("name", $.symbol))),
    pseudo_inst_struct_or_union_field_alloc_org: ($) =>
      seq($.dot_keyword_org, optional(field("offset", $._expression))),

    // Source lines inside block
    pseudo_inst_block: ($) => $._pseudo_inst_block_lines,
    _pseudo_inst_block_lines: ($) =>
      prec.left(
        seq(field("line", $.source_line), optional($._pseudo_inst_block_lines)),
      ),

    // ON/OFF option
    pseudo_inst_on_off_option: (_) =>
      choice("+", "-", make_case_insentive("on"), make_case_insentive("off")),

    // Address specification
    pseudo_inst_addr_spec: (_) =>
      choice(
        make_case_insentive("abs"),
        make_case_insentive("absolute"),
        make_case_insentive("default"),
        make_case_insentive("direct"),
        make_case_insentive("dword"),
        make_case_insentive("far"),
        make_case_insentive("long"),
        make_case_insentive("near"),
        make_case_insentive("zeropage"),
        make_case_insentive("zp"),
      ),

    // Unlimited in IntArg
    pseudo_inst_unlimited: (_) => make_case_insentive("unlimited"),

    // Expression
    _expression: ($) =>
      choice($.unary_expression, $.binary_expression, $._primary_expression),

    // Unary expression
    unary_expression: ($) => {
      const table = [
        { op: "+", prec: PREC.OP.UNARY.POSITIVE },
        { op: "-", prec: PREC.OP.UNARY.NEGATIVE },
        { op: "~", prec: PREC.OP.UNARY.BITNOT },
        { op: "<", prec: PREC.OP.UNARY.LOBYTE },
        { op: ">", prec: PREC.OP.UNARY.HIBYTE },
        { op: "^", prec: PREC.OP.UNARY.BANKBYTE },
        { op: "!", prec: PREC.OP.UNARY.NOT },
      ];
      return choice(
        ...table.map((item) => {
          return prec.right(
            item.prec,
            seq(field("op", item.op), field("expr", $._expression)),
          );
        }),
      );
    },

    // Binary expression
    binary_expression: ($) => {
      const table = [
        { op: "*", prec: PREC.OP.BINARY.MUL },
        { op: "/", prec: PREC.OP.BINARY.DIV },
        { op: $.dot_keyword_mod, prec: PREC.OP.BINARY.MOD },
        { op: "&", prec: PREC.OP.BINARY.BITAND },
        { op: $.dot_keyword_bitand, prec: PREC.OP.BINARY.BITAND },
        { op: "^", prec: PREC.OP.BINARY.BITXOR },
        { op: $.dot_keyword_bitxor, prec: PREC.OP.BINARY.BITXOR },
        { op: "<<", prec: PREC.OP.BINARY.LSHIFT },
        { op: $.dot_keyword_shl, prec: PREC.OP.BINARY.LSHIFT },
        { op: ">>", prec: PREC.OP.BINARY.RSHIFT },
        { op: $.dot_keyword_shr, prec: PREC.OP.BINARY.RSHIFT },
        { op: "+", prec: PREC.OP.BINARY.ADD },
        { op: "-", prec: PREC.OP.BINARY.SUB },
        { op: "|", prec: PREC.OP.BINARY.BITOR },
        { op: $.dot_keyword_bitor, prec: PREC.OP.BINARY.BITOR },
        { op: "=", prec: PREC.OP.BINARY.EQ },
        { op: "<>", prec: PREC.OP.BINARY.NE },
        { op: "<", prec: PREC.OP.BINARY.LT },
        { op: ">", prec: PREC.OP.BINARY.GT },
        { op: "<=", prec: PREC.OP.BINARY.LE },
        { op: ">=", prec: PREC.OP.BINARY.GE },
        { op: "&&", prec: PREC.OP.BINARY.AND },
        { op: $.dot_keyword_and, prec: PREC.OP.BINARY.AND },
        { op: "||", prec: PREC.OP.BINARY.OR },
        { op: $.dot_keyword_or, prec: PREC.OP.BINARY.OR },
      ];
      return choice(
        ...table.map((item) => {
          return prec.left(
            item.prec,
            seq(
              field("lhs", $._expression),
              field("op", item.op),
              field("rhs", $._expression),
            ),
          );
        }),
      );
    },

    _primary_expression: ($) => {
      const table = [
        { rule: $.group_expression, prec: PREC.PRIMARY.GROUP },
        { rule: $.pseudo_var, prec: PREC.PRIMARY.PSEUDO_VAR },
        { rule: $._pseudo_func, prec: PREC.PRIMARY.PSEUDO_FUN },
        { rule: $.symbol, prec: PREC.PRIMARY.SYMBOL },
        { rule: $.number, prec: PREC.PRIMARY.NUMBER },
        { rule: $.string, prec: PREC.PRIMARY.STRING },
        { rule: $.char, prec: PREC.PRIMARY.CHAR },
        { rule: $.member, prec: PREC.PRIMARY.MEMBER },
        { rule: $.macro_call, prec: PREC.PRIMARY.MACRO_CALL },
        { rule: $.local_label_literal, prec: PREC.PRIMARY.LOCAL_LABEL },
        { rule: $.unnamed_label_literal, prec: PREC.PRIMARY.UNNAMED_LABEL },
      ];
      return choice(
        ...table.map((item) => {
          return prec(item.prec, item.rule);
        }),
      );
    },

    // Grouping
    group_expression: ($) => seq("(", $._expression, ")"),

    // Pseudo variables
    pseudo_var: (_) =>
      choice(
        make_case_insentive("*"),
        make_case_insentive(".asize"),
        make_case_insentive(".cpu"),
        make_case_insentive(".isize"),
        make_case_insentive(".paramcount"),
        make_case_insentive(".time"),
        make_case_insentive(".version"),
      ),

    // Pseudo functions
    _pseudo_func: ($) =>
      choice(
        $.pseudo_func_addrsize,
        $.pseudo_func_bank,
        $.pseudo_func_bankbyte,
        $.pseudo_func_blank,
        $.pseudo_func_capability,
        $.pseudo_func_concat,
        $.pseudo_func_const,
        $.pseudo_func_defined,
        $.pseudo_func_definedmacro,
        $.pseudo_func_hibyte,
        $.pseudo_func_hiword,
        $.pseudo_func_ident,
        $.pseudo_func_ismnemonic,
        $.pseudo_func_left,
        $.pseudo_func_lobyte,
        $.pseudo_func_loword,
        $.pseudo_func_match,
        $.pseudo_func_max,
        $.pseudo_func_mid,
        $.pseudo_func_min,
        $.pseudo_func_referenced,
        $.pseudo_func_right,
        $.pseudo_func_sizeof,
        $.pseudo_func_sprintf,
        $.pseudo_func_start,
        $.pseudo_func_string,
        $.pseudo_func_strlen,
        $.pseudo_func_tcount,
        $.pseudo_func_xmatch,
      ),

    // .addrsize
    pseudo_func_addrsize: ($) =>
      seq($.dot_keyword_addrsize, "(", field("symbol", $.symbol), ")"),

    // .bank
    pseudo_func_bank: ($) =>
      seq($.dot_keyword_bank, "(", field("symbol", $.symbol), ")"),

    // .bankbyte
    pseudo_func_bankbyte: ($) =>
      seq($.dot_keyword_bankbyte, "(", field("expr", $._expression), ")"),

    // .blank
    pseudo_func_blank: ($) =>
      seq(
        $.dot_keyword_blank,
        "(",
        field("tokens", $.pseudo_func_blank_tokens),
        ")",
      ),
    pseudo_func_blank_tokens: (_) => /[^\)]*/,

    // .capability
    pseudo_func_capability: ($) =>
      seq(
        $.dot_keyword_capability,
        "(",
        optional($._pseudo_func_capability_args),
        ")",
      ),
    _pseudo_func_capability_args: ($) =>
      seq(
        field("name", $.pseudo_func_capability_name),
        optional(seq(",", $._pseudo_func_capability_args)),
      ),
    pseudo_func_capability_name: (_) =>
      choice(
        make_case_insentive("cpu_has_bitimm"),
        make_case_insentive("cpu_has_brab"),
        make_case_insentive("cpu_has_ina"),
        make_case_insentive("cpu_has_pushxy"),
        make_case_insentive("cpu_has_zpind"),
        make_case_insentive("cpu_has_stz"),
      ),

    // .concat
    pseudo_func_concat: ($) =>
      seq($.dot_keyword_concat, "(", optional($._pseudo_func_concat_args), ")"),
    _pseudo_func_concat_args: ($) =>
      seq(
        field("value", $._expression),
        optional(seq(",", $._pseudo_func_concat_args)),
      ),

    // .const
    pseudo_func_const: ($) =>
      seq($.dot_keyword_const, "(", field("expr", $._expression), ")"),

    // .defined
    pseudo_func_defined: ($) =>
      seq($.dot_keyword_defined, "(", field("name", $.symbol), ")"),

    // .definedmacro
    pseudo_func_definedmacro: ($) =>
      seq($.dot_keyword_definedmacro, "(", field("name", $.symbol), ")"),

    // .hibyte
    pseudo_func_hibyte: ($) =>
      seq($.dot_keyword_hibyte, "(", field("expr", $._expression), ")"),

    // .hiword
    pseudo_func_hiword: ($) =>
      seq($.dot_keyword_hiword, "(", field("expr", $._expression), ")"),

    // .ident
    pseudo_func_ident: ($) =>
      seq($.dot_keyword_ident, "(", field("expr", $._expression), ")"),

    // .ismnemonic
    pseudo_func_ismnemonic: ($) =>
      seq($.dot_keyword_ismnemonic, "(", field("name", $.symbol), ")"),

    // .left
    pseudo_func_left: ($) =>
      seq(
        $.dot_keyword_left,
        "(",
        field("count", $._expression),
        ",",
        field("tokens", $._pseudo_func_left_tokens),
        ")",
      ),
    _pseudo_func_left_tokens: ($) =>
      choice(
        $.pseudo_func_left,
        $.pseudo_func_right,
        seq("{", $.pseudo_func_left_tokens_enclosed, "}"),
        $.pseudo_func_left_tokens_raw,
      ),
    pseudo_func_left_tokens_enclosed: (_) => token(prec(-1, /[^\}]*/)),
    pseudo_func_left_tokens_raw: (_) => token(prec(-1, /[^\)]*/)),

    // .lobyte
    pseudo_func_lobyte: ($) =>
      seq($.dot_keyword_lobyte, "(", field("expr", $._expression), ")"),

    // .loword
    pseudo_func_loword: ($) =>
      seq($.dot_keyword_loword, "(", field("expr", $._expression), ")"),

    // .match
    pseudo_func_match: ($) =>
      seq(
        $.dot_keyword_match,
        "(",
        field("lhs", $._pseudo_func_match_lhs),
        ",",
        field("rhs", $._pseudo_func_match_rhs),
        ")",
      ),
    _pseudo_func_match_lhs: ($) =>
      choice(
        $.pseudo_func_left,
        $.pseudo_func_right,
        $.pseudo_func_match_lhs_raw,
        seq("{", $.pseudo_func_match_lhs_enclosed, "}"),
      ),
    pseudo_func_match_lhs_raw: (_) => token(prec(-1, /[^,]*/)),
    pseudo_func_match_lhs_enclosed: (_) => token(prec(-1, /[^}]*/)),
    _pseudo_func_match_rhs: ($) =>
      choice(
        $.pseudo_func_left,
        $.pseudo_func_right,
        $.pseudo_func_match_rhs_raw,
        seq("{", $.pseudo_func_match_rhs_enclosed, "}"),
      ),
    pseudo_func_match_rhs_raw: (_) => token(prec(-1, /[^)]*/)),
    pseudo_func_match_rhs_enclosed: (_) => token(prec(-1, /[^}]*/)),

    // .max
    pseudo_func_max: ($) =>
      seq(
        $.dot_keyword_max,
        "(",
        field("lhs", $._expression),
        ",",
        field("rhs", $._expression),
        ")",
      ),

    // .mid
    pseudo_func_mid: ($) =>
      seq(
        $.dot_keyword_mid,
        "(",
        field("start", $._expression),
        ",",
        field("end", $._expression),
        ",",
        field("tokens", $._pseudo_func_mid_tokens),
        ")",
      ),
    _pseudo_func_mid_tokens: ($) =>
      choice(
        $.pseudo_func_mid_tokens_raw,
        seq("{", $.pseudo_func_mid_tokens_enclosed, "}"),
      ),

    // .min
    pseudo_func_min: ($) =>
      seq(
        $.dot_keyword_min,
        "(",
        field("lhs", $._expression),
        ",",
        field("rhs", $._expression),
        ")",
      ),
    pseudo_func_mid_tokens_enclosed: (_) => token(prec(-1, /[^\}]*/)),
    pseudo_func_mid_tokens_raw: (_) => token(prec(-1, /[^\)]*/)),

    // .referenced
    pseudo_func_referenced: ($) =>
      seq($.dot_keyword_referenced, "(", field("symbol", $.symbol), ")"),

    // .right
    pseudo_func_right: ($) =>
      seq(
        $.dot_keyword_right,
        "(",
        field("count", $._expression),
        ",",
        field("tokens", $._pseudo_func_right_tokens),
        ")",
      ),
    _pseudo_func_right_tokens: ($) =>
      choice(
        $.pseudo_func_left,
        $.pseudo_func_right,
        seq("{", $.pseudo_func_right_tokens_enclosed, "}"),
        $.pseudo_func_right_tokens_raw,
      ),
    pseudo_func_right_tokens_enclosed: (_) => token(prec(-1, /[^\}]*/)),
    pseudo_func_right_tokens_raw: (_) => token(prec(-1, /[^\)]*/)),

    // .sizeof
    pseudo_func_sizeof: ($) =>
      seq(
        $.dot_keyword_sizeof,
        "(",
        field("target", choice($.symbol, $.local_label_literal, $.member)),
        ")",
      ),

    // .sprintf
    pseudo_func_sprintf: ($) =>
      seq(
        $.dot_keyword_sprintf,
        "(",
        field("fmt", $._expression),
        optional(seq(",", $._pseudo_func_sprintf_args)),
        ")",
      ),
    _pseudo_func_sprintf_args: ($) =>
      seq(
        field("arg", $._expression),
        optional(seq(",", $._pseudo_func_sprintf_args)),
      ),

    // .start
    pseudo_func_start: ($) =>
      seq(
        $.dot_keyword_start,
        "(",
        field("string", $._expression),
        ",",
        field("offset", $._expression),
        ")",
      ),

    // .string
    pseudo_func_string: ($) =>
      seq($.dot_keyword_string, "(", field("expr", $._expression), ")"),

    // .strlen
    pseudo_func_strlen: ($) =>
      seq($.dot_keyword_strlen, "(", field("string", $._expression), ")"),

    // .tcount
    pseudo_func_tcount: ($) =>
      seq(
        $.dot_keyword_tcount,
        "(",
        field("tokens", $._pseudo_func_tcount_tokens),
        ")",
      ),
    _pseudo_func_tcount_tokens: ($) =>
      choice(
        prec(1, seq("{", $.pseudo_func_tcount_tokens_enclosed, "}")),
        $.pseudo_func_tcount_tokens_raw,
      ),
    pseudo_func_tcount_tokens_enclosed: (_) => token(prec(-1, /[^\}]*/)),
    pseudo_func_tcount_tokens_raw: (_) => token(prec(-1, /[^\)]*/)),

    // .xmatch
    pseudo_func_xmatch: ($) =>
      seq(
        $.dot_keyword_xmatch,
        "(",
        field("lhs", $._pseudo_func_xmatch_lhs),
        ",",
        field("rhs", $._pseudo_func_xmatch_rhs),
        ")",
      ),
    _pseudo_func_xmatch_lhs: ($) =>
      choice(
        $.pseudo_func_left,
        $.pseudo_func_right,
        $.pseudo_func_xmatch_lhs_raw,
        seq("{", $.pseudo_func_xmatch_lhs_enclosed, "}"),
      ),
    pseudo_func_xmatch_lhs_raw: (_) => token(prec(-1, /[^,]*/)),
    pseudo_func_xmatch_lhs_enclosed: (_) => token(prec(-1, /[^}]*/)),
    _pseudo_func_xmatch_rhs: ($) =>
      choice(
        $.pseudo_func_left,
        $.pseudo_func_right,
        $.pseudo_func_xmatch_rhs_raw,
        seq("{", $.pseudo_func_xmatch_rhs_enclosed, "}"),
      ),
    pseudo_func_xmatch_rhs_raw: (_) => token(prec(-1, /[^)]*/)),
    pseudo_func_xmatch_rhs_enclosed: (_) => token(prec(-1, /[^}]*/)),

    // Macro call
    macro_call: ($) =>
      seq(field("name", $.macro_call_name), $._macro_call_args),
    macro_call_name: ($) => $._identifier,
    _macro_call_args: ($) =>
      prec.left(
        seq(
          field("arg", $._macro_call_arg),
          optional(seq(",", $._macro_call_args)),
        ),
      ),
    _macro_call_arg: ($) =>
      choice(seq("{", $.macro_call_arg_enclosed, "}"), $.macro_call_arg_raw),
    macro_call_arg_raw: (_) => token(prec(-1, /[^(\r?\n)\,]*/)),
    macro_call_arg_enclosed: (_) => token(prec(-1, /[^(\r?\n)\}]*/)),

    // Dot keywords used by pseudo instructions
    dot_keyword_a16: (_) => make_keyword(".a16"),
    dot_keyword_a8: (_) => make_keyword(".a8"),
    dot_keyword_addr: (_) => make_keyword(".addr"),
    dot_keyword_align: (_) => make_keyword(".align"),
    dot_keyword_asciiz: (_) => make_keyword(".asciiz"),
    dot_keyword_assert: (_) => make_keyword(".assert"),
    dot_keyword_autoimport: (_) => make_keyword(".autoimport"),
    dot_keyword_bankbytes: (_) => make_keyword(".bankbytes"),
    dot_keyword_bss: (_) => make_keyword(".bss"),
    dot_keyword_byte: (_) =>
      choice(make_keyword(".byt"), make_keyword(".byte")),
    dot_keyword_case: (_) => make_keyword(".case"),
    dot_keyword_charmap: (_) => make_keyword(".charmap"),
    dot_keyword_code: (_) => make_keyword(".code"),
    dot_keyword_condes: (_) => make_keyword(".condes"),
    dot_keyword_constructor: (_) => make_keyword(".constructor"),
    dot_keyword_data: (_) => make_keyword(".data"),
    dot_keyword_dbyt: (_) => make_keyword(".dbyt"),
    dot_keyword_debuginfo: (_) => make_keyword(".debuginfo"),
    dot_keyword_define: (_) => make_keyword(".define"),
    dot_keyword_delmacro: (_) =>
      choice(make_keyword(".delmac"), make_keyword(".delmacro")),
    dot_keyword_destructor: (_) => make_keyword(".destructor"),
    dot_keyword_dword: (_) => make_keyword(".dword"),
    dot_keyword_else: (_) => make_keyword(".else"),
    dot_keyword_elseif: (_) => make_keyword(".elseif"),
    dot_keyword_end: (_) => make_keyword(".end"),
    dot_keyword_endenum: (_) => make_keyword(".endenum"),
    dot_keyword_endif: (_) => make_keyword(".endif"),
    dot_keyword_endmacro: (_) =>
      choice(make_keyword(".endmac"), make_keyword(".endmacro")),
    dot_keyword_endproc: (_) => make_keyword(".endproc"),
    dot_keyword_endrepeat: (_) => make_keyword(".endrepeat"),
    dot_keyword_endscope: (_) => make_keyword(".endscope"),
    dot_keyword_endstruct: (_) => make_keyword(".endstruct"),
    dot_keyword_endunion: (_) => make_keyword(".endunion"),
    dot_keyword_enum: (_) => make_keyword(".enum"),
    dot_keyword_error: (_) => make_keyword(".error"),
    dot_keyword_exitmacro: (_) =>
      choice(make_keyword(".exitmac"), make_keyword(".exitmacro")),
    dot_keyword_export: (_) => make_keyword(".export"),
    dot_keyword_exportzp: (_) => make_keyword(".exportzp"),
    dot_keyword_faraddr: (_) => make_keyword(".faraddr"),
    dot_keyword_fatal: (_) => make_keyword(".fatal"),
    dot_keyword_feature: (_) => make_keyword(".feature"),
    dot_keyword_fileopt: (_) =>
      choice(make_keyword(".fileopt"), make_keyword(".fopt")),
    dot_keyword_forceimport: (_) => make_keyword(".forceimport"),
    dot_keyword_global: (_) => make_keyword(".global"),
    dot_keyword_globalzp: (_) => make_keyword(".globalzp"),
    dot_keyword_hibytes: (_) => make_keyword(".hibytes"),
    dot_keyword_i16: (_) => make_keyword(".i16"),
    dot_keyword_i8: (_) => make_keyword(".i8"),
    dot_keyword_if: (_) => make_keyword(".if"),
    dot_keyword_ifblank: (_) => make_keyword(".ifblank"),
    dot_keyword_ifconst: (_) => make_keyword(".ifconst"),
    dot_keyword_ifdef: (_) => make_keyword(".ifdef"),
    dot_keyword_ifnblank: (_) => make_keyword(".ifnblank"),
    dot_keyword_ifndef: (_) => make_keyword(".ifndef"),
    dot_keyword_ifnref: (_) => make_keyword(".ifnref"),
    dot_keyword_ifp02: (_) => make_keyword(".ifp02"),
    dot_keyword_ifp02x: (_) => make_keyword(".ifp02x"),
    dot_keyword_ifp45gs02: (_) => make_keyword(".ifp45gs02"),
    dot_keyword_ifp816: (_) => make_keyword(".ifp816"),
    dot_keyword_ifp4510: (_) => make_keyword(".ifp4510"),
    dot_keyword_ifp6280: (_) => make_keyword(".ifp6280"),
    dot_keyword_ifpc02: (_) => make_keyword(".ifpc02"),
    dot_keyword_ifpce02: (_) => make_keyword(".ifpce02"),
    dot_keyword_ifpdtv: (_) => make_keyword(".ifpdtv"),
    dot_keyword_ifpm740: (_) => make_keyword(".ifpm740"),
    dot_keyword_ifpsc02: (_) => make_keyword(".ifpsc02"),
    dot_keyword_ifpsweet16: (_) => make_keyword(".ifpsweet16"),
    dot_keyword_ifref: (_) => make_keyword(".ifref"),
    dot_keyword_ifpwc02: (_) => make_keyword(".ifpwc02"),
    dot_keyword_import: (_) => make_keyword(".import"),
    dot_keyword_importzp: (_) => make_keyword(".importzp"),
    dot_keyword_incbin: (_) => make_keyword(".incbin"),
    dot_keyword_include: (_) => make_keyword(".include"),
    dot_keyword_interruptor: (_) => make_keyword(".interruptor"),
    dot_keyword_list: (_) => make_keyword(".list"),
    dot_keyword_listbytes: (_) => make_keyword(".listbytes"),
    dot_keyword_literal: (_) => make_keyword(".literal"),
    dot_keyword_lobytes: (_) => make_keyword(".lobytes"),
    dot_keyword_local: (_) => make_keyword(".local"),
    dot_keyword_localchar: (_) => make_keyword(".localchar"),
    dot_keyword_macpack: (_) => make_keyword(".macpack"),
    dot_keyword_macro: (_) =>
      choice(make_keyword(".mac"), make_keyword(".macro")),
    dot_keyword_org: (_) => make_keyword(".org"),
    dot_keyword_out: (_) => make_keyword(".out"),
    dot_keyword_p02: (_) => make_keyword(".p02"),
    dot_keyword_p02x: (_) => make_keyword(".p02x"),
    dot_keyword_p4510: (_) => make_keyword(".p4510"),
    dot_keyword_p45gs02: (_) => make_keyword(".p45gs02"),
    dot_keyword_p6280: (_) => make_keyword(".p6280"),
    dot_keyword_p816: (_) => make_keyword(".p816"),
    dot_keyword_pagelength: (_) => make_keyword(".pagelength"),
    dot_keyword_pc02: (_) => make_keyword(".pc02"),
    dot_keyword_pce02: (_) => make_keyword(".pce02"),
    dot_keyword_pdtv: (_) => make_keyword(".pdtv"),
    dot_keyword_pm740: (_) => make_keyword(".pm740"),
    dot_keyword_popcharmap: (_) => make_keyword(".popcharmap"),
    dot_keyword_popcpu: (_) => make_keyword(".popcpu"),
    dot_keyword_popseg: (_) => make_keyword(".popseg"),
    dot_keyword_proc: (_) => make_keyword(".proc"),
    dot_keyword_psc02: (_) => make_keyword(".psc02"),
    dot_keyword_psweet16: (_) => make_keyword(".psweet16"),
    dot_keyword_pushcharmap: (_) => make_keyword(".pushcharmap"),
    dot_keyword_pushcpu: (_) => make_keyword(".pushcpu"),
    dot_keyword_pushseg: (_) => make_keyword(".pushseg"),
    dot_keyword_pwc02: (_) => make_keyword(".pwc02"),
    dot_keyword_referto: (_) =>
      choice(make_keyword(".referto"), make_keyword(".refto")),
    dot_keyword_reloc: (_) => make_keyword(".reloc"),
    dot_keyword_repeat: (_) => make_keyword(".repeat"),
    dot_keyword_res: (_) => make_keyword(".res"),
    dot_keyword_rodata: (_) => make_keyword(".rodata"),
    dot_keyword_scope: (_) => make_keyword(".scope"),
    dot_keyword_segment: (_) => make_keyword(".segment"),
    dot_keyword_set: (_) => make_keyword(".set"),
    dot_keyword_setcpu: (_) => make_keyword(".setcpu"),
    dot_keyword_smart: (_) => make_keyword(".smart"),
    dot_keyword_struct: (_) => make_keyword(".struct"),
    dot_keyword_tag: (_) => make_keyword(".tag"),
    dot_keyword_undefine: (_) =>
      choice(make_keyword(".undefine"), make_keyword(".undef")),
    dot_keyword_union: (_) => make_keyword(".union"),
    dot_keyword_warning: (_) => make_keyword(".warning"),
    dot_keyword_word: (_) => make_keyword(".word"),
    dot_keyword_zeropage: (_) => make_keyword(".zeropage"),

    // Dot keyword used by pseudo functions
    dot_keyword_addrsize: (_) => make_keyword(".addrsize"),
    dot_keyword_bank: (_) => make_keyword(".bank"),
    dot_keyword_bankbyte: (_) => make_keyword(".bankbyte"),
    dot_keyword_blank: (_) => make_keyword(".blank"),
    dot_keyword_capability: (_) =>
      choice(make_keyword(".cap"), make_keyword(".capability")),
    dot_keyword_concat: (_) => make_keyword(".concat"),
    dot_keyword_const: (_) => make_keyword(".const"),
    dot_keyword_defined: (_) =>
      choice(make_keyword(".def"), make_keyword(".defined")),
    dot_keyword_definedmacro: (_) => make_keyword(".definedmacro"),
    dot_keyword_hibyte: (_) => make_keyword(".hibyte"),
    dot_keyword_hiword: (_) => make_keyword(".hiword"),
    dot_keyword_ident: (_) => make_keyword(".ident"),
    dot_keyword_ismnemonic: (_) =>
      choice(make_keyword(".ismnem"), make_keyword(".ismnemonic")),
    dot_keyword_left: (_) => make_keyword(".left"),
    dot_keyword_lobyte: (_) => make_keyword(".lobyte"),
    dot_keyword_loword: (_) => make_keyword(".loword"),
    dot_keyword_match: (_) => make_keyword(".match"),
    dot_keyword_max: (_) => make_keyword(".max"),
    dot_keyword_mid: (_) => make_keyword(".mid"),
    dot_keyword_min: (_) => make_keyword(".min"),
    dot_keyword_referenced: (_) =>
      choice(make_keyword(".ref"), make_keyword(".referenced")),
    dot_keyword_right: (_) => make_keyword(".right"),
    dot_keyword_sizeof: (_) => make_keyword(".sizeof"),
    dot_keyword_sprintf: (_) => make_keyword(".sprintf"),
    dot_keyword_start: (_) => make_keyword(".start"),
    dot_keyword_string: (_) => make_keyword(".string"),
    dot_keyword_strlen: (_) => make_keyword(".strlen"),
    dot_keyword_tcount: (_) => make_keyword(".tcount"),
    dot_keyword_xmatch: (_) => make_keyword(".xmatch"),

    // Dot keywords used by binary operators
    dot_keyword_mod: (_) => make_keyword(".mod"),
    dot_keyword_bitand: (_) => make_keyword(".bitand"),
    dot_keyword_bitxor: (_) => make_keyword(".bitxor"),
    dot_keyword_shl: (_) => make_keyword(".shl"),
    dot_keyword_shr: (_) => make_keyword(".shr"),
    dot_keyword_bitor: (_) => make_keyword(".bitor"),
    dot_keyword_and: (_) => make_keyword(".and"),
    dot_keyword_or: (_) => make_keyword(".or"),

    // Literals
    symbol: ($) => $._identifier,
    number: ($) => choice($._number_bin, $._number_dec, $._number_hex),
    _number_hex: (_) => seq("$", /[a-fA-F0-9]+/),
    _number_dec: (_) => /[0-9]+/,
    _number_bin: (_) => seq("%", /[0-1]+/),
    string: (_) => seq('"', /[^"]*/, '"'),
    char: (_) => seq("'", /[^']/, "'"),
    member: ($) =>
      seq(
        field("src", choice($.symbol, $.member)),
        "::",
        field("dst", $.symbol),
      ),
    local_label_literal: ($) => seq("@", $._identifier),
    unnamed_label_literal: ($) =>
      prec.left(
        seq(
          ":",
          choice($._unnamed_label_literal_inc, $._unnamed_label_literal_dec),
        ),
      ),
    _unnamed_label_literal_inc: ($) =>
      seq(token.immediate("+"), optional($._unnamed_label_literal_inc)),
    _unnamed_label_literal_dec: ($) =>
      seq(token.immediate("-"), optional($._unnamed_label_literal_dec)),

    // Identifier
    _identifier: (_) => /[a-zA-Z_][0-9a-zA-Z_]*/,

    // Comment
    comment: (_) => token(seq(";", /.*/)),
  },
});

/**
 * @param {Array<any>} arr1
 * @param {Array<any>} arr2
 * @return Array<Array<any>>
 */
function product(arr1, arr2) {
  let res = [];
  for (const value1 of arr1) {
    for (const value2 of arr2) {
      res.push([value1, value2]);
    }
  }
  return res;
}

/**
 * @param {string} s
 */
function escape(s) {
  return s.replace("*", "\\*").replace(".", "\\.");
}

/**
 * @param {string} name
 */
function make_keyword(name) {
  return new RegExp(escape(name), "i");
}

/**
 * @param {string} name
 */
function make_case_insentive(name) {
  return new RegExp(escape(name), "i");
}

/**
 * @param {string} name
 */
function make_opcode(name) {
  return new RegExp(escape(name), "i");
}
