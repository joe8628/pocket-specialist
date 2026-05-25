"""Formula extraction provider contracts and UniMERNet service client."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from pocket_specialist.core.config import get_settings
from pocket_specialist.core.gpu import gpu_scheduler
from pocket_specialist.core.tasks import FormulaUnit
from pocket_specialist.core.validation import OutputValidationError, OutputValidator, ValidationIssue


@dataclass(slots=True)
class FormulaResult:
    latex: str
    mathml: str | None
    provider: str
    confidence: float | None
    is_inline: bool
    raw_response: str
    latency_ms: int


class FormulaExtractor(Protocol):
    def load(self) -> None: ...
    def extract(self, image_bytes: bytes) -> FormulaResult: ...
    def extract_batch(self, crops: list[bytes]) -> list[FormulaResult]: ...
    def offload(self) -> None: ...


class FormulaExtractionError(RuntimeError):
    """Raised when a formula provider cannot return a valid formula result."""


def _validate_formula_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise OutputValidationError([ValidationIssue(code="type_error", message="Formula payload must be a dict")])

    latex = payload.get("latex")
    if not isinstance(latex, str) or not latex.strip():
        raise OutputValidationError([ValidationIssue(code="missing_latex", message="Formula payload requires non-empty latex")])

    mathml = payload.get("mathml")
    if mathml is not None and not isinstance(mathml, str):
        raise OutputValidationError([ValidationIssue(code="mathml_type_error", message="mathml must be a string or null")])

    confidence = payload.get("confidence")
    if confidence is not None and not isinstance(confidence, int | float):
        raise OutputValidationError([ValidationIssue(code="confidence_type_error", message="confidence must be numeric or null")])

    is_inline = payload.get("is_inline", payload.get("inline", False))
    if not isinstance(is_inline, bool):
        raise OutputValidationError([ValidationIssue(code="inline_type_error", message="is_inline must be boolean")])

    return {
        "latex": latex.strip(),
        "mathml": mathml,
        "provider": str(payload.get("provider") or "unimernet"),
        "confidence": float(confidence) if confidence is not None else None,
        "is_inline": is_inline,
        "raw_response": payload.get("raw_response"),
        "latency_ms": payload.get("latency_ms"),
    }


class UniMERNetFormulaExtractor:
    """Formula extractor backed by the isolated UniMERNet HTTP microservice."""

    name = "unimernet"

    def __init__(self, base_url: str | None = None, model_size: str | None = None, validator: OutputValidator | None = None) -> None:
        settings = get_settings().formula
        self._base_url = (base_url or settings.base_url).rstrip("/")
        self._model_size = model_size or settings.model_size
        self._timeout = settings.timeout_seconds
        self._validator = validator or OutputValidator()
        self._session: requests.Session | None = None

    def load(self) -> None:
        self._session = requests.Session()
        try:
            with gpu_scheduler.claim("formula"):
                response = self._session.post(f"{self._base_url}/load", json={"model_size": self._model_size}, timeout=self._timeout)
            if response.status_code == 404:
                return
            response.raise_for_status()
        except requests.RequestException as exc:
            self.offload()
            raise FormulaExtractionError(f"UniMERNet service load failed: {exc}") from exc

    def extract(self, image_bytes: bytes) -> FormulaResult:
        if self._session is None:
            raise RuntimeError("UniMERNetFormulaExtractor.load() must be called before extract()")

        started = time.monotonic()
        payload = {
            "image": base64.b64encode(image_bytes).decode("ascii"),
            "model_size": self._model_size,
        }
        try:
            with gpu_scheduler.claim("formula"):
                response = self._session.post(f"{self._base_url}/extract", json=payload, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise FormulaExtractionError(f"UniMERNet service extract failed: {exc}") from exc

        normalized = self._validator.require(body, _validate_formula_payload)
        raw_response = normalized["raw_response"]
        if raw_response is None:
            raw_response = json.dumps(body, ensure_ascii=False)
        latency_ms = normalized["latency_ms"]
        if not isinstance(latency_ms, int):
            latency_ms = int((time.monotonic() - started) * 1000)
        return FormulaResult(
            latex=str(normalized["latex"]),
            mathml=normalized["mathml"] if isinstance(normalized["mathml"], str) else None,
            provider=str(normalized["provider"]),
            confidence=normalized["confidence"] if isinstance(normalized["confidence"], float) else None,
            is_inline=bool(normalized["is_inline"]),
            raw_response=str(raw_response),
            latency_ms=latency_ms,
        )

    def extract_batch(self, crops: list[bytes]) -> list[FormulaResult]:
        if self._session is None:
            raise RuntimeError("UniMERNetFormulaExtractor.load() must be called before extract_batch()")
        if not crops:
            return []

        payload = {
            "images": [base64.b64encode(crop).decode("ascii") for crop in crops],
            "model_size": self._model_size,
        }
        try:
            with gpu_scheduler.claim("formula"):
                response = self._session.post(f"{self._base_url}/extract_batch", json=payload, timeout=self._timeout)
            if response.status_code == 404:
                return [self.extract(crop) for crop in crops]
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise FormulaExtractionError(f"UniMERNet service batch extract failed: {exc}") from exc

        raw_results = body.get("results") if isinstance(body, dict) else body
        if not isinstance(raw_results, list):
            raise FormulaExtractionError("UniMERNet batch response must be a list or contain a results list")
        return [self._result_from_payload(item) for item in raw_results]

    def _result_from_payload(self, payload: object) -> FormulaResult:
        normalized = self._validator.require(payload, _validate_formula_payload)
        raw_response = normalized["raw_response"]
        if raw_response is None:
            raw_response = json.dumps(payload, ensure_ascii=False)
        latency_ms = normalized["latency_ms"]
        return FormulaResult(
            latex=str(normalized["latex"]),
            mathml=normalized["mathml"] if isinstance(normalized["mathml"], str) else None,
            provider=str(normalized["provider"]),
            confidence=normalized["confidence"] if isinstance(normalized["confidence"], float) else None,
            is_inline=bool(normalized["is_inline"]),
            raw_response=str(raw_response),
            latency_ms=latency_ms if isinstance(latency_ms, int) else 0,
        )

    def extract_units(self, units: list[FormulaUnit]) -> list[FormulaResult]:
        return self.extract_batch([unit.image_bytes for unit in units])

    def offload(self) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            with gpu_scheduler.claim("formula"):
                session.post(f"{self._base_url}/offload", timeout=min(self._timeout, 5))
        except requests.RequestException:
            pass
        session.close()

    def health_check(self) -> bool:
        try:
            response = requests.get(f"{self._base_url}/health", timeout=min(self._timeout, 5))
            return response.ok
        except requests.RequestException:
            return False


def build_formula_extractor(provider_name: str | None = None) -> FormulaExtractor:
    configured = (provider_name or "unimernet").lower()
    if configured not in {"unimernet", "unimernet-service", "unimernet-http"}:
        raise ValueError(f"Unsupported formula provider: {provider_name}")
    return UniMERNetFormulaExtractor()


__all__ = [
    "FormulaExtractionError",
    "FormulaExtractor",
    "FormulaResult",
    "UniMERNetFormulaExtractor",
    "build_formula_extractor",
]
