"""The vendored grammar (ca65_ls._grammar) must load and parse CA65 source."""

from tree_sitter import Language, Parser

from ca65_ls import _grammar


def test_language_loads():
    lang = Language(_grammar.language())
    assert lang.node_kind_count > 0


def test_parses_basic_ca65():
    parser = Parser(Language(_grammar.language()))
    tree = parser.parse(b".proc main\n    lda #$00\n    rts\n.endproc\n")
    root = tree.root_node
    assert root.type == "source"
    assert not root.has_error
