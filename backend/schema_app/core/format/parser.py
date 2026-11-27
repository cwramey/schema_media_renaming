"""Pratt parser for format expressions (SPEC §8.2).

Precedence, lowest to highest::

    ternary  a ? b : c          (right associative; branches are full expressions)
    elvis    a ?: b
    or       ||
    and      &&
    equality == !=
    compare  < <= > >=
    sum      + -
    product  * %
    unary    ! - +
    postfix  .name  .name(args)  ?.name  [index]

Primaries: strings, numbers, true/false/null, identifiers (bindings), ``( expr )``, list literals
``[a, b]`` and ``any{…}{…}`` / ``allOf{…}{…}`` closures. Every node carries absolute start/end
offsets into the format string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schema_app.core.format.errors import FormatSyntaxError
from schema_app.core.format.lexer import EOF, GSTR, IDENT, NUM, OP, STR, Token

# --------------------------------------------------------------------------- AST


@dataclass(slots=True)
class Node:
    start: int
    end: int


@dataclass(slots=True)
class Lit(Node):
    value: Any


@dataclass(slots=True)
class Ident(Node):
    name: str


@dataclass(slots=True)
class ListLit(Node):
    items: list[Node]


@dataclass(slots=True)
class Member(Node):
    obj: Node
    name: str
    safe: bool = False


@dataclass(slots=True)
class Call(Node):
    obj: Node
    name: str
    args: list[Node]
    safe: bool = False
    name_start: int = 0
    name_end: int = 0


@dataclass(slots=True)
class Index(Node):
    obj: Node
    index: Node


@dataclass(slots=True)
class Unary(Node):
    op: str
    operand: Node


@dataclass(slots=True)
class Binary(Node):
    op: str
    left: Node
    right: Node


@dataclass(slots=True)
class Elvis(Node):
    left: Node
    right: Node


@dataclass(slots=True)
class Ternary(Node):
    cond: Node
    then: Node
    other: Node


@dataclass(slots=True)
class Closures(Node):
    kind: str  # "any" | "allOf"
    bodies: list[Node]


@dataclass(slots=True)
class GStr(Node):
    parts: list[str | Node] = field(default_factory=list)


# --------------------------------------------------------------------------- constants

FILEBOT_FUNCTIONS = frozenset(
    {
        "none", "count", "list", "concat", "component", "isEmptyValue", "quote", "quoteBash",
        "quotePowerShell", "csv", "lines", "text", "json", "xml", "html", "include", "toJson",
        "milliseconds", "OpenAI", "println", "print", "sprintf",
    }
)  # fmt: skip

_UNSUPPORTED_KEYWORDS = {
    "def": "Variable definitions ('def') are not supported",
    "new": "Object construction ('new') is not supported",
    "return": "'return' is not supported; a block's value is its expression",
    "if": "'if' statements are not supported; use 'cond ? a : b'",
    "else": "'else' is not supported; use 'cond ? a : b'",
    "for": "Loops are not supported",
    "while": "Loops are not supported",
    "import": "Imports are not supported",
    "class": "Class definitions are not supported",
    "switch": "'switch' is not supported; use 'cond ? a : b'",
    "try": "'try' is not supported; use any{…}{…} for fallbacks",
    "catch": "'catch' is not supported",
    "throw": "'throw' is not supported",
    "assert": "'assert' is not supported",
    "this": "'this' is not supported",
    "super": "'super' is not supported",
}

_INFIX_KEYWORDS = {
    "in": "The 'in' operator is not supported; use list.contains(x)",
    "as": "Type coercion ('as') is not supported; use .toInteger() / .toString()",
    "instanceof": "'instanceof' is not supported",
}

_BINARY_BP = {
    "||": 30,
    "&&": 40,
    "==": 50,
    "!=": 50,
    "<": 60,
    "<=": 60,
    ">": 60,
    ">=": 60,
    "+": 70,
    "-": 70,
    "*": 80,
    "%": 80,
}
_TERNARY_BP = 10
_ELVIS_BP = 20
_UNARY_BP = 90
_POSTFIX_BP = 100


def _describe(tok: Token) -> str:
    if tok.kind == EOF:
        return "end of block"
    if tok.kind in (STR, GSTR):
        return "string"
    if tok.kind == NUM:
        return f"number {tok.value}"
    return f"'{tok.value}'"


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    # ------------------------------------------------------------------ helpers
    def peek(self, offset: int = 0) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return tok

    def is_op(self, value: str, offset: int = 0) -> bool:
        tok = self.peek(offset)
        return tok.kind == OP and tok.value == value

    def expect_op(self, value: str, context: str) -> Token:
        tok = self.peek()
        if tok.kind == OP and tok.value == value:
            return self.advance()
        raise FormatSyntaxError(f"Expected '{value}' {context}, found {_describe(tok)}", tok.start, max(tok.end, tok.start + 1))

    # ------------------------------------------------------------------ entry
    def parse_block(self) -> Node:
        node = self.expression(0)
        tok = self.peek()
        if tok.kind != EOF:
            if tok.kind == OP and tok.value == "{":
                raise FormatSyntaxError(
                    "Closures are only supported in any{…} and allOf{…}", tok.start, tok.end
                )
            raise FormatSyntaxError(f"Unexpected {_describe(tok)}", tok.start, max(tok.end, tok.start + 1))
        return node

    # ------------------------------------------------------------------ pratt core
    def lbp(self, tok: Token) -> int:
        if tok.kind == IDENT and tok.value in _INFIX_KEYWORDS:
            return _POSTFIX_BP + 1  # trigger a helpful error in led()
        if tok.kind != OP:
            return 0
        value = tok.value
        if value == "?":
            return _TERNARY_BP
        if value == "?:":
            return _ELVIS_BP
        if value in _BINARY_BP:
            return _BINARY_BP[value]
        if value in (".", "?.", "[", "(", "{"):
            return _POSTFIX_BP
        return 0

    def expression(self, rbp: int) -> Node:
        tok = self.advance()
        left = self.nud(tok)
        while rbp < self.lbp(self.peek()):
            tok = self.advance()
            left = self.led(tok, left)
        return left

    def nud(self, tok: Token) -> Node:
        if tok.kind == NUM:
            return Lit(tok.start, tok.end, tok.value)
        if tok.kind == STR:
            return Lit(tok.start, tok.end, tok.value)
        if tok.kind == GSTR:
            parts: list[str | Node] = []
            for part in tok.value:
                if isinstance(part, str):
                    parts.append(part)
                else:
                    parts.append(_Parser(part).parse_block())
            return GStr(tok.start, tok.end, parts)
        if tok.kind == IDENT:
            return self.identifier(tok)
        if tok.kind == OP:
            value = tok.value
            if value == "(":
                inner = self.expression(0)
                close = self.expect_op(")", "to close '('")
                return _with_span(inner, tok.start, close.end)
            if value == "[":
                return self.list_literal(tok)
            if value in ("-", "!", "+"):
                operand = self.expression(_UNARY_BP)
                return Unary(tok.start, operand.end, value, operand)
            if value == "{":
                raise FormatSyntaxError("Closures are only supported in any{…} and allOf{…}", tok.start, tok.end)
        if tok.kind == EOF:
            raise FormatSyntaxError("Expected an expression", tok.start, tok.start + 1)
        raise FormatSyntaxError(f"Unexpected {_describe(tok)}", tok.start, max(tok.end, tok.start + 1))

    def identifier(self, tok: Token) -> Node:
        name = tok.value
        if name == "true":
            return Lit(tok.start, tok.end, True)
        if name == "false":
            return Lit(tok.start, tok.end, False)
        if name == "null":
            return Lit(tok.start, tok.end, None)
        if name in ("any", "allOf"):
            if not self.is_op("{"):
                raise FormatSyntaxError(f"Use {name}{{…}}{{…}} with closures", tok.start, tok.end)
            bodies: list[Node] = []
            end = tok.end
            while self.is_op("{"):
                open_tok = self.advance()
                if self.is_op("}"):
                    raise FormatSyntaxError("Empty closure", open_tok.start, self.peek().end)
                bodies.append(self.expression(0))
                end = self.expect_op("}", f"to close the {name}{{…}} closure").end
            return Closures(tok.start, end, name, bodies)
        if name in _UNSUPPORTED_KEYWORDS:
            raise FormatSyntaxError(_UNSUPPORTED_KEYWORDS[name], tok.start, tok.end)
        if name in _INFIX_KEYWORDS:
            raise FormatSyntaxError(f"Unexpected keyword '{name}'", tok.start, tok.end)
        if self.is_op("{") and name in FILEBOT_FUNCTIONS:
            raise FormatSyntaxError(f"FileBot function '{name}{{…}}' is not supported", tok.start, tok.end)
        return Ident(tok.start, tok.end, name)

    def list_literal(self, open_tok: Token) -> Node:
        items: list[Node] = []
        while not self.is_op("]"):
            items.append(self.expression(0))
            if self.is_op(":"):
                tok = self.peek()
                raise FormatSyntaxError("Map literals ([key: value]) are not supported", tok.start, tok.end)
            if self.is_op(","):
                self.advance()
                continue
            if not self.is_op("]"):
                tok = self.peek()
                raise FormatSyntaxError(
                    f"Expected ',' or ']' in list, found {_describe(tok)}", tok.start, max(tok.end, tok.start + 1)
                )
        close = self.advance()
        return ListLit(open_tok.start, close.end, items)

    def led(self, tok: Token, left: Node) -> Node:
        if tok.kind == IDENT:
            raise FormatSyntaxError(_INFIX_KEYWORDS[tok.value], tok.start, tok.end)
        value = tok.value
        if value == "?":
            then = self.expression(0)
            self.expect_op(":", "in 'cond ? a : b'")
            other = self.expression(_TERNARY_BP - 1)
            return Ternary(left.start, other.end, left, then, other)
        if value == "?:":
            right = self.expression(_ELVIS_BP)
            return Elvis(left.start, right.end, left, right)
        if value in _BINARY_BP:
            right = self.expression(_BINARY_BP[value])
            return Binary(left.start, right.end, value, left, right)
        if value in (".", "?."):
            name_tok = self.advance()
            if name_tok.kind != IDENT:
                raise FormatSyntaxError(
                    f"Expected a property or method name after '{value}'", name_tok.start, max(name_tok.end, name_tok.start + 1)
                )
            safe = value == "?."
            if self.is_op("("):
                self.advance()
                args: list[Node] = []
                while not self.is_op(")"):
                    args.append(self.expression(0))
                    if self.is_op(","):
                        self.advance()
                        continue
                    if not self.is_op(")"):
                        nxt = self.peek()
                        raise FormatSyntaxError(
                            f"Expected ',' or ')' in arguments, found {_describe(nxt)}", nxt.start, max(nxt.end, nxt.start + 1)
                        )
                close = self.advance()
                if self.is_op("{"):
                    brace = self.peek()
                    raise FormatSyntaxError(
                        f"Closures after methods ('.{name_tok.value}(…){{…}}') are not supported", brace.start, brace.end
                    )
                return Call(left.start, close.end, left, name_tok.value, args, safe, name_tok.start, name_tok.end)
            if self.is_op("{"):
                brace = self.peek()
                raise FormatSyntaxError(
                    f"Closures after methods ('.{name_tok.value}{{…}}') are not supported", brace.start, brace.end
                )
            return Member(left.start, name_tok.end, left, name_tok.value, safe)
        if value == "[":
            index = self.expression(0)
            close = self.expect_op("]", "to close '['")
            return Index(left.start, close.end, left, index)
        if value == "(":
            if isinstance(left, Ident):
                if left.name in FILEBOT_FUNCTIONS or left.name in ("any", "allOf"):
                    raise FormatSyntaxError(
                        f"FileBot function '{left.name}()' is not supported", left.start, tok.end
                    )
                raise FormatSyntaxError(
                    f"Function calls ('{left.name}(…)') are not supported; use value.method(…)", left.start, tok.end
                )
            raise FormatSyntaxError("Calling a value is not supported", tok.start, tok.end)
        if value == "{":
            raise FormatSyntaxError("Closures are only supported in any{…} and allOf{…}", tok.start, tok.end)
        raise FormatSyntaxError(f"Unexpected {_describe(tok)}", tok.start, max(tok.end, tok.start + 1))


def _with_span(node: Node, start: int, end: int) -> Node:
    """Parenthesized expressions keep their inner node but widen the span for error reporting."""
    node.start = start
    node.end = end
    return node


def parse_tokens(tokens: list[Token]) -> Node:
    """Parse one block's tokens (terminated by EOF) into an AST."""
    return _Parser(tokens).parse_block()


def iter_nodes(node: Node):
    """Depth-first walk over every node (including GString parts)."""
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, ListLit):
            stack.extend(reversed(current.items))
        elif isinstance(current, Member):
            stack.append(current.obj)
        elif isinstance(current, Call):
            stack.extend(reversed(current.args))
            stack.append(current.obj)
        elif isinstance(current, Index):
            stack.append(current.index)
            stack.append(current.obj)
        elif isinstance(current, Unary):
            stack.append(current.operand)
        elif isinstance(current, Binary | Elvis):
            stack.append(current.right)
            stack.append(current.left)
        elif isinstance(current, Ternary):
            stack.extend([current.other, current.then, current.cond])
        elif isinstance(current, Closures):
            stack.extend(reversed(current.bodies))
        elif isinstance(current, GStr):
            stack.extend(part for part in reversed(current.parts) if isinstance(part, Node))
