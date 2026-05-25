"""OCR provider abstraction and provider routing implementations."""

from __future__ import annotations

import base64
import gc
import io
import json
import sys
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

    return {"blocks": normalized}


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

    def extract(self, image_bytes: bytes, region_type: str) -> OCRResult:
        if self._session is None:
            raise RuntimeError(f"{self.name}.load() must be called before extract()")

        started = time.monotonic()
        prompt = (
            "Extract the content from the attached image and return only strict JSON. "
            f"Use the region_type '{region_type}'. "
            "Every response must include a top-level 'blocks' list with OCR spans and their bounding boxes. "
            "Follow this shape exactly: "
            f"{_contract_hint(region_type)}"
        )
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
        parse_result = self._validator.parse_json(raw_response)
        if parse_result.value is None:
            raise OutputValidationError(parse_result.issues)
        typed_content = self._validator.require(parse_result.value, _validate_ocr_payload)
        latency_ms = int((time.monotonic() - started) * 1000)
        return OCRResult(
            region_type=region_type,
            typed_content=typed_content,
            provider=self.name,
            confidence=None,
            structure_confidence=None,
            bounding_boxes=[block["bbox"] for block in typed_content["blocks"]],
            raw_response=raw_response,
            latency_ms=latency_ms,
            extraction_metadata={
                "mode": (ExtractionMode.PAGE_STRUCTURED.value if region_type == "page" else ExtractionMode.REGION_GUIDED.value),
                "runtime": "ollama",
                "block_count": len(typed_content["blocks"]),
            },
        )

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
        except ImportError:
            print("Error: surya-ocr is not installed. Run: pip install surya-ocr", file=sys.stderr)
            sys.exit(1)

        self._det_predictor = DetectionPredictor()
        self._foundation = FoundationPredictor(checkpoint=settings.RECOGNITION_MODEL_CHECKPOINT)
        self._rec_predictor = RecognitionPredictor(self._foundation)

    def extract(self, image_bytes: bytes, region_type: str) -> OCRResult:
        if self._det_predictor is None or self._rec_predictor is None:
            raise RuntimeError("SuryaOCRProvider.load() must be called before extract()")

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        started = time.monotonic()
        with gpu_scheduler.claim("ocr"):
            results = self._rec_predictor([image], det_predictor=self._det_predictor)
        blocks: list[TextBlock] = []
        confidences: list[float] = []
        for line in results[0].text_lines:
            text = line.text.strip()
            if not text:
                continue
            x0, y0, x1, y1 = line.bbox
            confidence = float(line.confidence or 0.0)
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
        latency_ms = int((time.monotonic() - started) * 1000)
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
            },
        )

    def extract_batch(self, units: list[RegionUnit]) -> list[OCRResult]:
        return [self.extract(unit.image_bytes, unit.region_type) for unit in units]

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
