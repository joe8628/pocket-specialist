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
    polygon: list[tuple[int, int]] | None = None
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
    "abstract": "text",
    "algorithm": "code",
    "aside_text": "text",
    "caption": "text",
    "chart": "figure",
    "code": "code",
    "content": "text",
    "doc_title": "heading",
    "equation": "formula",
    "figure": "figure",
    "figure_title": "text",
    "footnote": "footer",
    "footer": "footer",
    "form": "key_value",
    "formula": "formula",
    "formula_number": "text",
    "header": "header",
    "image": "figure",
    "key_value": "key_value",
    "keyvalue": "key_value",
    "list_item": "list",
    "listitem": "list",
    "number": "text",
    "pagefooter": "footer",
    "pageheader": "header",
    "paragraph_title": "heading",
    "reference": "text",
    "reference_content": "text",
    "seal": "figure",
    "section_header": "heading",
    "sectionheader": "heading",
    "table": "table",
    "text": "text",
    "title": "heading",
    "vision_footnote": "footer",
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


def _build_pp_doclayout_v3_components(transformers_module: ModuleType, model_id: str, device: int):
    image_processor = transformers_module.AutoImageProcessor.from_pretrained(model_id)
    model = transformers_module.AutoModelForObjectDetection.from_pretrained(model_id)
    torch_device = torch.device(f"cuda:{device}") if device >= 0 and torch.cuda.is_available() else torch.device("cpu")
    model.to(torch_device)
    model.eval()
    return image_processor, model, torch_device


def normalize_layout_label(provider_label: str) -> str:
    normalized = provider_label.strip().lower().replace("-", "_").replace(" ", "_")
    compact = normalized.replace("_", "")
    return _LAYOUT_LABEL_MAP.get(normalized, _LAYOUT_LABEL_MAP.get(compact, normalized))


def _layout_box(raw_region: object) -> tuple[int, int, int, int]:
    if isinstance(raw_region, dict):
        box = raw_region.get("box")
        if isinstance(box, dict):
            return (
                int(float(box.get("xmin", 0))),
                int(float(box.get("ymin", 0))),
                int(float(box.get("xmax", 0))),
                int(float(box.get("ymax", 0))),
            )
    if hasattr(raw_region, "tolist"):
        values = raw_region.tolist()
    else:
        values = list(raw_region)
    return tuple(int(float(value)) for value in values[:4])


def _polygon_points(raw_polygon: object) -> list[tuple[int, int]] | None:
    if raw_polygon is None:
        return None
    points: list[tuple[int, int]] = []
    for raw_point in raw_polygon:
        if hasattr(raw_point, "tolist"):
            raw_point = raw_point.tolist()
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) != 2:
            continue
        try:
            points.append((int(float(raw_point[0])), int(float(raw_point[1]))))
        except (TypeError, ValueError):
            continue
    return points if len(points) >= 3 else None


class PPDocLayoutV3LayoutProvider:
    """Transformers-backed PP-DocLayoutV3 adapter."""

    def __init__(self, provider_name: str | None = None, model_id: str = _PP_DOCLAYOUT_V3_MODEL_ID) -> None:
        self.name = provider_name or "pp-doclayout-v3"
        self.model_id = model_id
        self._image_processor = None
        self._model = None
        self._device = torch.device("cpu")

    def load(self) -> None:
        if self._model is not None and self._image_processor is not None:
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
            self._image_processor, self._model, self._device = _build_pp_doclayout_v3_components(
                transformers_module,
                self.model_id,
                device,
            )
        except ValueError as exc:
            raise RuntimeError(
                "Failed to initialize pp-doclayout-v3 through Transformers. "
                f"Installed transformers version: {installed_version}."
            ) from exc

    def detect(self, image_bytes: bytes) -> LayoutResult:
        if self._model is None or self._image_processor is None:
            raise RuntimeError("PPDocLayoutV3LayoutProvider.load() must be called before detect()")

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        with gpu_scheduler.claim("layout"):
            inputs = self._image_processor(images=image, return_tensors="pt")
            inputs = inputs.to(self._device)
            with torch.inference_mode():
                outputs = self._model(**inputs)
            processed = self._image_processor.post_process_object_detection(
                outputs,
                threshold=0.5,
                target_sizes=[(image.height, image.width)],
            )

        raw_result = processed[0] if processed else {}
        raw_scores = raw_result.get("scores") if isinstance(raw_result, dict) else []
        raw_labels = raw_result.get("labels") if isinstance(raw_result, dict) else []
        raw_boxes = raw_result.get("boxes") if isinstance(raw_result, dict) else []
        raw_polygons = raw_result.get("polygon_points") if isinstance(raw_result, dict) else []
        raw_order = raw_result.get("order_seq") if isinstance(raw_result, dict) else []
        if raw_scores is None:
            raw_scores = []
        if raw_labels is None:
            raw_labels = []
        if raw_boxes is None:
            raw_boxes = []
        if raw_polygons is None:
            raw_polygons = []
        if raw_order is None:
            raw_order = []

        regions: list[LayoutRegion] = []
        confidences: list[float] = []
        for idx, raw_box in enumerate(raw_boxes, 1):
            x0, y0, x1, y1 = _layout_box(raw_box)
            label_value = raw_labels[idx - 1]
            label_id = int(label_value.item()) if hasattr(label_value, "item") else int(label_value)
            provider_label = str(self._model.config.id2label.get(label_id, "text"))
            score_value = raw_scores[idx - 1]
            confidence = float(score_value.item()) if hasattr(score_value, "item") else float(score_value)
            polygon = _polygon_points(raw_polygons[idx - 1] if idx - 1 < len(raw_polygons) else None)
            order_value = raw_order[idx - 1] if idx - 1 < len(raw_order) else idx - 1
            reading_order = int(order_value.item()) if hasattr(order_value, "item") else int(order_value)
            confidences.append(confidence)
            regions.append(
                LayoutRegion(
                    region_id=f"region-{idx:04d}",
                    region_type=normalize_layout_label(provider_label),
                    bbox=(x0, y0, x1, y1),
                    confidence=confidence,
                    reading_order=reading_order,
                    polygon=polygon,
                    metadata={
                        "provider": self.name,
                        "provider_label": provider_label,
                        "model_id": self.model_id,
                        "has_polygon": polygon is not None,
                        "order_seq": reading_order,
                    },
                )
            )

        return LayoutResult(
            page_id="page",
            regions=regions,
            layout_confidence=(sum(confidences) / len(confidences)) if confidences else None,
        )

    def offload(self) -> None:
        self._image_processor = None
        self._model = None
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


class PaddleOCRLayoutServiceProvider:
    """HTTP-backed PaddleOCR layout adapter isolated from the main runtime dependency stack."""

    def __init__(self, provider_name: str | None = None, base_url: str | None = None) -> None:
        settings = get_settings().layout
        self.name = provider_name or "paddleocr-layout-service"
        self._base_url = (base_url or settings.base_url).rstrip("/")
        self._session: requests.Session | None = None

    def load(self) -> None:
        if self._session is not None:
            return
        settings = get_settings().layout
        self._session = requests.Session()
        payload = {
            "provider": self.name,
            "model_name": settings.model_name,
            "img_size": list(settings.img_size) if isinstance(settings.img_size, tuple) else settings.img_size,
            "threshold": settings.threshold,
            "formula_threshold": settings.formula_threshold,
            "layout_nms": settings.layout_nms,
            "layout_unclip_ratio": list(settings.layout_unclip_ratio) if isinstance(settings.layout_unclip_ratio, tuple) else settings.layout_unclip_ratio,
            "layout_merge_bboxes_mode": settings.layout_merge_bboxes_mode,
        }
        try:
            response = self._session.post(
                f"{self._base_url}/load",
                json=payload,
                timeout=_LAYOUT_SERVICE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self.offload()
            raise RuntimeError(f"PaddleOCR layout service load failed: {exc}") from exc

    def detect(self, image_bytes: bytes) -> LayoutResult:
        if self._session is None:
            raise RuntimeError("PaddleOCRLayoutServiceProvider.load() must be called before detect()")

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
            raise RuntimeError(f"PaddleOCR layout service detect failed: {exc}") from exc
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
        polygon_obj = raw_region.get("polygon")
        polygon = None
        if isinstance(polygon_obj, list):
            normalized_points: list[tuple[int, int]] = []
            for point in polygon_obj:
                if isinstance(point, (list, tuple)) and len(point) == 2:
                    normalized_points.append((int(float(point[0])), int(float(point[1]))))
            polygon = normalized_points if len(normalized_points) >= 3 else None
        regions.append(
            LayoutRegion(
                region_id=f"region-{idx:04d}",
                region_type=region_type,
                bbox=tuple(int(float(value)) for value in bbox),
                confidence=confidence,
                reading_order=reading_order,
                polygon=polygon,
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
    if configured in {"paddleocr-layout-service", "paddleocr-layout", "paddle-layout", "paddleocr"}:
        return PaddleOCRLayoutServiceProvider(provider_name="paddleocr-layout-service")
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
    "PaddleOCRLayoutServiceProvider",
    "build_layout_provider",
    "crop_region_image",
    "normalize_layout_label",
]
