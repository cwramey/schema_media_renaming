"""Whitelisted methods callable as ``value.method(args)`` (SPEC §8.4, FileBot helper names).

Every method is a pure function ``fn(receiver, args) -> value``. Implementations raise
:class:`MethodError` for type/argument problems (the evaluator turns that into a positioned
``FormatEvalError``) and :class:`BindingMissing` when FileBot would return null / throw "not found"
(so the enclosing block renders empty).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from unidecode import unidecode

from schema_app.core.format.errors import BindingMissing, MethodError
from schema_app.core.format.regex import (
    GuardedPattern,
    check_subject,
    compile_or_literal,
    compile_regex,
    java_replacement,
)
from schema_app.core.format.values import (
    BindingObject,
    groovy_equals,
    is_empty,
    is_number,
    op_add,
    op_compare,
    op_sub,
    to_text,
    type_name,
)

MethodFn = Callable[[Any, list[Any]], Any]


@dataclass(frozen=True, slots=True)
class MethodSpec:
    name: str
    fn: MethodFn
    min_args: int
    max_args: int | None  # None = variadic
    signature: str
    description: str
    example: str

    def arity_text(self) -> str:
        if self.max_args is None:
            return f"at least {self.min_args}"
        if self.min_args == self.max_args:
            return str(self.min_args)
        return f"{self.min_args}–{self.max_args}"

    def accepts(self, count: int) -> bool:
        return count >= self.min_args and (self.max_args is None or count <= self.max_args)


METHODS: dict[str, MethodSpec] = {}


def _method(
    name: str, signature: str, description: str, example: str, min_args: int = 0, max_args: int | None = -1
) -> Callable[[MethodFn], MethodFn]:
    def register(fn: MethodFn) -> MethodFn:
        METHODS[name] = MethodSpec(
            name, fn, min_args, min_args if max_args == -1 else max_args, signature, description, example
        )
        return fn

    return register


def _alias(name: str, target: str, signature: str, description: str, example: str) -> None:
    spec = METHODS[target]
    METHODS[name] = MethodSpec(name, spec.fn, spec.min_args, spec.max_args, signature, description, example)


# Methods FileBot (or the Groovy GDK it exposes) has but Schema deliberately doesn't implement.
UNSUPPORTED_FILEBOT_METHODS = frozenset(
    {
        "transliterate", "clean", "findMatch", "findWordMatch", "isLatin", "getGraphemeClusters",
        "negative", "joiningDistinct", "bounds", "parseDate", "toDate", "zone", "toLocale", "check",
        "listPath", "getRoot", "getTail", "head", "tail", "getRelativePathTail", "getDiskSpace",
        "toFile", "collect", "find", "findAll", "each", "eachWithIndex", "findResult", "findResults",
        "inject", "every", "grep", "groupBy", "countBy", "collectEntries", "flatten", "withIndex",
        "getInfo", "getDetails", "getEpisodes", "getCrew", "getActors", "getArtwork", "getFanart",
        "getExtras", "getCollection", "getAlternativeTitles", "getTranslations", "getExternalIds",
        "getExternalId", "getExternalSeries", "getRaw", "getService", "isVideo", "isAudio",
        "isSubtitle", "isImage", "isArchive", "isDisk", "isClutter", "isEpisode", "isMovie",
        "getMediaInfo", "getCRC32", "hash", "validateFileName", "validateFilePath",
        "stripReleaseInfo", "normalizePunctuation", "getSimilarity", "sortBySimilarity", "call",
        "getAt", "putAt", "leftShift", "div", "multiply", "power", "mod", "center", "execute",
        "eachLine", "readLines", "getText", "getBytes", "toURL", "toURI", "derive", "deriveFolder",
    }
)  # fmt: skip


# --------------------------------------------------------------------------- coercion helpers


def _text(value: Any, method: str) -> str:
    if value is None:
        raise MethodError(f"Cannot call {method}() on null")
    if isinstance(value, list | tuple):
        raise MethodError(f"{method}() expects text, got a List")
    return to_text(value)


def _arg_text(value: Any, method: str) -> str:
    if value is None:
        raise MethodError(f"{method}(): argument must not be null")
    if isinstance(value, list | tuple):
        raise MethodError(f"{method}(): argument must be text, got a List")
    return to_text(value)


def _arg_int(value: Any, method: str) -> int:
    if isinstance(value, bool) or value is None:
        raise MethodError(f"{method}(): expected a whole number, got {type_name(value)}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    raise MethodError(f"{method}(): expected a whole number, got {type_name(value)}")


def _number(value: Any, method: str) -> int | float:
    if is_number(value):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text) if re.fullmatch(r"-?\d+", text) else float(text)
        except ValueError:
            pass
    raise MethodError(f"{method}() needs a number, got {type_name(value)}")


def _list(value: Any, method: str) -> list[Any]:
    if isinstance(value, list | tuple):
        return list(value)
    raise MethodError(f"{method}() needs a List, got {type_name(value)}")


def _sub(pattern: GuardedPattern, repl: str | Callable[[Any], str], text: str) -> str:
    # GuardedPattern applies the subject limit and the regex timeout (see core/format/regex.py).
    return pattern.sub(repl if callable(repl) else (lambda _m: repl), check_subject(text))


# --------------------------------------------------------------------------- numbers & padding


MAX_PAD_WIDTH = 255  # a path component can't be longer anyway
MAX_ARG_LENGTH = 1024


def _arg_width(value: Any, method: str) -> int:
    width = _arg_int(value, method)
    if width > MAX_PAD_WIDTH:
        raise MethodError(f"{method}(): width must be at most {MAX_PAD_WIDTH}")
    return width


@_method("pad", "pad(width[, char])", "Zero-pad a number (strings: pad embedded numbers; with a char: left-pad the text).",
         "{e.pad(3)} → 007", 1, 2)
def _pad(recv: Any, args: list[Any]) -> Any:
    width = _arg_width(args[0], "pad")
    if width < 1:
        raise MethodError("pad(): width must be at least 1")
    char = _arg_text(args[1], "pad")[:1] if len(args) > 1 else "0"
    if not char:
        raise MethodError("pad(): padding character must not be empty")
    if isinstance(recv, list | tuple):
        return [_pad(item, args) for item in recv]
    if isinstance(recv, float) and recv.is_integer():
        recv = int(recv)
    if isinstance(recv, int) and not isinstance(recv, bool):
        sign = "-" if recv < 0 else ""
        return sign + str(abs(recv)).rjust(width - len(sign), char)
    text = _text(recv, "pad")
    if len(args) > 1 or text.isdigit():
        return text.rjust(width, char)
    return re.sub(r"\d+", lambda m: m.group().rjust(width, "0"), text)


@_method("padLeft", "padLeft(width[, char])", "Left-pad text to a width (Groovy).", "{'7'.padLeft(3, '0')} → 007", 1, 2)
def _pad_left(recv: Any, args: list[Any]) -> str:
    char = _arg_text(args[1], "padLeft")[:1] if len(args) > 1 else " "
    return _text(recv, "padLeft").rjust(_arg_width(args[0], "padLeft"), char or " ")


@_method("padRight", "padRight(width[, char])", "Right-pad text to a width (Groovy).", "{'ab'.padRight(4, '.')} → ab..", 1, 2)
def _pad_right(recv: Any, args: list[Any]) -> str:
    char = _arg_text(args[1], "padRight")[:1] if len(args) > 1 else " "
    return _text(recv, "padRight").ljust(_arg_width(args[0], "padRight"), char or " ")


def _round_half_up(value: float | int, digits: int) -> Decimal:
    quant = Decimal(1).scaleb(-digits)
    return Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)


@_method("round", "round([digits])", "Round a number (half up).", "{rating.round()} → 8", 0, 1)
def _round(recv: Any, args: list[Any]) -> Any:
    value = _number(recv, "round")
    digits = _arg_int(args[0], "round") if args else 0
    if digits < 0 or digits > 10:
        raise MethodError("round(): digits must be between 0 and 10")
    rounded = _round_half_up(value, digits)
    return int(rounded) if digits == 0 else float(rounded)


@_method("toInteger", "toInteger()", "Convert to a whole number.", "{'007'.toInteger()} → 7")
def _to_integer(recv: Any, args: list[Any]) -> int:
    if isinstance(recv, bool):
        raise MethodError("toInteger(): cannot convert a Boolean")
    if isinstance(recv, int):
        return recv
    if isinstance(recv, float):
        return int(recv)
    text = _text(recv, "toInteger").strip()
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    raise MethodError(f"toInteger(): '{text}' is not a whole number")


@_method("toString", "toString()", "Convert to text.", "{y.toString()} → 2019")
def _to_string(recv: Any, args: list[Any]) -> str:
    return to_text(recv)


_ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
          (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]  # fmt: skip


def _to_roman(number: int) -> str:
    out = []
    for value, letters in _ROMAN:
        while number >= value:
            out.append(letters)
            number -= value
    return "".join(out)


@_method("roman", "roman()", "Arabic numbers to Roman numerals (standalone numbers in text).", "{'Rocky 2'.roman()} → Rocky II")
def _roman(recv: Any, args: list[Any]) -> str:
    if isinstance(recv, int) and not isinstance(recv, bool):
        if not 0 < recv < 4000:
            raise MethodError("roman(): number must be between 1 and 3999")
        return _to_roman(recv)
    text = _text(recv, "roman")
    return re.sub(r"\b\d+\b", lambda m: _to_roman(int(m.group())) if 0 < int(m.group()) < 4000 else m.group(), text)


# --------------------------------------------------------------------------- case


def _case(recv: Any, args: list[Any], method: str, convert: Callable[[str], str]) -> str:
    text = _text(recv, method)
    if args:
        pattern = compile_or_literal(_arg_text(args[0], method))
        return _sub(pattern, lambda m: convert(m.group()), text)
    return convert(text)


@_method("lower", "lower([pattern])", "Lower-case (optionally only the parts matching a regex).", "{n.lower()} → starfall station", 0, 1)
def _lower(recv: Any, args: list[Any]) -> str:
    return _case(recv, args, "lower", str.lower)


@_method("upper", "upper([pattern])", "Upper-case (optionally only the parts matching a regex).", "{n.upper()} → STARFALL STATION", 0, 1)
def _upper(recv: Any, args: list[Any]) -> str:
    return _case(recv, args, "upper", str.upper)


@_method("toLowerCase", "toLowerCase()", "Lower-case (Java).", "{n.toLowerCase()} → starfall station")
def _to_lower(recv: Any, args: list[Any]) -> str:
    return _text(recv, "toLowerCase").lower()


@_method("toUpperCase", "toUpperCase()", "Upper-case (Java).", "{vf.toUpperCase()} → 1080P")
def _to_upper(recv: Any, args: list[Any]) -> str:
    return _text(recv, "toUpperCase").upper()


@_method("upperInitial", "upperInitial()", "Upper-case the first letter of every word.", "{'the office'.upperInitial()} → The Office")
def _upper_initial(recv: Any, args: list[Any]) -> str:
    return re.sub(r"(?<![\w'’])([^\W\d_])", lambda m: m.group(1).upper(), _text(recv, "upperInitial"))


@_method("lowerTrail", "lowerTrail()", "Lower-case every letter except the first of each word.", "{'THE OFFICE'.lowerTrail()} → The Office")
def _lower_trail(recv: Any, args: list[Any]) -> str:
    return re.sub(r"([^\W_])([\w'’]*)", lambda m: m.group(1) + m.group(2).lower(), _text(recv, "lowerTrail"))


@_method("capitalize", "capitalize()", "Upper-case the first character.", "{'starfall'.capitalize()} → Starfall")
def _capitalize(recv: Any, args: list[Any]) -> str:
    text = _text(recv, "capitalize")
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------- replacement helpers


@_method("colon", "colon(replacement) / colon(ratio, replacement)",
         "Replace ':' (e.g. ' - '; a replacement with spaces absorbs the spaces around the colon). "
         "With two arguments the first one replaces ratio colons like 16:9.",
         "{n.colon(' - ')} → Avengers - Endgame", 1, 2)
def _colon(recv: Any, args: list[Any]) -> str:
    text = _text(recv, "colon")
    if len(args) == 2:
        ratio = _arg_text(args[0], "colon")
        repl = _arg_text(args[1], "colon")
        text = re.sub(r"(?<=\d):(?=\d)", lambda _m: ratio, text)
    else:
        repl = _arg_text(args[0], "colon")
    if repl != repl.strip():
        return re.sub(r"\s*:\s*", lambda _m: repl, text)
    return text.replace(":", repl)


@_method("slash", "slash(replacement)", "Replace '/' and '\\'.", "{'AC/DC'.slash('-')} → AC-DC", 1)
def _slash(recv: Any, args: list[Any]) -> str:
    repl = _arg_text(args[0], "slash")
    return _text(recv, "slash").replace("/", repl).replace("\\", repl)


@_method("space", "space(replacement)", "Replace whitespace runs.", "{n.space('.')} → Starfall.Station", 1)
def _space(recv: Any, args: list[Any]) -> str:
    repl = _arg_text(args[0], "space")
    return re.sub(r"\s+", lambda _m: repl, _text(recv, "space"))


@_method("replace", "replace(text, replacement)", "Replace every literal occurrence.", "{n.replace(' ', '_')} → Starfall_Station", 2)
def _replace(recv: Any, args: list[Any]) -> str:
    return _text(recv, "replace").replace(_arg_text(args[0], "replace"), _arg_text(args[1], "replace"))


@_method("replaceAll", "replaceAll(regex[, replacement])", "Regex replace (Java $1 group references).",
         "{t.replaceAll(/[!?.]+$/, '')}", 1, 2)
def _replace_all(recv: Any, args: list[Any]) -> str:
    pattern = compile_regex(_arg_text(args[0], "replaceAll"))
    repl = _arg_text(args[1], "replaceAll") if len(args) > 1 else ""
    return _sub(pattern, java_replacement(repl, pattern), _text(recv, "replaceAll"))


@_method("replaceFirst", "replaceFirst(regex, replacement)", "Regex replace of the first match.",
         "{n.replaceFirst(/^The /, '')} → Office", 2)
def _replace_first(recv: Any, args: list[Any]) -> str:
    pattern = compile_regex(_arg_text(args[0], "replaceFirst"))
    replace = java_replacement(_arg_text(args[1], "replaceFirst"), pattern)
    return pattern.sub(replace, check_subject(_text(recv, "replaceFirst")), count=1)


@_method("removeAll", "removeAll(regex)", "Delete every regex match.", "{t.removeAll(/[()]/)}", 1)
def _remove_all(recv: Any, args: list[Any]) -> str:
    return _sub(compile_regex(_arg_text(args[0], "removeAll")), "", _text(recv, "removeAll"))


@_method("remove", "remove(pattern, …)", "Delete every match of one or more patterns (regex, or literal text).",
         "{n.remove(', The')}", 1, None)
def _remove(recv: Any, args: list[Any]) -> str:
    text = _text(recv, "remove")
    for arg in args:
        text = _sub(compile_or_literal(_arg_text(arg, "remove")), "", text)
    return text


@_method("before", "before(pattern)", "Text before the first match (undefined when not found).", "{t.before(/ \\(/)}", 1)
def _before(recv: Any, args: list[Any]) -> str:
    text = check_subject(_text(recv, "before"))
    match = compile_or_literal(_arg_text(args[0], "before")).search(text)
    if not match:
        raise BindingMissing("before", "before(): pattern not found")
    return text[: match.start()]


@_method("after", "after(pattern)", "Text after the first match (undefined when not found).", "{t.after(/: /)}", 1)
def _after(recv: Any, args: list[Any]) -> str:
    text = check_subject(_text(recv, "after"))
    match = compile_or_literal(_arg_text(args[0], "after")).search(text)
    if not match:
        raise BindingMissing("after", "after(): pattern not found")
    return text[match.end() :]


def _group(match: Any, args: list[Any], method: str) -> str:
    if len(args) > 1:
        key = args[1]
        try:
            value = match.group(key if isinstance(key, str) else _arg_int(key, method))
        except IndexError as exc:
            raise MethodError(f"{method}(): no such group {to_text(key)}") from exc
    else:
        value = match.group(1) if match.re.groups >= 1 else match.group(0)
    if value is None:
        raise BindingMissing(method, f"{method}(): group did not participate in the match")
    return value


@_method("match", "match(regex[, group])", "First match: group 1 if the regex has groups, else the whole match (undefined when not found).",
         "{fn.match(/S\\d+E\\d+/)} → S01E02", 1, 2)
def _match(recv: Any, args: list[Any]) -> str:
    text = check_subject(_text(recv, "match"))
    found = compile_or_literal(_arg_text(args[0], "match")).search(text)
    if not found:
        raise BindingMissing("match", "match(): pattern not found")
    return _group(found, args, "match")


@_method("matchAll", "matchAll(regex[, group])", "Every match as a list (undefined when none).", "{fn.matchAll(/\\d+/)}", 1, 2)
def _match_all(recv: Any, args: list[Any]) -> list[str]:
    text = check_subject(_text(recv, "matchAll"))
    pattern = compile_or_literal(_arg_text(args[0], "matchAll"))
    results = [_group(m, args, "matchAll") for m in pattern.finditer(text)]
    if not results:
        raise BindingMissing("matchAll", "matchAll(): pattern not found")
    return results


@_method("matches", "matches(regex)", "True when the whole text matches.", "{vf.matches(/\\d+p/)} → true", 1)
def _matches(recv: Any, args: list[Any]) -> bool:
    return compile_regex(_arg_text(args[0], "matches")).fullmatch(check_subject(_text(recv, "matches"))) is not None


@_method("split", "split(regex)", "Split into a list (Java semantics: trailing empty parts dropped).", "{'a.b.c'.split(/\\./)} → [a, b, c]", 1)
def _split(recv: Any, args: list[Any]) -> list[str]:
    text = check_subject(_text(recv, "split"))
    parts = compile_or_literal(_arg_text(args[0], "split")).split(text)
    while parts and parts[-1] == "":
        parts.pop()
    return parts


@_method("tokenize", "tokenize([delimiters])", "Split on any of the delimiter characters (default whitespace), dropping empties.",
         "{'a b  c'.tokenize()} → [a, b, c]", 0, 1)
def _tokenize(recv: Any, args: list[Any]) -> list[str]:
    text = _text(recv, "tokenize")
    if not args:
        return text.split()
    delims = _arg_text(args[0], "tokenize")
    if not delims:
        return [text] if text else []
    return [part for part in re.split("[" + re.escape(delims) + "]", text) if part]


# --------------------------------------------------------------------------- transliteration & names

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def ascii_text(text: str) -> str:
    converted = unidecode(text.translate(_UMLAUTS))
    return "".join(ch for ch in converted if ord(ch) < 128)


@_method("ascii", "ascii()", "Transliterate to ASCII (umlauts expanded, other non-ASCII removed).", "{'風の翼'.ascii()} → Feng No Yi")
def _ascii(recv: Any, args: list[Any]) -> str:
    return ascii_text(_text(recv, "ascii")).strip()


@_method("asciiQuotes", "asciiQuotes()", "Normalize curly quotes and apostrophes.", "{t.asciiQuotes()}")
def _ascii_quotes(recv: Any, args: list[Any]) -> str:
    table = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'", "`": "'",
                           "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"'})  # fmt: skip
    return _text(recv, "asciiQuotes").translate(table)


@_method("acronym", "acronym()", "First letter of each word.", "{'Starfall Station'.acronym()} → SS")
def _acronym(recv: Any, args: list[Any]) -> str:
    return "".join(word[0] for word in re.findall(r"[^\W_]+", _text(recv, "acronym")))


@_method("initialName", "initialName()", "Abbreviate given names: 'John Smith' → 'J. Smith'.", "{'Sam Reyes'.initialName()} → S. Reyes")
def _initial_name(recv: Any, args: list[Any]) -> str:
    words = _text(recv, "initialName").split()
    if len(words) < 2:
        return " ".join(words)
    return " ".join([w[0] + "." for w in words[:-1]] + [words[-1]])


_ARTICLE_RE = re.compile(r"(?i)^(the|a|an)\s+(.+)$")


def sort_name(text: str, repl: str = "$2, $1") -> str:
    match = _ARTICLE_RE.match(text.strip())
    if not match:
        return text
    return java_replacement(repl, _ARTICLE_RE)(match)


def sort_initial(text: str) -> str:
    match = _ARTICLE_RE.match(text.strip())
    base = (match.group(2) if match else text).strip()
    if not base:
        raise BindingMissing("sortInitial", "sortInitial(): empty name")
    first = ascii_text(base[0])[:1] or base[0]
    if first.isdigit():
        return "0-9"
    if first.isalpha():
        return first.upper()
    return "#"


@_method("sortName", "sortName([replacement])", "Move a leading The/A/An to the end ('$2, $1').", "{'The Office'.sortName()} → Office, The", 0, 1)
def _sort_name(recv: Any, args: list[Any]) -> str:
    repl = _arg_text(args[0], "sortName") if args else "$2, $1"
    return sort_name(_text(recv, "sortName"), repl)


@_method("sortInitial", "sortInitial()", "First letter of the sort name; digits → 0-9, other → #.", "{'The Office'.sortInitial()} → O")
def _sort_initial(recv: Any, args: list[Any]) -> str:
    return sort_initial(_text(recv, "sortInitial"))


@_method("trim", "trim()", "Remove surrounding whitespace.", "{' x '.trim()} → x")
def _trim(recv: Any, args: list[Any]) -> str:
    return _text(recv, "trim").strip()


@_method("truncate", "truncate(length[, breakPattern])", "Shorten to a length, optionally at the last break-pattern match.",
         "{t.truncate(20, /\\s/)}", 1, 2)
def _truncate(recv: Any, args: list[Any]) -> str:
    text = _text(recv, "truncate")
    limit = _arg_int(args[0], "truncate")
    if limit < 0:
        raise MethodError("truncate(): length must not be negative")
    if len(text) <= limit:
        return text
    if len(args) > 1:
        pattern = compile_or_literal(_arg_text(args[1], "truncate"))
        cut = None
        for match in pattern.finditer(check_subject(text[: limit + 1])):
            if match.start() > 0:
                cut = match.start()
        if cut:
            return text[:cut].rstrip()
    return text[:limit].rstrip()


@_method("replaceTrailingBrackets", "replaceTrailingBrackets([replacement])", "Remove (or replace) a trailing (…) or […] group.",
         "{'The Office (US)'.replaceTrailingBrackets()} → The Office", 0, 1)
def _replace_trailing_brackets(recv: Any, args: list[Any]) -> str:
    repl = _arg_text(args[0], "replaceTrailingBrackets") if args else ""
    text = _text(recv, "replaceTrailingBrackets")
    result = re.sub(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])\s*$", lambda _m: repl, text)
    return result if repl else result.rstrip()


@_method("removeBrackets", "removeBrackets()", "Remove every (…), […] and {…} group.", "{'Title (2019) [x]'.removeBrackets()} → Title")
def _remove_brackets(recv: Any, args: list[Any]) -> str:
    text = re.sub(r"\s*(?:\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\})", "", _text(recv, "removeBrackets"))
    return re.sub(r"\s{2,}", " ", text).strip()


@_method("replacePart", "replacePart([replacement])", "Normalize a trailing '(1)' / 'Part 1' marker (default ', Part $1').",
         "{'Dead Reckoning (1)'.replacePart()} → Dead Reckoning, Part 1", 0, 1)
def _replace_part(recv: Any, args: list[Any]) -> str:
    repl = _arg_text(args[0], "replacePart") if args else ", Part $1"
    text = _text(recv, "replacePart")
    match = re.search(r"(?i)(?:\s*[(\[]\s*(?:part\s*)?(\d+)\s*[)\]]|[\s,.:-]+(?:part|pt\.?)\s*(\d+))\s*$", text)
    if not match:
        return text
    number = match.group(1) or match.group(2)
    return text[: match.start()] + repl.replace("$1", number)


_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_LOOKALIKES = str.maketrans({"<": "‹", ">": "›", ":": "∶", '"': "″", "/": "∕", "\\": "⧵", "|": "ǀ", "?": "？", "*": "∗"})


@_method("removeIllegalCharacters", "removeIllegalCharacters()", "Delete characters that are illegal in file names.",
         "{'What?'.removeIllegalCharacters()} → What")
def _remove_illegal(recv: Any, args: list[Any]) -> str:
    return _ILLEGAL_RE.sub("", _text(recv, "removeIllegalCharacters"))


@_method("replaceIllegalCharacters", "replaceIllegalCharacters()", "Map illegal file-name characters to look-alike Unicode.",
         "{'A: B?'.replaceIllegalCharacters()} → A∶ B？")
def _replace_illegal(recv: Any, args: list[Any]) -> str:
    return _text(recv, "replaceIllegalCharacters").translate(_LOOKALIKES)


# --------------------------------------------------------------------------- strings & lists


@_method("take", "take(n)", "First n characters or list items.", "{n.take(4)} → Star", 1)
def _take(recv: Any, args: list[Any]) -> Any:
    count = max(0, _arg_int(args[0], "take"))
    if isinstance(recv, list | tuple):
        return list(recv)[:count]
    return _text(recv, "take")[:count]


@_method("drop", "drop(n)", "Everything after the first n characters or list items.", "{n.drop(9)} → Station", 1)
def _drop(recv: Any, args: list[Any]) -> Any:
    count = max(0, _arg_int(args[0], "drop"))
    if isinstance(recv, list | tuple):
        return list(recv)[count:]
    return _text(recv, "drop")[count:]


@_method("substring", "substring(begin[, end])", "Java substring.", "{n.substring(0, 4)} → Star", 1, 2)
def _substring(recv: Any, args: list[Any]) -> str:
    text = _text(recv, "substring")
    begin = _arg_int(args[0], "substring")
    end = _arg_int(args[1], "substring") if len(args) > 1 else len(text)
    if not 0 <= begin <= end <= len(text):
        raise MethodError(f"substring({begin}, {end}): index out of range for length {len(text)}")
    return text[begin:end]


@_method("join", "join([separator])", "Join a list (null entries dropped).", "{genres.join(', ')} → Drama, Sci-Fi", 0, 1)
def _join(recv: Any, args: list[Any]) -> str:
    sep = _arg_text(args[0], "join") if args else ""
    if not isinstance(recv, list | tuple):
        return to_text(recv)
    return sep.join(to_text(item) for item in recv if item is not None)


@_method("joining", "joining(separator[, prefix, suffix])", "Join non-empty values (undefined when nothing is left).",
         "{[vf, vc].joining(' ', '[', ']')} → [1080p x264]", 1, 3)
def _joining(recv: Any, args: list[Any]) -> str:
    sep = _arg_text(args[0], "joining")
    prefix = _arg_text(args[1], "joining") if len(args) > 1 else ""
    suffix = _arg_text(args[2], "joining") if len(args) > 2 else ""
    items = list(recv) if isinstance(recv, list | tuple) else [recv]
    values = [to_text(item) for item in items if not is_empty(item)]
    if not values:
        raise BindingMissing("joining", "joining(): nothing to join")
    return prefix + sep.join(values) + suffix


def _ends(recv: Any, method: str, index: int) -> Any:
    if isinstance(recv, list | tuple):
        if not recv:
            raise BindingMissing(method, f"{method}(): the list is empty")
        return recv[index]
    text = _text(recv, method)
    if not text:
        raise BindingMissing(method, f"{method}(): the text is empty")
    return text[index]


@_method("first", "first()", "First list item (or character).", "{genres.first()} → Drama")
def _first(recv: Any, args: list[Any]) -> Any:
    return _ends(recv, "first", 0)


@_method("last", "last()", "Last list item (or character).", "{genres.last()} → Sci-Fi")
def _last(recv: Any, args: list[Any]) -> Any:
    return _ends(recv, "last", -1)


def _sort_key(item: Any) -> tuple[int, Any]:
    if is_number(item):
        return (0, item)
    return (1, to_text(item).casefold())


@_method("sort", "sort()", "Sorted copy of a list.", "{genres.sort()}")
def _sort(recv: Any, args: list[Any]) -> list[Any]:
    return sorted((item for item in _list(recv, "sort") if item is not None), key=_sort_key)


@_method("unique", "unique()", "List without duplicates (order kept).", "{alias.unique()}")
def _unique(recv: Any, args: list[Any]) -> list[Any]:
    out: list[Any] = []
    for item in _list(recv, "unique"):
        if not any(groovy_equals(item, seen) for seen in out):
            out.append(item)
    return out


@_method("reverse", "reverse()", "Reversed list or text.", "{'abc'.reverse()} → cba")
def _reverse(recv: Any, args: list[Any]) -> Any:
    if isinstance(recv, list | tuple):
        return list(reversed(recv))
    return _text(recv, "reverse")[::-1]


def _size(recv: Any, method: str) -> int:
    if isinstance(recv, list | tuple):
        return len(recv)
    return len(_text(recv, method))


@_method("size", "size()", "Number of list items or characters.", "{es.size()} → 2")
def _size_method(recv: Any, args: list[Any]) -> int:
    return _size(recv, "size")


@_method("length", "length()", "Number of characters.", "{n.length()} → 16")
def _length(recv: Any, args: list[Any]) -> int:
    return _size(recv, "length")


@_method("isEmpty", "isEmpty()", "True for an empty text or list.", "{tags.isEmpty()} → false")
def _is_empty(recv: Any, args: list[Any]) -> bool:
    if isinstance(recv, list | tuple | str):
        return len(recv) == 0
    return False


@_method("contains", "contains(value)", "List membership or substring test.", "{genres.contains('Drama')} → true", 1)
def _contains(recv: Any, args: list[Any]) -> bool:
    if isinstance(recv, list | tuple):
        return any(groovy_equals(item, args[0]) for item in recv)
    return _arg_text(args[0], "contains") in _text(recv, "contains")


@_method("startsWith", "startsWith(prefix)", "Text starts with a prefix.", "{n.startsWith('Star')} → true", 1)
def _starts_with(recv: Any, args: list[Any]) -> bool:
    return _text(recv, "startsWith").startswith(_arg_text(args[0], "startsWith"))


@_method("endsWith", "endsWith(suffix)", "Text ends with a suffix.", "{n.endsWith('Station')} → true", 1)
def _ends_with(recv: Any, args: list[Any]) -> bool:
    return _text(recv, "endsWith").endswith(_arg_text(args[0], "endsWith"))


@_method("equalsIgnoreCase", "equalsIgnoreCase(text)", "Case-insensitive comparison.", "{vf.equalsIgnoreCase('1080P')} → true", 1)
def _equals_ignore_case(recv: Any, args: list[Any]) -> bool:
    return _text(recv, "equalsIgnoreCase").casefold() == _arg_text(args[0], "equalsIgnoreCase").casefold()


@_method("indexOf", "indexOf(value)", "Position of a value or substring (-1 when absent).", "{n.indexOf(' ')} → 8", 1)
def _index_of(recv: Any, args: list[Any]) -> int:
    if isinstance(recv, list | tuple):
        for i, item in enumerate(recv):
            if groovy_equals(item, args[0]):
                return i
        return -1
    return _text(recv, "indexOf").find(_arg_text(args[0], "indexOf"))


@_method("max", "max()", "Largest list item.", "{es.max()} → 3")
def _max(recv: Any, args: list[Any]) -> Any:
    items = [item for item in _list(recv, "max") if item is not None]
    if not items:
        raise BindingMissing("max", "max(): the list is empty")
    return max(items, key=_sort_key)


@_method("min", "min()", "Smallest list item.", "{es.min()} → 2")
def _min(recv: Any, args: list[Any]) -> Any:
    items = [item for item in _list(recv, "min") if item is not None]
    if not items:
        raise BindingMissing("min", "min(): the list is empty")
    return min(items, key=_sort_key)


@_method("sum", "sum()", "Sum of a list of numbers.", "{es.sum()} → 5")
def _sum(recv: Any, args: list[Any]) -> Any:
    total: int | float = 0
    for item in _list(recv, "sum"):
        if item is not None:
            total += _number(item, "sum")
    return total


@_method("plus", "plus(value)", "Same as '+'.", "{n.plus(' (US)')}", 1)
def _plus(recv: Any, args: list[Any]) -> Any:
    return op_add(recv, args[0])


@_method("minus", "minus(value)", "Same as '-' (text: remove the first occurrence).", "{n.minus('The ')}", 1)
def _minus(recv: Any, args: list[Any]) -> Any:
    return op_sub(recv, args[0])


@_method("compareTo", "compareTo(value)", "-1, 0 or 1.", "{y.compareTo(2000)} → 1", 1)
def _compare_to(recv: Any, args: list[Any]) -> int:
    return op_compare(recv, args[0])


# --------------------------------------------------------------------------- format()

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"]  # fmt: skip
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def format_date(value: date, pattern: str) -> str:
    """Java DateTimeFormatter-style patterns (English names, locale independent)."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    hour = value.hour if isinstance(value, datetime) else 0
    minute = value.minute if isinstance(value, datetime) else 0
    second = value.second if isinstance(value, datetime) else 0
    while i < n:
        ch = pattern[i]
        if ch == "'":
            close = pattern.find("'", i + 1)
            if close == -1:
                raise MethodError("format(): unterminated quote in pattern")
            out.append(pattern[i + 1 : close] if close > i + 1 else "'")
            i = close + 1
            continue
        if not ch.isalpha():
            out.append(ch)
            i += 1
            continue
        j = i
        while j < n and pattern[j] == ch:
            j += 1
        count = j - i
        if ch in "yu":
            out.append(f"{value.year % 100:02d}" if count == 2 else f"{value.year:0{max(count, 4)}d}")
        elif ch in "ML":
            if count >= 4:
                out.append(_MONTHS[value.month - 1])
            elif count == 3:
                out.append(_MONTHS[value.month - 1][:3])
            else:
                out.append(f"{value.month:0{count}d}")
        elif ch == "d":
            out.append(f"{value.day:0{count}d}")
        elif ch == "D":
            out.append(f"{value.timetuple().tm_yday:0{count}d}")
        elif ch == "E":
            name = _DAYS[value.weekday()]
            out.append(name if count >= 4 else name[:3])
        elif ch == "H":
            out.append(f"{hour:0{count}d}")
        elif ch == "m":
            out.append(f"{minute:0{count}d}")
        elif ch == "s":
            out.append(f"{second:0{count}d}")
        else:
            raise MethodError(f"format(): unsupported date pattern letter '{ch}'")
        i = j
    return "".join(out)


def format_decimal(value: int | float, pattern: str) -> str:
    """DecimalFormat-ish: '0', '0.0', '#.##', '#,##0', '000' with optional literal prefix/suffix."""
    match = re.search(r"[#0,]*\.?[#0]*", pattern)
    core = match.group(0) if match and match.group(0) else ""
    if not core or not re.search(r"[#0]", core):
        raise MethodError(f"format(): unsupported number pattern '{pattern}'")
    prefix, suffix = pattern[: match.start()], pattern[match.end() :]
    int_part, _, frac_part = core.partition(".")
    min_int = int_part.count("0")
    grouping = "," in int_part
    min_frac = frac_part.count("0")
    max_frac = len(frac_part)
    try:
        quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-max_frac), rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise MethodError("format(): number out of range") from exc
    negative = quantized < 0
    whole, _, frac = f"{abs(quantized):f}".partition(".")
    frac = frac.rstrip("0").ljust(min_frac, "0")
    whole_int = int(whole)
    whole_str = str(whole_int) if (whole_int or min_int) else ""
    if grouping and whole_str:
        whole_str = f"{whole_int:,}"
    whole_str = whole_str.rjust(min_int, "0")
    text = whole_str + ("." + frac if frac else "")
    return prefix + ("-" if negative else "") + (text or "0") + suffix


@_method("format", "format(pattern)", "Format a date ('yyyy-MM-dd', 'MMM d, yyyy') or a number ('0.0', '#,##0').",
         "{d.format('yyyy.MM.dd')} → 2024.03.04", 1)
def _format(recv: Any, args: list[Any]) -> str:
    pattern = _arg_text(args[0], "format")
    if isinstance(recv, date):
        return format_date(recv, pattern)
    if is_number(recv):
        return format_decimal(recv, pattern)
    raise MethodError(f"format() needs a date or a number, got {type_name(recv)}")


# --------------------------------------------------------------------------- aliases

_alias("lowerCase", "toLowerCase", "lowerCase()", "Alias of lower().", "{n.lowerCase()}")
_alias("upperCase", "toUpperCase", "upperCase()", "Alias of upper().", "{n.upperCase()}")


def call_method(name: str, receiver: Any, args: list[Any]) -> Any:
    """Dispatch a whitelisted method. Raises MethodError for unknown names / arity."""
    spec = METHODS.get(name)
    if spec is None:
        if name in UNSUPPORTED_FILEBOT_METHODS:
            raise MethodError(f"Unsupported FileBot method '{name}()'")
        raise MethodError(f"Unknown method '{name}()'")
    if not spec.accepts(len(args)):
        raise MethodError(f"{name}() takes {spec.arity_text()} argument(s), got {len(args)}")
    if receiver is None:
        raise MethodError(f"Cannot call {name}() on null")
    # Replacement/separator arguments multiply the receiver's length (s.replace('x', <huge>)):
    # bounding them keeps every method's output within a few MB before the evaluator's length check.
    if any(isinstance(arg, str) and len(arg) > MAX_ARG_LENGTH for arg in args):
        raise MethodError(f"{name}(): text arguments are limited to {MAX_ARG_LENGTH} characters")
    if isinstance(receiver, BindingObject) and name not in ("toString",):
        receiver = str(receiver)
    return spec.fn(receiver, args)
