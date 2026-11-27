"""Template scanner and expression tokenizer.

A format is literal text with ``{…}`` expression blocks. ``{{`` / ``}}`` outside a block emit a
literal brace. Inside a block, braces are depth-counted (``any{…}{…}`` closures) and string
literals are skipped, so ``{'}'}`` works. Offsets in every token are absolute positions in the
whole format string, so errors point at the right characters in the editor.

Supported literals: ``'single'``, ``"double"`` (with ``${expr}`` / ``$name.prop`` interpolation,
Groovy GString style) and slashy ``/regex/`` strings (Groovy style; there is no division operator,
so ``/`` inside an expression can only start a regex string).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schema_app.core.format.errors import FormatSyntaxError

NUM = "NUM"
STR = "STR"
GSTR = "GSTR"
IDENT = "IDENT"
OP = "OP"
EOF = "EOF"


@dataclass(slots=True)
class Token:
    kind: str
    value: Any
    start: int
    end: int
    regex: bool = False  # slashy string


@dataclass(slots=True)
class LiteralSegment:
    text: str
    start: int
    end: int


@dataclass(slots=True)
class BlockSegment:
    tokens: list[Token]
    start: int  # offset of "{"
    end: int  # offset after "}"
    source: str = field(default="")


_SUPPORTED_OPS = (
    "?.", "?:", "==", "!=", "<=", ">=", "&&", "||",
    "+", "-", "*", "%", "!", "<", ">", "?", ":", ".", ",", "(", ")", "[", "]",
)  # fmt: skip

_REGEX_OP_MSG = "Regex operators (=~, ==~) are not supported; use .matches(/re/) or .match(/re/)"
_UNSUPPORTED_OPS: dict[str, str] = {
    "==~": _REGEX_OP_MSG,
    "<=>": "The spaceship operator '<=>' is not supported",
    "=~": _REGEX_OP_MSG,
    "**": "The power operator '**' is not supported",
    "++": "Increment/decrement operators are not supported",
    "--": "Increment/decrement operators are not supported",
    "->": "Closures with parameters ('->') are not supported",
    "..": "Ranges ('a..b') are not supported; use .take(n) or .substring(a, b)",
    "<<": "Shift operators are not supported",
    ">>": "Shift operators are not supported",
    "*.": "The spread operator '*.' is not supported",
    ".&": "Method pointers ('.&') are not supported",
    ".@": "Direct field access ('.@') is not supported",
    "=": "Assignment ('=') is not supported; use '==' to compare",
    ";": "Multiple statements (';') are not supported; use one expression per {…} block",
    "&": "Bitwise operators are not supported (use '&&')",
    "|": "Bitwise operators are not supported (use '||')",
    "^": "Bitwise operators are not supported",
    "~": "The '~' operator is not supported",
}

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0",
    "\\": "\\", "'": "'", '"': '"', "$": "$", "/": "/",
}  # fmt: skip


def _ident_start(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


def _ident_part(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


class _Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.n = len(text)

    # ------------------------------------------------------------------ blocks
    def lex_block(self, pos: int, open_pos: int) -> tuple[list[Token], int]:
        """Tokenize from ``pos`` up to the ``}`` matching the ``{`` at ``open_pos``.

        Returns the tokens (terminated by an EOF token placed at the closing brace) and the
        offset just after that brace.
        """
        text, n = self.text, self.n
        tokens: list[Token] = []
        depth = 0
        i = pos
        while True:
            while i < n and text[i].isspace():
                i += 1
            if i >= n:
                raise FormatSyntaxError("Missing '}' to close this block", open_pos, n)
            ch = text[i]
            if ch == "}":
                if depth == 0:
                    tokens.append(Token(EOF, None, i, i))
                    return tokens, i + 1
                depth -= 1
                tokens.append(Token(OP, "}", i, i + 1))
                i += 1
                continue
            if ch == "{":
                depth += 1
                tokens.append(Token(OP, "{", i, i + 1))
                i += 1
                continue
            if ch == "'":
                token, i = self._single(i)
            elif ch == '"':
                token, i = self._double(i)
            elif ch == "/":
                token, i = self._slashy(i)
            elif ch.isdigit():
                token, i = self._number(i)
            elif _ident_start(ch):
                j = i + 1
                while j < n and _ident_part(text[j]):
                    j += 1
                token = Token(IDENT, text[i:j], i, j)
                i = j
            else:
                token, i = self._operator(i)
            tokens.append(token)

    # ------------------------------------------------------------------ literals
    def _escape(self, j: int, start: int) -> tuple[str, int]:
        """``self.text[j]`` is a backslash. Returns (decoded char, next offset)."""
        text = self.text
        if j + 1 >= self.n:
            raise FormatSyntaxError("Unterminated string", start, self.n)
        nxt = text[j + 1]
        if nxt == "u" and j + 6 <= self.n:
            digits = text[j + 2 : j + 6]
            try:
                return chr(int(digits, 16)), j + 6
            except ValueError as exc:
                raise FormatSyntaxError("Invalid \\u escape", j, j + 6) from exc
        return _ESCAPES.get(nxt, nxt), j + 2

    def _single(self, i: int) -> tuple[Token, int]:
        text, n = self.text, self.n
        triple = text.startswith("'''", i)
        quote = "'''" if triple else "'"
        j = i + len(quote)
        buf: list[str] = []
        while j < n:
            if text[j] == "\\":
                ch, j = self._escape(j, i)
                buf.append(ch)
                continue
            if text.startswith(quote, j):
                end = j + len(quote)
                return Token(STR, "".join(buf), i, end), end
            buf.append(text[j])
            j += 1
        raise FormatSyntaxError("Unterminated string", i, n)

    def _double(self, i: int) -> tuple[Token, int]:
        text, n = self.text, self.n
        triple = text.startswith('"""', i)
        quote = '"""' if triple else '"'
        j = i + len(quote)
        parts: list[str | list[Token]] = []
        buf: list[str] = []
        interpolated = False
        while j < n:
            ch = text[j]
            if ch == "\\":
                decoded, j = self._escape(j, i)
                buf.append(decoded)
                continue
            if text.startswith(quote, j):
                end = j + len(quote)
                if buf:
                    parts.append("".join(buf))
                if not interpolated:
                    return Token(STR, "".join(p for p in parts if isinstance(p, str)), i, end), end
                return Token(GSTR, parts, i, end), end
            if ch == "$" and j + 1 < n and text[j + 1] == "{":
                if buf:
                    parts.append("".join(buf))
                    buf = []
                tokens, j2 = self.lex_block(j + 2, j + 1)
                if len(tokens) == 1:
                    raise FormatSyntaxError("Empty ${} interpolation", j, j2)
                parts.append(tokens)
                interpolated = True
                j = j2
                continue
            if ch == "$" and j + 1 < n and _ident_start(text[j + 1]):
                if buf:
                    parts.append("".join(buf))
                    buf = []
                tokens: list[Token] = []
                k = j + 1
                while True:
                    s = k
                    while k < n and _ident_part(text[k]):
                        k += 1
                    tokens.append(Token(IDENT, text[s:k], s, k))
                    if k + 1 < n and text[k] == "." and _ident_start(text[k + 1]):
                        tokens.append(Token(OP, ".", k, k + 1))
                        k += 1
                        continue
                    break
                tokens.append(Token(EOF, None, k, k))
                parts.append(tokens)
                interpolated = True
                j = k
                continue
            buf.append(ch)
            j += 1
        raise FormatSyntaxError("Unterminated string", i, n)

    def _slashy(self, i: int) -> tuple[Token, int]:
        text, n = self.text, self.n
        if text.startswith("//", i) or text.startswith("/*", i):
            raise FormatSyntaxError("Comments are not supported in format expressions", i, i + 2)
        j = i + 1
        buf: list[str] = []
        while j < n:
            ch = text[j]
            if ch == "\\" and j + 1 < n and text[j + 1] == "/":
                buf.append("/")
                j += 2
                continue
            if ch == "/":
                return Token(STR, "".join(buf), i, j + 1, regex=True), j + 1
            buf.append(ch)
            j += 1
        raise FormatSyntaxError(
            "Unterminated /regex/ string (there is no division operator; '/' starts a regex)", i, n
        )

    def _number(self, i: int) -> tuple[Token, int]:
        text, n = self.text, self.n
        j = i
        while j < n and text[j].isdigit():
            j += 1
        is_float = False
        if j + 1 < n and text[j] == "." and text[j + 1].isdigit():
            is_float = True
            j += 1
            while j < n and text[j].isdigit():
                j += 1
        raw = text[i:j]
        # Groovy numeric suffixes (1L, 2.5d, 3G ...) are accepted and ignored.
        if j < n and text[j] in "lLgGdDfFiI" and not (j + 1 < n and _ident_part(text[j + 1])):
            j += 1
        if j < n and _ident_start(text[j]):
            raise FormatSyntaxError(f"Invalid number '{text[i:j + 1]}'", i, j + 1)
        value: int | float = float(raw) if is_float else int(raw)
        return Token(NUM, value, i, j), j

    def _operator(self, i: int) -> tuple[Token, int]:
        text = self.text
        for width in (3, 2, 1):
            chunk = text[i : i + width]
            if len(chunk) != width:
                continue
            if chunk in _UNSUPPORTED_OPS:
                raise FormatSyntaxError(_UNSUPPORTED_OPS[chunk], i, i + width)
            if chunk in _SUPPORTED_OPS:
                return Token(OP, chunk, i, i + width), i + width
        raise FormatSyntaxError(f"Unexpected character '{text[i]}'", i, i + 1)


def scan_template(text: str) -> list[LiteralSegment | BlockSegment]:
    """Split a format into literal segments and tokenized ``{…}`` blocks."""
    lexer = _Lexer(text)
    segments: list[LiteralSegment | BlockSegment] = []
    buf: list[str] = []
    lit_start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            if text.startswith("{{", i):
                buf.append("{")
                i += 2
                continue
            if buf:
                segments.append(LiteralSegment("".join(buf), lit_start, i))
                buf = []
            tokens, end = lexer.lex_block(i + 1, i)
            if len(tokens) == 1:
                raise FormatSyntaxError("Empty expression block '{}'", i, end)
            segments.append(BlockSegment(tokens, i, end, text[i:end]))
            i = end
            lit_start = i
            continue
        if ch == "}":
            if text.startswith("}}", i):
                buf.append("}")
                i += 2
                continue
            raise FormatSyntaxError("Unexpected '}' (use '}}' for a literal brace)", i, i + 1)
        buf.append(ch)
        i += 1
    if buf:
        segments.append(LiteralSegment("".join(buf), lit_start, n))
    return segments
