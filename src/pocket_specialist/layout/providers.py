"""First-class layout subsystem contracts and implementation adapters."""

from __future__ import annotations

import base64
import gc
import io
from dataclasses import dataclass, field
from types import ModuleType
from typing import Protocol

from PIL import Image
import requests
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
    "text": "text",
    "sectionheader": "heading",
    "section_header": "heading",
    "title": "heading",
    "table": "table",
    "equation": "formula",
    "formula": "formula",
    "code": "code",
    "figure": "figure",
    "caption": "text",
    "listitem": "list",
    "list_item": "list",
    "footnote": "footer",
    "pageheader": "header",
    "header": "header",
    "pagefooter": "footer",
    "footer": "footer",
    "form": "key_value",
    "keyvalue": "key_value",
    "key_value": "key_value",
}

_PP_DOCLAYOUT_V3_MODEL_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
_PP_DOCLAYOUT_V3_MIN_TRANSFORMERS = "5.5.4"
_LAYOUT_SERVICE_TIMEOUT_SECONDS = 60


def _parse_version(version_text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version_text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _load_transformers_module() -> ModuleType:
    try:
        import transformers
    except ImportError as exc:
        raise RuntimeError("transformers is not installed. Run: pip install transformers") from exc
    return transformers


def _build_pp_doclayout_v3_pipeline(transformers_module: ModuleType, model_id: str, device: int):
    return transformers_module.pipeline("object-detection", model=model_id, device=device)


def normalize_layout_label(provider_label: str) -> str:
    normalized = provider_label.strip().lower().replace("-", "_").replace(" ", "_")
    compact = normalized.replace("_", "")
    return _LAYOUT_LABEL_MAP.get(normalized, _LAYOUT_LABEL_MAP.get(compact, normalized))


def _layout_box(raw_region: object) -> tuple[int, int, int, int]:
    box = raw_region.get("box", {}) if isinstance(raw_region, dict) else {}
    return (
        int(float(box.get("xmin", 0))),
        int(float(box.get("ymin", 0))),
        int(float(box.get("xmax", 0))),
        int(float(box.get("ymax", 0))),
    )


def _reading_order_key(raw_region: object) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = _layout_box(raw_region)
    return (y0, x0, y1, x1)


class PPDocLayoutV3LayoutProvider:
    """Transformers-backed PP-DocLayoutV3 adapter."""

    def __init__(self, provider_name: str | None = None, model_id: str = _PP_DOCLAYOUT_V3_MODEL_ID) -> None:
        self.name = provider_name or "pp-doclayout-v3"
        self.model_id = model_id
        self._pipeline = None

    def load(self) -> None:
        if self._pipeline is not None:
            return

        transformers_module = _load_transformers_module()
        installed_version = getattr(transformers_module, "__version__", "0")
        if _parse_version(installed_version) < _parse_version(_PP_DOCLAYOUT_V3_MIN_TRANSFORMERS):
            raise RuntimeError(
                "pp-doclayout-v3 requires transformers >= "
                f"{_PP_DOCLAYOUT_V3_MIN_TRANSFORMERS}; found {installed_version}"
            )

        device = 0 if torch.cuda.is_available() else -1
        try:
            self._pipeline = _build_pp_doclayout_v3_pipeline(transformers_module, self.model_id, device)
        except ValueError as exc:
            raise RuntimeError(
                "Failed to initialize pp-doclayout-v3 through Transformers. "
                f"Installed transformers version: {installed_version}."
            ) from exc

    def detect(self, image_bytes: bytes) -> LayoutResult:
        if self._pipeline is None:
            raise RuntimeError("PPDocLayoutV3LayoutProvider.load() must be called before detect()")

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        with gpu_scheduler.claim("layout"):
            raw_regions = self._pipeline(image)

        regions: list[LayoutRegion] = []
        confidences: list[float] = []
        for idx, raw_region in enumerate(sorted(raw_regions, key=_reading_order_key), 1):
            x0, y0, x1, y1 = _layout_box(raw_region)
            provider_label = str(raw_region.get("label", "text")) if isinstance(raw_region, dict) else "text"
            confidence = float(raw_region.get("score", 0.0)) if isinstance(raw_region, dict) else 0.0
            confidences.append(confidence)
            regions.append(
                LayoutRegion(
                    region_id=f"region-{idx:04d}",
                    region_type=normalize_layout_label(provider_label),
                    bbox=(x0, y0, x1, y1),
                    confidence=confidence,
                    reading_order=idx - 1,
                    metadata={"provider": self.name, "provider_label": provider_label, "model_id": self.model_id},
                )
            )

        return LayoutResult(
            page_id="page",
            regions=regions,
            layout_confidence=(sum(confidences) / len(confidences)) if confidences else None,
        )

    def offload(self) -> None:
        self._pipeline = None
        gc.collect()
        gpu_scheduler.release_memory()


class SuryaLayoutServiceProvider:
    """HTTP-backed Surya layout adapter isolated from the main runtime dependency stack."""

    def __init__(self, provider_name: str | None = None, base_url: str | None = None) -> None:
        settings = get_settings().layout
        self.name = provider_name or "surya-layout-service"
        self._base_url = (base_url or settings.base_url).rstrip("/")
        self._session: requests.Session | None = None

    def load(self) -> None:
        if self._session is not None:
            return
        self._session = requests.Session()
        try:
            response = self._session.post(
                f"{self._base_url}/load",
                json={"provider": self.name},
                timeout=_LAYOUT_SERVICE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self.offload()
            raise RuntimeError(f"Surya layout service load failed: {exc}") from exc

    def detect(self, image_bytes: bytes) -> LayoutResult:
        if self._session is None:
            raise RuntimeError("SuryaLayoutServiceProvider.load() must be called before detect()")

        payload = {"image": base64.b64encode(image_bytes).decode("ascii")}
        try:
            response = self._session.post(
                f"{self._base_url}/detect",
                json=payload,
                timeout=_LAYOUT_SERVICE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"Surya layout service detect failed: {exc}") from exc
        return _layout_result_from_service_payload(body, provider_name=self.name)

    def offload(self) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            session.post(f"{self._base_url}/offload", timeout=min(_LAYOUT_SERVICE_TIMEOUT_SECONDS, 5))
        except requests.RequestException:
            pass
        session.close()
        gpu_scheduler.release_memory()

    def health_check(self) -> bool:
        try:
            response = requests.get(f"{self._base_url}/health", timeout=min(_LAYOUT_SERVICE_TIMEOUT_SECONDS, 5))
            return response.ok
        except requests.RequestException:
            return False


def _layout_result_from_service_payload(payload: object, *, provider_name: str) -> LayoutResult:
    if not isinstance(payload, dict):
        raise RuntimeError("Surya layout service returned a non-object response")
    raw_regions = payload.get("regions")
    if not isinstance(raw_regions, list):
        raise RuntimeError("Surya layout service response must include a regions list")
    page_id = str(payload.get("page_id") or "page")
    layout_confidence = payload.get("layout_confidence")
    regions: list[LayoutRegion] = []
    for idx, raw_region in enumerate(raw_regions, 1):
        if not isinstance(raw_region, dict):
            raise RuntimeError(f"Surya layout service region {idx} is not an object")
        bbox = raw_region.get("bbox")
        if not isinstance(bbox, list | tuple) or len(bbox) != 4:
            raise RuntimeError(f"Surya layout service region {idx} has invalid bbox")
        provider_label = str(raw_region.get("provider_label") or raw_region.get("region_type") or "text")
        region_type = str(raw_region.get("region_type") or normalize_layout_label(provider_label))
        confidence = float(raw_region.get("confidence") or 0.0)
        reading_order = int(raw_region.get("reading_order") if raw_region.get("reading_order") is not None else idx - 1)
        regions.append(
            LayoutRegion(
                region_id=f"region-{idx:04d}",
                region_type=region_type,
                bbox=tuple(int(float(value)) for value in bbox),
                confidence=confidence,
                reading_order=reading_order,
                metadata={
                    "provider": provider_name,
                    "provider_label": provider_label,
                },
            )
        )
    return LayoutResult(
        page_id=page_id,
        regions=regions,
        layout_confidence=float(layout_confidence) if layout_confidence is not None else None,
    )


def build_layout_provider(provider_name: str | None = None) -> LayoutProvider:
    configured = (provider_name or get_settings().layout.provider).lower()
    if configured in {"pp-doclayout-v3", "ppdoclayoutv3"}:
        return PPDocLayoutV3LayoutProvider(provider_name="pp-doclayout-v3")
    if configured in {"surya-layout-service", "surya-layout", "surya"}:
        return SuryaLayoutServiceProvider(provider_name="surya-layout-service")
    raise ValueError(f"Unsupported layout provider: {provider_name or get_settings().layout.provider}")


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
    "PPDocLayoutV3LayoutProvider",
    "SuryaLayoutServiceProvider",
    "build_layout_provider",
    "crop_region_image",
    "normalize_layout_label",
]
