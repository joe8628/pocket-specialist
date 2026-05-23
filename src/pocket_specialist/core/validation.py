"""Structured output validation and lightweight JSON repair."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(slots=True)
class ValidationResult(Generic[T]):
    value: T | None
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues and self.value is not None


class OutputValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        message = "; ".join(issue.message for issue in issues) or "output validation failed"
        super().__init__(message)


class OutputValidator:
    """Mandatory validation layer before model outputs enter CIF."""

    def parse_json(self, raw: str, *, repair: bool = True) -> ValidationResult[object]:
        errors: list[ValidationIssue] = []
        try:
            return ValidationResult(value=json.loads(raw))
        except json.JSONDecodeError as exc:
            errors.append(ValidationIssue(code="malformed_json", message=str(exc)))
            if not repair:
                return ValidationResult(value=None, issues=errors)

        repaired = self._repair_json(raw)
        if repaired == raw:
            return ValidationResult(value=None, issues=errors)
        try:
            return ValidationResult(value=json.loads(repaired), issues=errors)
        except json.JSONDecodeError as exc:
            errors.append(ValidationIssue(code="repair_failed", message=str(exc)))
            return ValidationResult(value=None, issues=errors)

    def validate(self, payload: object, validator: Callable[[object], T]) -> ValidationResult[T]:
        try:
            return ValidationResult(value=validator(payload))
        except OutputValidationError as exc:
            return ValidationResult(value=None, issues=exc.issues)

    def require(self, payload: object, validator: Callable[[object], T]) -> T:
        result = self.validate(payload, validator)
        if result.value is None:
            raise OutputValidationError(result.issues)
        return result.value

    @staticmethod
    def _repair_json(raw: str) -> str:
        text = raw.strip()
        if not text:
            return text
        if text.startswith("```"):
            lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
            text = "\n".join(lines).strip()
        if text.count("{") > text.count("}"):
            text = text + ("}" * (text.count("{") - text.count("}")))
        if text.count("[") > text.count("]"):
            text = text + ("]" * (text.count("[") - text.count("]")))
        return text
