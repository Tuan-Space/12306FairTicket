"""Conservatively read the official inline quiet-carriage capability.

Only an unambiguous top-level literal declaration is accepted.  This is a
restricted lexical reader, not a JavaScript evaluator: unfamiliar expressions
and conflicting writes leave the optional preference disabled.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
import re


class _InlineScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.saw_tag = False
        self._parts: list[str] | None = None
        self._inert_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.saw_tag = True
        if tag in {"template", "noscript", "textarea", "title"}:
            self._inert_tags.append(tag)
            return
        if tag != "script":
            return
        attributes = dict(attrs)
        script_type = (attributes.get("type") or "").strip().lower()
        executable = script_type in {"", "text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript"}
        self._parts = [] if executable and "src" not in attributes and not self._inert_tags else None

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        self.saw_tag = True
        if tag in self._inert_tags:
            self._inert_tags = self._inert_tags[:self._inert_tags.index(tag)]
            return
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    depth: int


_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_OPERATORS = re.compile(r"(?:>>>=|===|!==|\*\*=|&&=|\|\|=|\?\?=|<<=|>>=|=>|==|!=|<=|>=|&&|\|\||\?\?|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<|>>>|>>|\*\*)")
_WRITES = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "&&=", "||=", "??=", "**=", "<<=", ">>=", ">>>=", "++", "--"}


def _tokens(source: str) -> list[_Token] | None:
    result: list[_Token] = []
    stack: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith(("//", "<!--"), index):
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                return None
            index = end + 2
            continue
        if char in "\"'`":
            quote, start = char, index
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                elif source[index] == quote:
                    break
                else:
                    index += 1
            if index >= len(source):
                return None
            raw = source[start + 1:index]
            if quote == "`" and "${" in raw and "is_jy" in raw:
                return None
            result.append(_Token("template" if quote == "`" else "string", raw, len(stack)))
            index += 1
            continue
        # Ignore regular-expression bodies too: their text is not executable
        # declarations. Ambiguous/unclosed regex syntax disables the preference.
        previous = result[-1].value if result else ""
        if char == "/" and previous in {"", "=", "(", ")", "[", "{", ",", ":", ";", "!", "?", "return", "=>", "&&", "||"}:
            index += 1
            in_class = False
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "[":
                    in_class = True
                elif source[index] == "]":
                    in_class = False
                elif source[index] == "/" and not in_class:
                    break
                elif source[index] in "\r\n":
                    return None
                index += 1
            if index >= len(source):
                return None
            index += 1
            while index < len(source) and source[index].isalpha():
                index += 1
            result.append(_Token("regex", "", len(stack)))
            continue
        identifier = _IDENTIFIER.match(source, index)
        if identifier:
            result.append(_Token("identifier", identifier.group(), len(stack)))
            index = identifier.end()
            continue
        operator = _OPERATORS.match(source, index)
        value = operator.group() if operator else char
        result.append(_Token("punctuation", value, len(stack)))
        if char in "{([":
            stack.append(char)
        elif char in "})]":
            if not stack or stack.pop() != {"}": "{", ")": "(", "]": "["}[char]:
                return None
        index += len(value)
    return None if stack else result


def extract_quiet_carriage_available(html: str) -> bool:
    """Accept exactly one top-level ``var/let/const is_jy = 'Y'`` literal."""

    if not isinstance(html, str):
        return False
    parser = _InlineScripts()
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError):
        return False
    scripts = parser.scripts if parser.saw_tag else [html]
    declarations: list[bool] = []
    for script in scripts:
        tokens = _tokens(script)
        if tokens is None:
            return False
        for index, token in enumerate(tokens):
            if token.value != "is_jy":
                continue
            before = tokens[index - 1] if index else None
            after = tokens[index + 1] if index + 1 < len(tokens) else None
            # A quoted property name may be used in a computed/dynamic write.
            # Such constructs are deliberately unsupported, even if seemingly
            # harmless, rather than guessing the effect of JavaScript.
            if token.kind == "string":
                return False
            writing = (after is not None and after.value in _WRITES | {":"}) or (before is not None and before.value in {"++", "--"})
            if not writing:
                continue
            literal = tokens[index + 2] if index + 2 < len(tokens) else None
            terminator = tokens[index + 3] if index + 3 < len(tokens) else None
            boundary = tokens[index - 2] if index >= 2 else None
            supported = (
                token.kind == "identifier" and token.depth == 0
                and before is not None and before.kind == "identifier"
                and before.value in {"var", "let", "const"}
                and (boundary is None or boundary.value == ";")
                and after is not None and after.value == "="
                and literal is not None and literal.kind == "string" and literal.value == "Y"
                and (terminator is None or terminator.value == ";")
            )
            declarations.append(supported)
    return declarations == [True]
