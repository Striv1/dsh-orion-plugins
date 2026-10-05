"""Flat rule atoms and safe negation shared by authoring and execution."""
from __future__ import annotations

import re

PARSED_FACT = re.compile(
    r"^(?P<predicate>[A-Za-z_][A-Za-z0-9_:-]{0,127})\((?P<arguments>.*)\)$"
)

def parse_atom(expression: str) -> tuple[str, list[str]]:
    """Parse flat atoms without treating commas inside quoted literals as syntax."""
    if not isinstance(expression, str):
        raise ValueError("reasoning atom must be a string")
    matched = PARSED_FACT.fullmatch(expression.strip())
    if matched is None:
        raise ValueError(f"invalid reasoning atom: {expression}")
    arguments = []
    current: list[str] = []
    quote = None
    escaped = False
    for character in matched.group("arguments"):
        if quote is not None:
            current.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in "\"'":
            if "".join(current).strip():
                raise ValueError("quoted literal must occupy the entire argument")
            quote = character
            current.append(character)
        elif character == ",":
            arguments.append("".join(current).strip())
            current = []
        elif character in "()":
            raise ValueError("nested reasoning atoms are unsupported")
        else:
            current.append(character)
    if quote is not None or escaped:
        raise ValueError("unterminated reasoning literal")
    arguments.append("".join(current).strip())
    for argument in arguments:
        if not argument:
            raise ValueError("empty reasoning argument")
        if argument[0] in "\"'":
            # Reject suffixes/adjacent literals; preserve quoted bytes exactly.
            literal = re.fullmatch(r"(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", argument, re.S)
            if literal is None:
                raise ValueError("ambiguous quoted reasoning literal")
        elif re.search(r"[\s\"']", argument):
            raise ValueError("unquoted reasoning argument contains whitespace or quotes")
    return matched.group("predicate"), arguments


def require_safe_negation(
    positive_atoms: list[tuple[str, list[str]]], negative_atoms: list[tuple[str, list[str]]],
) -> None:
    if not positive_atoms or not negative_atoms:
        raise ValueError("closed-world rules require positive guards and negated atoms")
    bound = {arg for _, args in positive_atoms for arg in args if arg.startswith("?")}
    unsafe = {arg for _, args in negative_atoms for arg in args if arg.startswith("?") and arg not in bound}
    if unsafe:
        raise ValueError("unsafe closed-world variables are not positively bound: " + ", ".join(sorted(unsafe)))


def validate_rule_expression(expression: str) -> None:
    matched = re.fullmatch(r"IF\s+(.+?)\s+THEN\s+(.+)", expression, re.I)
    if matched is None:
        raise ValueError("expected IF <premises> THEN <conclusion>")
    positive = []
    negative = []
    for value in re.split(r"\s+AND\s+", matched[1], flags=re.I):
        negated = bool(re.match(r"^NOT\s+", value.strip(), re.I))
        atom = parse_atom(re.sub(r"^NOT\s+", "", value.strip(), flags=re.I))
        (negative if negated else positive).append(atom)
    parse_atom(matched[2].strip())
    if negative:
        require_safe_negation(positive, negative)
