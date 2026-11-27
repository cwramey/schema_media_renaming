"""Format language semantics (SPEC §8.3): missing bindings, truthiness, operators, closures,
per-block isolation, step budget and final sanitization."""

from __future__ import annotations

from datetime import date

import pytest
from format_helpers import render, render_raw

from schema_app.core.format import (
    BindingMissing,
    Bindings,
    FormatEvalError,
    FormatSyntaxError,
    compile_format,
)
from schema_app.core.format.evaluator import Evaluator


def test_missing_binding_renders_empty_block() -> None:
    assert render("{n}{t}", n="Movie") == "Movie"


def test_optional_segment_idiom() -> None:
    assert render("{n}{' - ' + t}", n="Show", t="Pilot") == "Show - Pilot"
    assert render("{n}{' - ' + t}", n="Show") == "Show"


def test_none_value_counts_as_missing() -> None:
    assert render("{n}{' [' + group + ']'}", n="X", group=None) == "X"


def test_bindings_mapping_raises_binding_missing() -> None:
    bindings = Bindings.from_dict({"n": "x"})
    with pytest.raises(BindingMissing):
        bindings["t"]
    assert bindings.get("t") is None
    assert "n" in bindings and "t" not in bindings


@pytest.mark.parametrize(
    ("literal", "expected"),
    [("null", "no"), ("''", "no"), ("0", "no"), ("0.0", "no"), ("false", "no"), ("[]", "no"),
     ("'x'", "yes"), ("1", "yes"), ("true", "yes"), ("[0]", "yes")],
)  # fmt: skip
def test_groovy_truthiness(literal: str, expected: str) -> None:
    assert render("{" + literal + " ? 'yes' : 'no'}") == expected


def test_elvis_uses_fallback_for_falsy_and_missing() -> None:
    assert render("{t ?: 'Untitled'}", t="") == "Untitled"
    assert render("{t ?: 'Untitled'}") == "Untitled"
    assert render("{t ?: 'Untitled'}", t="Pilot") == "Pilot"


def test_any_returns_first_non_empty_alternative() -> None:
    assert render("{any{t}{''}{n}}", n="Name") == "Name"


def test_any_skips_alternatives_that_error() -> None:
    assert render("{any{n.nope()}{n}}", n="Name") == "Name"


def test_any_all_failed_renders_empty() -> None:
    assert render("A{any{t}{x}}B") == "AB"


def test_allof_collects_values() -> None:
    assert render("{allOf{vf}{vc}{ac}.join(' ')}", vf="1080p", ac="DTS") == "1080p DTS"


def test_list_literal_drops_missing_entries() -> None:
    assert render("{[vf, vc, ac]}", vf="1080p", ac="AAC") == "[1080p, AAC]"


def test_empty_list_renders_empty() -> None:
    assert render("x{[]}") == "x"


def test_string_concatenation_with_numbers() -> None:
    assert render("{'Season ' + s}", s=3) == "Season 3"
    assert render("{s + 1}", s=3) == "4"
    assert render("{1.5 + 1}") == "2.5"


def test_list_concatenation() -> None:
    assert render("{[1] + [2] + 3}") == "[1, 2, 3]"


def test_string_minus_and_repeat() -> None:
    assert render("{n - 'The '}", n="The Office") == "Office"
    assert render("{'ab' * 3}") == "ababab"
    assert render("{7 % 3}") == "1"


@pytest.mark.parametrize(
    ("expr", "expected"),
    [("{1 == 1.0}", "true"), ("{'1' == 1}", "false"), ("{true == 1}", "false"), ("{'a' != 'b'}", "true"),
     ("{2 < 10}", "true"), ("{'abc' < 'abd'}", "true"), ("{3 >= 3}", "true"), ("{null == null}", "true"),
     ("{!true}", "false"), ("{-s}", "-3"), ("{[1, 2] == [1, 2]}", "true")],
)  # fmt: skip
def test_comparisons_and_unary(expr: str, expected: str) -> None:
    assert render(expr, s=3) == expected


def test_date_comparison_and_properties() -> None:
    assert render("{d > startdate}", d=date(2024, 3, 4), startdate=date(2019, 1, 1)) == "true"
    assert render("{d.year}-{d.month}", d=date(2024, 3, 4)) == "2024-3"


def test_logical_operators_short_circuit() -> None:
    assert render("{false && missing ? 'y' : 'n'}") == "n"
    assert render("{true || missing ? 'y' : 'n'}") == "y"


def test_indexing_strings_and_negative_indexes() -> None:
    assert render("{n[0]}{n[-1]}", n="Starfall") == "Sl"
    assert render("A{alias[5]}B", alias=["x"]) == "AB"  # out of range -> missing


def test_safe_navigation_on_null() -> None:
    assert render("x{null?.lower()}y") == "xy"


def test_type_error_isolated_to_its_block() -> None:
    template = compile_format("{n} {n < 3} {y}")
    result = template.render_detailed(Bindings.from_dict({"n": "Name", "y": 2019}))
    assert result.path == "Name 2019"
    assert [i.severity for i in result.issues] == ["warning"]
    assert "Cannot compare" in result.issues[0].message
    assert result.issues[0].start == 5


def test_strict_render_raises_block_errors() -> None:
    with pytest.raises(FormatEvalError) as info:
        render("{n} {n.unknownMethod()}", strict=True, n="Name")
    assert "Unknown method" in info.value.message


def test_unknown_method_is_block_warning_when_lenient() -> None:
    result = compile_format("{n}{n.nope()}").render_detailed(Bindings.from_dict({"n": "Name"}))
    assert result.path == "Name" and "Unknown method 'nope()'" in result.issues[0].message


def test_property_on_plain_value_is_error() -> None:
    with pytest.raises(FormatEvalError) as info:
        render("{n.size}", strict=True, n="abc")
    assert "did you mean .size()" in info.value.message


def test_step_budget_guards_pathological_expressions() -> None:
    node = compile_format("{allOf" + "{n.lower()}" * 40 + "}").blocks[0].node
    evaluator = Evaluator(Bindings.from_dict({"n": "x"}), budget=50)
    with pytest.raises(FormatEvalError) as info:
        evaluator.eval(node)
    assert "too complex" in info.value.message


def test_deeply_nested_expression_is_rejected_not_crashing() -> None:
    expr = "{" + "+".join(["1"] * 3000) + "}"
    with pytest.raises(FormatEvalError) as info:
        render(expr, strict=True)
    assert "too complex" in info.value.message


@pytest.mark.parametrize(
    "expr",
    [
        "{('x' * 1000) * 1000 * 1000}",  # 1 GB in three steps without the length check
        "{'x'.padLeft(5000000000)}",
        "{'x'.padRight(100000)}",
        "{n.pad(1000000)}",
        "{('x' * 1000).replace('x', 'y' * 1000).replace('y', 'z' * 1000)}",
    ],
)
def test_formats_cannot_build_huge_strings(expr: str) -> None:
    with pytest.raises(FormatEvalError):
        render(expr, strict=True, n=7)


def test_reasonable_padding_and_repetition_still_work() -> None:
    assert render("{n.pad(3)}-{'7'.padLeft(3, '0')}-{'-' * 3}", n=7) == "007-007----"


def test_overlong_format_is_rejected() -> None:
    with pytest.raises(FormatSyntaxError):
        compile_format("x" * 10_001)


def test_empty_result_is_an_error() -> None:
    with pytest.raises(FormatEvalError) as info:
        render("{t}")
    assert "empty path" in info.value.message


def test_absolute_result_is_an_error() -> None:
    with pytest.raises(FormatEvalError):
        render("/storage/{n}", n="x")
    with pytest.raises(FormatEvalError):
        render("D:/Media/{n}", n="x")


def test_title_starting_with_letter_colon_is_not_a_drive() -> None:
    assert render("{n}", n="X: First Class") == "X - First Class"


def test_slash_in_value_never_creates_folders() -> None:
    assert render("{n}/{t}", n="AC/DC", t="Face/Off") == "AC-DC/Face-Off"


def test_literal_slash_creates_folders() -> None:
    assert render("{n + '/' + t}", n="A", t="B") == "A/B"


def test_backslash_literal_is_a_separator() -> None:
    assert render("{n}\\{t}", n="A", t="B") == "A/B"


def test_colon_is_sanitized_late_so_colon_method_works() -> None:
    assert render("{n}", n="Avengers: Endgame") == "Avengers - Endgame"
    assert render("{n.colon('\u2236')}", n="Avengers: Endgame") == "Avengers\u2236 Endgame"


def test_trailing_dots_and_whitespace_are_trimmed_per_component() -> None:
    assert render("{n}./x.", n="Name ") == "Name/x"


def test_empty_components_are_dropped() -> None:
    assert render("{n}/{t}/{y}", n="Name", y=2019) == "Name/2019"


def test_extension_suffix_is_appended() -> None:
    assert render("{n}", ext=".mkv", n="Movie.") == "Movie.mkv"


def test_render_raw_keeps_raw_text() -> None:
    assert render_raw("{n}: {t}", n="A", t="B") == "A: B"


def test_bool_and_float_rendering() -> None:
    assert render("{flag} {rating}", flag=True, rating=7.5) == "true 7.5"
