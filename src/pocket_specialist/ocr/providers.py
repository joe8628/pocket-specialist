"""OCR provider abstraction and provider routing implementations."""

from __future__ import annotations

import base64
import gc
import io
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from PIL import Image
import requests
import torch

from pocket_specialist.core.models import BlockType, BoundingBox, TextBlock

from pocket_specialist.core.config import get_settings
from pocket_specialist.core.gpu import gpu_scheduler
from pocket_specialist.core.tasks import RegionUnit
from pocket_specialist.core.validation import OutputValidationError, OutputValidator, ValidationIssue


class ExtractionMode(str, Enum):
    REGION_GUIDED = "REGION_GUIDED"
    PAGE_STRUCTURED = "PAGE_STRUCTURED"


@dataclass(slots=True)
class OCRResult:
    region_type: str
    typed_content: dict[str, object]
    provider: str
    confidence: float | None
    structure_confidence: float | None
    bounding_boxes: list[dict[str, float]] | None
    raw_response: str
    latency_ms: int
    extraction_metadata: dict[str, object] = field(default_factory=dict)


class OCRProvider(Protocol):
    def load(self) -> None: ...
    def extract(self, image_bytes: bytes, region_type: str) -> OCRResult: ...
    def extract_batch(self, units: list[RegionUnit]) -> list[OCRResult]: ...
    def offload(self) -> None: ...
    def health_check(self) -> bool: ...


def _validate_ocr_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise OutputValidationError([ValidationIssue(code="type_error", message="OCR payload must be a dict")])

    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        raise OutputValidationError([ValidationIssue(code="missing_blocks", message="OCR payload requires a blocks list")])

    normalized: list[dict[str, object]] = []
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise OutputValidationError(
                [ValidationIssue(code="block_type_error", message="OCR block must be a dict", path=f"blocks[{idx}]")]
            )
        required = {"bbox", "raw_text", "confidence", "block_type"}
        missing = sorted(required - set(block))
        if missing:
            raise OutputValidationError(
                [
                    ValidationIssue(
                        code="missing_keys",
                        message=f"OCR block missing required keys: {', '.join(missing)}",
                        path=f"blocks[{idx}]",
                    )
                ]
            )
        normalized.append(block)

    validated: dict[str, object] = {"blocks": normalized}
    for optional_key in ("type", "headers", "rows", "caption"):
        if optional_key in payload:
            validated[optional_key] = payload[optional_key]
    return validated


def _contract_hint(region_type: str) -> str:
    if region_type == "table":
        return '{"type":"TableBlock","headers":[...],"rows":[...],"caption":null,"blocks":[{"bbox":{"x0":0,"y0":0,"x1":0,"y1":0},"raw_text":"...","confidence":1.0,"block_type":"table"}]}'
    if region_type == "code":
        return '{"type":"CodeBlock","language":null,"code":"...","blocks":[{"bbox":{"x0":0,"y0":0,"x1":0,"y1":0},"raw_text":"...","confidence":1.0,"block_type":"code"}]}'
    if region_type == "key_value":
        return '{"type":"KeyValueBlock","pairs":{"key":"value"},"blocks":[{"bbox":{"x0":0,"y0":0,"x1":0,"y1":0},"raw_text":"key: value","confidence":1.0,"block_type":"key_value"}]}'
    if region_type in {"text", "heading", "list", "footer", "header"}:
        return '{"type":"TextBlock","text":"...","heading_level":null,"language":null,"blocks":[{"bbox":{"x0":0,"y0":0,"x1":0,"y1":0},"raw_text":"...","confidence":1.0,"block_type":"text"}]}'
    if region_type == "figure":
        return '{"type":"FigureBlock","caption":null,"alt_text":"...","embedded_text":null,"blocks":[]}'
    return '{"type":"TextBlock","text":"...","blocks":[{"bbox":{"x0":0,"y0":0,"x1":0,"y1":0},"raw_text":"...","confidence":1.0,"block_type":"text"}]}'


def _constrained_prompt_suffix() -> str:
    return (
        " Return only compact valid JSON with double-quoted keys and string values where required. "
        "Do not emit markdown fences, commentary, or trailing text. "
        "If uncertain, return an empty 'blocks' list that still matches the schema exactly."
    )


def _is_timeout_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, requests.Timeout)):
        return True
    return "timeout" in str(exc).lower() or "timed out" in str(exc).lower()


def _is_oom_error(exc: Exception) -> bool:
    if isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    message = str(exc).lower()
    return "out of memory" in message or "cuda oom" in message or message.strip() == "oom"


class OllamaOCRProvider:
    """Structured OCR provider backed by a local Ollama model."""

    def __init__(self, model_name: str, validator: OutputValidator | None = None) -> None:
        self.model_name = model_name
        self._validator = validator or OutputValidator()
        self._base_url = get_settings().ocr.ollama_base_url.rstrip("/")
        self._timeout = get_settings().ocr.timeout_seconds
        self._session: requests.Session | None = None

    @property
    def name(self) -> str:
        return self.model_name

    def load(self) -> None:
        self._session = requests.Session()

    def _build_prompt(self, region_type: str, *, constrained: bool = False) -> str:
        prompt = (
            "Extract the content from the attached image and return only strict JSON. "
            f"Use the region_type '{region_type}'. "
            "Every response must include a top-level 'blocks' list with OCR spans and their bounding boxes. "
            "Follow this shape exactly: "
            f"{_contract_hint(region_type)}"
        )
        if constrained:
            prompt += _constrained_prompt_suffix()
        return prompt

    def _extract_once(self, image_bytes: bytes, region_type: str, *, constrained: bool = False) -> OCRResult:
        if self._session is None:
            raise RuntimeError(f"{self.name}.load() must be called before extract()")

        prompt = self._build_prompt(region_type, constrained=constrained)
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "images": [base64.b64encode(image_bytes).decode("ascii")],
            "stream": False,
            "format": "json",
        }
        with gpu_scheduler.claim("ocr"):
            response = self._session.post(f"{self._base_url}/api/generate", json=payload, timeout=self._timeout)
        response.raise_for_status()
        body = response.json()
        raw_response = str(body.get("response", "")).strip()
        parse_result = self._validator.parse_json(raw_response, repair=constrained)
        if parse_result.value is None:
            raise OutputValidationError(parse_result.issues)
        typed_content = self._validator.require(parse_result.value, _validate_ocr_payload)
        return OCRResult(
            region_type=region_type,
            typed_content=typed_content,
            provider=self.name,
            confidence=None,
            structure_confidence=None,
            bounding_boxes=[block["bbox"] for block in typed_content["blocks"]],
            raw_response=raw_response,
            latency_ms=0,
            extraction_metadata={
                "mode": (ExtractionMode.PAGE_STRUCTURED.value if region_type == "page" else ExtractionMode.REGION_GUIDED.value),
                "runtime": "ollama",
                "block_count": len(typed_content["blocks"]),
                "constrained_prompt": constrained,
            },
        )

    def extract(self, image_bytes: bytes, region_type: str) -> OCRResult:
        started = time.monotonic()
        attempt_count = 1
        try:
            result = self._extract_once(image_bytes, region_type, constrained=False)
        except OutputValidationError as exc:
            attempt_count = 2
            result = self._extract_once(image_bytes, region_type, constrained=True)
            result.extraction_metadata["retry_strategy"] = "constrained_prompt"
            result.extraction_metadata["initial_validation_issue_codes"] = [issue.code for issue in exc.issues]
        result.latency_ms = int((time.monotonic() - started) * 1000)
        result.extraction_metadata["attempt_count"] = attempt_count
        return result

    def extract_batch(self, units: list[RegionUnit]) -> list[OCRResult]:
        return [self.extract(unit.image_bytes, unit.region_type) for unit in units]

    def offload(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        gpu_scheduler.release_memory()

    def health_check(self) -> bool:
        try:
            response = requests.get(f"{self._base_url}/api/tags", timeout=min(self._timeout, 5))
            return response.ok
        except requests.RequestException:
            return False


class GLMOCRProvider(OllamaOCRProvider):
    def __init__(self, validator: OutputValidator | None = None) -> None:
        super().__init__("glm-ocr", validator=validator)


class DeepSeekOCRProvider(OllamaOCRProvider):
    def __init__(self, validator: OutputValidator | None = None) -> None:
        super().__init__("deepseek-ocr", validator=validator)


class SuryaOCRProvider:
    """Compatibility OCR adapter backed by Surya."""

    name = "surya"

    def __init__(self, validator: OutputValidator | None = None) -> None:
        self._validator = validator or OutputValidator()
        self._det_predictor = None
        self._rec_predictor = None
        self._foundation = None

    def load(self) -> None:
        if self._det_predictor is not None and self._rec_predictor is not None:
            return

        try:
            from surya.detection import DetectionPredictor
            from surya.foundation import FoundationPredictor
            from surya.recognition import RecognitionPredictor
            from surya.settings import settings
        except ImportError as exc:
            raise RuntimeError("surya-ocr is not installed. Run: pip install surya-ocr") from exc

        self._det_predictor = DetectionPredictor()
        self._foundation = FoundationPredictor(checkpoint=settings.RECOGNITION_MODEL_CHECKPOINT)
        self._rec_predictor = RecognitionPredictor(self._foundation)

    def _result_from_prediction(self, prediction: object, region_type: str, latency_ms: int, *, batch_size: int) -> OCRResult:
        blocks: list[TextBlock] = []
        confidences: list[float] = []
        for line in getattr(prediction, "text_lines", []):
            text = str(getattr(line, "text", "")).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line.bbox
            confidence = float(getattr(line, "confidence", 0.0) or 0.0)
            confidences.append(confidence)
            blocks.append(
                TextBlock(
                    bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
                    raw_text=text,
                    confidence=confidence,
                    block_type=BlockType.UNKNOWN,
                )
            )

        typed_content = {"blocks": [block.to_dict() for block in blocks]}
        typed_content = self._validator.require(typed_content, _validate_ocr_payload)
        return OCRResult(
            region_type=region_type,
            typed_content=typed_content,
            provider=self.name,
            confidence=(sum(confidences) / len(confidences)) if confidences else None,
            structure_confidence=None,
            bounding_boxes=[block["bbox"] for block in typed_content["blocks"]],
            raw_response="surya::text_lines",
            latency_ms=latency_ms,
            extraction_metadata={
                "mode": (ExtractionMode.PAGE_STRUCTURED.value if region_type == "page" else ExtractionMode.REGION_GUIDED.value),
                "language": get_settings().ocr.language,
                "block_count": len(typed_content["blocks"]),
                "runtime": "surya",
                "batch_size": batch_size,
            },
        )

    def _recognize_batch(self, images: list[Image.Image]) -> list[object]:
        if self._det_predictor is None or self._rec_predictor is None:
            raise RuntimeError("SuryaOCRProvider.load() must be called before extract()")
        with gpu_scheduler.claim("ocr"):
            return self._rec_predictor(images, det_predictor=self._det_predictor)

    def _extract_batch_once(self, units: list[RegionUnit]) -> list[OCRResult]:
        images = [Image.open(io.BytesIO(unit.image_bytes)).convert("RGB") for unit in units]
        started = time.monotonic()
        predictions = self._recognize_batch(images)
        latency_ms = int((time.monotonic() - started) * 1000)
        return [
            self._result_from_prediction(prediction, unit.region_type, latency_ms, batch_size=len(units))
            for unit, prediction in zip(units, predictions, strict=False)
        ]

    def _extract_batch_with_recovery(self, units: list[RegionUnit]) -> list[OCRResult]:
        try:
            return self._extract_batch_once(units)
        except Exception as exc:
            if len(units) > 1 and (_is_timeout_error(exc) or _is_oom_error(exc)):
                midpoint = max(1, len(units) // 2)
                return self._extract_batch_with_recovery(units[:midpoint]) + self._extract_batch_with_recovery(units[midpoint:])
            raise

    def extract(self, image_bytes: bytes, region_type: str) -> OCRResult:
        unit = RegionUnit(unit_id="single", doc_id="single", region_type=region_type, image_bytes=image_bytes)
        return self._extract_batch_once([unit])[0]

    def extract_batch(self, units: list[RegionUnit]) -> list[OCRResult]:
        if not units:
            return []
        batch_size = max(1, get_settings().ocr.batch_size)
        results: list[OCRResult] = []
        for start in range(0, len(units), batch_size):
            results.extend(self._extract_batch_with_recovery(units[start : start + batch_size]))
        return results

    def offload(self) -> None:
        self._rec_predictor = None
        self._det_predictor = None
        self._foundation = None
        gc.collect()
        gpu_scheduler.release_memory()

    def health_check(self) -> bool:
        return True


_PROVIDER_FACTORIES = {
    "glm-ocr": GLMOCRProvider,
    "deepseek-ocr": DeepSeekOCRProvider,
    "surya": SuryaOCRProvider,
}


def build_ocr_provider(provider_name: str | None = None) -> OCRProvider:
    target = (provider_name or get_settings().ocr.provider).lower()
    factory = _PROVIDER_FACTORIES.get(target)
    if factory is None:
        raise ValueError(f"Unsupported OCR provider: {provider_name or get_settings().ocr.provider}")
    return factory()


def build_primary_ocr_provider() -> OCRProvider:
    return build_ocr_provider(get_settings().ocr.provider)


def build_fallback_ocr_provider() -> OCRProvider:
    return build_ocr_provider(get_settings().ocr.fallback_provider)


__all__ = [
    "DeepSeekOCRProvider",
    "ExtractionMode",
    "GLMOCRProvider",
    "OCRProvider",
    "OCRResult",
    "OllamaOCRProvider",
    "SuryaOCRProvider",
    "build_fallback_ocr_provider",
    "build_ocr_provider",
    "build_primary_ocr_provider",
]
