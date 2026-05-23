"""OCR provider abstraction and Surya implementation."""

from __future__ import annotations

import gc
import io
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from PIL import Image
import torch

from pipeline.models import BlockType, BoundingBox, TextBlock

from .config import get_settings
from .gpu import gpu_scheduler
from .tasks import RegionUnit
from .validation import OutputValidationError, OutputValidator, ValidationIssue


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


class SuryaOCRProvider:
    """Phase A OCR provider adapter backed by Surya."""

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
                "mode": ExtractionMode.PAGE_STRUCTURED.value,
                "language": get_settings().ocr.language,
                "block_count": len(typed_content["blocks"]),
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
