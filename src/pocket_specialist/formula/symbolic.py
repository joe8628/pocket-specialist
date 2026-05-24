"""Deterministic symbolic formula heuristics used as fallback routing."""

from __future__ import annotations

import re
from dataclasses import dataclass

_INLINE_FORMULA_RE = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)|\\\((.+?)\\\)|\\\[(.+?)\\\]", re.DOTALL)
_LATEX_COMMAND_RE = re.compile(r"\\[a-zA-Z]+")
_SYMBOL_RE = re.compile(r"[=^_{}\[\]()+\-*/∑∫√≤≥≠≈∞α-ωΑ-Ω]")
_VARIABLE_RE = re.compile(r"\b[a-zA-Z]\b")


@dataclass(frozen=True, slots=True)
class InlineFormulaCandidate:
    raw_text: str
    latex: str
    start: int
    end: int


def strip_formula_delimiters(text: str) -> str:
    value = text.strip()
    pairs = (("$$", "$$"), ("$", "$"), ("\\(", "\\)"), ("\\[", "\\]"))
    for left, right in pairs:
        if value.startswith(left) and value.endswith(right) and len(value) >= len(left) + len(right):
            return value[len(left) : len(value) - len(right)].strip()
    return value


def inline_formula_candidates(text: str) -> list[InlineFormulaCandidate]:
    candidates: list[InlineFormulaCandidate] = []
    for match in _INLINE_FORMULA_RE.finditer(text):
        latex = next(group for group in match.groups() if group is not None).strip()
        if latex:
            candidates.append(InlineFormulaCandidate(raw_text=match.group(0), latex=latex, start=match.start(), end=match.end()))
    return candidates


def looks_symbolic(text: str) -> bool:
    value = strip_formula_delimiters(text)
    if not value:
        return False
    if _LATEX_COMMAND_RE.search(value):
        return True
    symbols = _SYMBOL_RE.findall(value)
    variables = _VARIABLE_RE.findall(value)
    if "=" in value and (symbols or len(variables) >= 2):
        return True
    return len(symbols) >= 2 and bool(variables)


def symbolic_latex(text: str) -> str:
    return strip_formula_delimiters(text)


__all__ = ["InlineFormulaCandidate", "inline_formula_candidates", "looks_symbolic", "strip_formula_delimiters", "symbolic_latex"]
