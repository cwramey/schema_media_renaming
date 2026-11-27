"""Format language: template scanning, lexing, Pratt parsing and syntax errors (SPEC §8.1–§8.2)."""

from __future__ import annotations

import pytest
from format_helpers import render

from schema_app.core.format import FormatSyntaxError, compile_format
from schema_app.core.format.lexer import GSTR, NUM, STR, scan_template
from schema_app.core.format.parser import Binary, Call, Closures, Elvis, Ternary


def test_literal_only_template() -> None:
    assert render("Movies/Static") == "Movies/Static"


def test_double_braces_are_literal() -> None:
    assert render("{{n}} {n}", n="X") == "{n} X"


def test_scan_segments_and_offsets() -> None:
    segments = scan_template("a{n}b{t}")
    assert [type(s).__name__ for s in segments] == ["LiteralSegment", "BlockSegment", "LiteralSegment", "BlockSegment"]
    block = segments[1]
    assert (block.start, block.end) == (1, 4)


def test_strings_numbers_and_escapes() -> None:
    tokens = scan_template("{'it\\'s' + \"tab\\there\" + 12 + 3.5}")[0].tokens
    kinds = [t.kind for t in tokens if t.kind in (STR, NUM)]
    assert kinds == [STR, STR, NUM, NUM]
    assert tokens[0].value == "it's"
    assert tokens[2].value == "tab\there"


def test_unicode_escape() -> None:
    assert render("{'\\u0041BC'}") == "ABC"


def test_brace_inside_string_does_not_close_block() -> None:
    assert render("{'}' + n + '{'}", n="x") == "}x{"


def test_slashy_regex_string() -> None:
    tokens = scan_template(r"{n.replaceAll(/[:\/]/, '')}")[0].tokens
    regex = [t for t in tokens if t.kind == STR and t.regex]
    assert regex and regex[0].value == "[:/]"


def test_gstring_interpolation() -> None:
    assert render('{"S${s.pad(2)}E${e.pad(2)}"}', s=1, e=2) == "S01E02"


def test_gstring_dollar_name() -> None:
    tokens = scan_template('{"$n - $t"}')[0].tokens
    assert tokens[0].kind == GSTR
    assert render('{"$n - $t"}', n="A", t="B") == "A - B"


def test_gstring_escaped_dollar() -> None:
    assert render('{"\\$5"}') == "$5"


def test_precedence_multiplication_before_addition() -> None:
    assert render("{1 + 2 * 3}") == "7"


def test_parentheses_override_precedence() -> None:
    assert render("{(1 + 2) * 3}") == "9"


def test_ternary_is_right_associative() -> None:
    node = compile_format("{a ? 'x' : b ? 'y' : 'z'}").blocks[0].node
    assert isinstance(node, Ternary)
    assert isinstance(node.other, Ternary)
    assert render("{a ? 'x' : b ? 'y' : 'z'}", a=False, b=True) == "y"


def test_elvis_binds_tighter_than_ternary() -> None:
    node = compile_format("{a ?: b ? 'x' : 'y'}").blocks[0].node
    assert isinstance(node, Ternary)
    assert isinstance(node.cond, Elvis)


def test_and_binds_tighter_than_or() -> None:
    node = compile_format("{a || b && c}").blocks[0].node
    assert isinstance(node, Binary) and node.op == "||"
    assert isinstance(node.right, Binary) and node.right.op == "&&"


def test_comparison_binds_tighter_than_equality() -> None:
    node = compile_format("{a < b == true}").blocks[0].node
    assert isinstance(node, Binary) and node.op == "=="


def test_method_chain_and_index() -> None:
    node = compile_format("{n.lower().take(3)}").blocks[0].node
    assert isinstance(node, Call) and node.name == "take"
    assert render("{alias[1]}", alias=["a", "b"]) == "b"


def test_any_and_allof_closures_parse() -> None:
    node = compile_format("{any{t}{n}{'x'}}").blocks[0].node
    assert isinstance(node, Closures) and node.kind == "any" and len(node.bodies) == 3
    node = compile_format("{allOf{vf}{vc}.join(' ')}").blocks[0].node
    assert isinstance(node, Call)


def test_list_literal_with_trailing_comma() -> None:
    assert render("{[1, 2,]}") == "[1, 2]"


def test_identifiers_collected() -> None:
    assert compile_format("{n} - {any{t}{'x'}} {'S' + s.pad(2)}").identifiers == frozenset({"n", "t", "s"})


def test_compile_is_cached() -> None:
    assert compile_format("{n}") is compile_format("{n}")


@pytest.mark.parametrize(
    ("expr", "message", "start"),
    [
        ("{n", "Missing '}'", 0),
        ("abc}", "Unexpected '}'", 3),
        ("{}", "Empty expression", 0),
        ("{'abc}", "Unterminated string", 1),
        ("{n == }", "Expected an expression", 6),
        ("{n +}", "Expected an expression", 4),
        ("{(n}", "Expected ')'", 3),
        ("{[1, 2}", "Expected ',' or ']'", 6),
        ("{n.}", "Expected a property or method name", 3),
        ("{a ? b}", "Expected ':'", 6),
    ],
)
def test_syntax_errors_have_offsets(expr: str, message: str, start: int) -> None:
    with pytest.raises(FormatSyntaxError) as info:
        compile_format(expr)
    assert message in info.value.message
    assert info.value.start == start
    assert info.value.end >= info.value.start


@pytest.mark.parametrize(
    ("expr", "fragment"),
    [
        ("{x = 1}", "Assignment"),
        ("{n; t}", "Multiple statements"),
        ("{n =~ /x/}", "Regex operators"),
        ("{n ==~ /x/}", "Regex operators"),
        ("{s ** 2}", "power operator"),
        ("{e++}", "Increment"),
        ("{[1].collect{ it -> it }}", "Closures with parameters"),
        ("{[1].collect{ it }}", "Closures after methods"),
        ("{1..3}", "Ranges"),
        ("{def x}", "'def'"),
        ("{vf in ['1080p']}", "'in' operator"),
        ("{n as String}", "Type coercion"),
        ("{csv('/x.csv')}", "FileBot function 'csv()'"),
        ("{foo(1)}", "Function calls"),
        ("{n.find{ it }}", "Closures after methods"),
        ("{ {n} }", "Closures are only supported"),
        ("{[a: 1]}", "Map literals"),
        ("{n & t}", "Bitwise"),
        ("{n | t}", "Bitwise"),
        ("{bytes / 1000}", "Unterminated /regex/"),
        ("{n // comment}", "Comments"),
        ("{n $ t}", "Unexpected character"),
        ("{none{n}}", "FileBot function"),
        ("{if (n) t}", "'if'"),
    ],
)
def test_unsupported_groovy_constructs(expr: str, fragment: str) -> None:
    with pytest.raises(FormatSyntaxError) as info:
        compile_format(expr)
    assert fragment in info.value.message


def test_error_offsets_point_into_later_blocks() -> None:
    with pytest.raises(FormatSyntaxError) as info:
        compile_format("{n} - {s00e00} - {t =}")
    assert info.value.start == 20


def test_empty_format_is_an_error() -> None:
    with pytest.raises(FormatSyntaxError):
        compile_format("   ")


def test_groovy_numeric_suffix_is_accepted() -> None:
    assert render("{2L + 1}") == "3"
