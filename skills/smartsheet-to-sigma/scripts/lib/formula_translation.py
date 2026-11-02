"""Conservative Smartsheet-to-Sigma scalar formula translation."""
from __future__ import annotations

import re


FUNCTIONS = {
    "IF": "If",
    "IFERROR": "IfError",
    "AND": "And",
    "OR": "Or",
    "NOT": "Not",
    "ABS": "Abs",
    "ROUND": "Round",
    "TODAY": "Today",
}
HIERARCHY_FUNCTIONS = {"CHILDREN", "PARENT", "ANCESTORS", "DESCENDANTS"}
_FUNCTION_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]*)\s*\(")
_SAME_ROW_RE = re.compile(r"\[([^\]\r\n]+)\]@row", re.IGNORECASE)
_NUMBERED_REF_RE = re.compile(r"\[[^\]\r\n]+\]\s*\d+")
_BARE_AT_RE = re.compile(r"@(?:cell|row)\b", re.IGNORECASE)
_STRING_RE = re.compile(r'"(?:[^"]|"")*"')
_SAFE_RE = re.compile(r"^[\w\s\[\].,+\-*/%^<>=!&|()'\":@$]+$")


def normalize_formula(formula: str) -> str:
    """Normalize a formula for same-shape column comparison."""
    text = str(formula or "").strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(\[[^\]]+\])\d+", r"\1#", text)
    return text.upper()


def translate_formula(formula: str) -> dict:
    """Return translation details; unsupported formulas remain explicit."""
    source = str(formula or "").strip()
    if not source.startswith("="):
        return _unsupported(source, "FORMULA_MISSING_EQUALS")
    expression = source[1:].strip()
    if not expression:
        return _unsupported(source, "FORMULA_EMPTY")

    without_strings = _STRING_RE.sub('""', expression)
    if "{" in without_strings or "}" in without_strings:
        return _unsupported(source, "CROSS_SHEET_REFERENCE")
    if _NUMBERED_REF_RE.search(without_strings) or ":" in without_strings:
        return _unsupported(source, "ROW_POSITION_REFERENCE")
    if _BARE_AT_RE.search(without_strings) and not _SAME_ROW_RE.search(without_strings):
        return _unsupported(source, "UNQUALIFIED_CELL_REFERENCE")

    if not _SAFE_RE.fullmatch(without_strings):
        return _unsupported(source, "UNSUPPORTED_TOKEN")

    names = {name.upper() for name in _FUNCTION_RE.findall(without_strings)}
    hierarchy = sorted(names & HIERARCHY_FUNCTIONS)
    if hierarchy:
        return _unsupported(source, "HIERARCHY_FUNCTION", functions=hierarchy)
    unsupported = sorted(name for name in names if name not in FUNCTIONS)
    if unsupported:
        return _unsupported(source, "UNSUPPORTED_FUNCTION", functions=unsupported)

    translated = _rewrite_unquoted(expression)
    return {
        "source": source,
        "normalized": normalize_formula(source),
        "supported": True,
        "sigma": translated,
        "reason": None,
        "functions": sorted(names),
    }


def _rewrite_unquoted(expression: str) -> str:
    def rewrite(segment: str) -> str:
        segment = _SAME_ROW_RE.sub(lambda match: f"[{match.group(1)}]", segment)
        segment = re.sub(r"<>", "!=", segment)
        return _FUNCTION_RE.sub(
            lambda match: FUNCTIONS[match.group(1).upper()] + "(",
            segment,
        )

    pieces = []
    end = 0
    for match in _STRING_RE.finditer(expression):
        pieces.append(rewrite(expression[end:match.start()]))
        pieces.append(match.group(0))
        end = match.end()
    pieces.append(rewrite(expression[end:]))
    return "".join(pieces)


def _unsupported(source: str, reason: str, *, functions: list[str] | None = None) -> dict:
    return {
        "source": source,
        "normalized": normalize_formula(source),
        "supported": False,
        "sigma": None,
        "reason": reason,
        "functions": functions or [],
    }
