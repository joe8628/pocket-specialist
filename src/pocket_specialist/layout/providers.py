"""First-class layout subsystem contracts and implementation adapters."""

from __future__ import annotations

import gc
import io
import sys
from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image
import torch

from pocket_specialist.core.config import get_settings
from pocket_specialist.core.gpu import gpu_scheduler


@dataclass(slots=True)
class LayoutRegion:
    region_id: str
    region_type: str
    bbox: tuple[int, int, int, int]
    confidence: float
    reading_order: int
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class LayoutResult:
    page_id: str
    regions: list[LayoutRegion]
    layout_confidence: float | None


class LayoutProvider(Protocol):
    def load(self) -> None: ...
    def detect(self, image_bytes: bytes) -> LayoutResult: ...
    def offload(self) -> None: ...


_LAYOUT_LABEL_MAP = {
    "Text": "text",
    "SectionHeader": "heading",
    "Table": "table",
    "Equation": "formula",
    "Code": "code",
    "Figure": "figure",
    "Caption": "text",
    "ListItem": "list",
    "Footnote": "footer",
    "PageHeader": "header",
    "PageFooter": "footer",
    "Form": "key_value",
}


class SuryaLayoutProvider:
    """Compatibility layout adapter used behind configurable layout provider names."""

    def __init__(self, provider_name: str | None = None) -> None:
        self.name = provider_name or get_settings().layout.provider
        self._predictor = None
        self._foundation = None

    def load(self) -> None:
        if self._predictor is not None:
            return
        try:
            from surya.foundation import FoundationPredictor
            from surya.layout import LayoutPredictor
            from surya.settings import settings
        except ImportError:
            print("Error: surya-ocr is not installed. Run: pip install surya-ocr", file=sys.stderr)
            sys.exit(1)

        self._foundation = FoundationPredictor(checkpoint=settings.LAYOUT_MODEL_CHECKPOINT)
        self._predictor = LayoutPredictor(self._foundation)

    def detect(self, image_bytes: bytes) -> LayoutResult:
        if self._predictor is None:
            raise RuntimeError("SuryaLayoutProvider.load() must be called before detect()")

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        result = self._predictor([image])[0]
        raw_regions = getattr(result, "bboxes", [])
        ordered = sorted(raw_regions, key=lambda item: getattr(item, "position", 0))
        regions: list[LayoutRegion] = []
        confidences: list[float] = []
        for idx, raw_region in enumerate(ordered, 1):
            x0, y0, x1, y1 = raw_region.bbox
            confidence = float(getattr(raw_region, "confidence", 1.0) or 1.0)
            confidences.append(confidence)
            provider_label = getattr(raw_region, "label", "Text")
            regions.append(
                LayoutRegion(
                    region_id=f"region-{idx:04d}",
                    region_type=_LAYOUT_LABEL_MAP.get(provider_label, provider_label.lower()),
                    bbox=(int(x0), int(y0), int(x1), int(y1)),
                    confidence=confidence,
                    reading_order=int(getattr(raw_region, "position", idx - 1)),
                    metadata={"provider": self.name, "provider_label": provider_label},
                )
            )

        return LayoutResult(
            page_id="page",
            regions=regions,
            layout_confidence=(sum(confidences) / len(confidences)) if confidences else None,
        )

    def offload(self) -> None:
        self._predictor = None
        self._foundation = None
        gc.collect()
        gpu_scheduler.release_memory()


def build_layout_provider(provider_name: str | None = None) -> LayoutProvider:
    configured = (provider_name or get_settings().layout.provider).lower()
    if configured not in {"pp-doclayout-v3", "surya-layout", "surya"}:
        raise ValueError(f"Unsupported layout provider: {provider_name or get_settings().layout.provider}")
    return SuryaLayoutProvider(provider_name=configured)


def crop_region_image(image_bytes: bytes, bbox: tuple[int, int, int, int]) -> bytes:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    left, top, right, bottom = bbox
    cropped = image.crop((left, top, right, bottom))
    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG")
    return buffer.getvalue()


__all__ = [
    "LayoutProvider",
    "LayoutRegion",
    "LayoutResult",
    "SuryaLayoutProvider",
    "build_layout_provider",
    "crop_region_image",
]
