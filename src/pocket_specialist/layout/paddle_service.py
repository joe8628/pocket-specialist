"""Isolated PaddleOCR layout microservice.

This service keeps PaddleOCR and its compatible runtime outside the main
pipeline environment. It exposes the same small JSON layout API used by the
main process layout providers.
"""

from __future__ import annotations

import argparse
import base64
import gc
import json
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from typing import Any, Protocol

import numpy as np
from PIL import Image

try:
    from pocket_specialist.core.gpu import gpu_scheduler
except Exception:  # pragma: no cover - isolated service fallback when torch is absent
    from contextlib import contextmanager

    class _FallbackGPUScheduler:
        @contextmanager
        def claim(self, _resource: str):
            yield

    gpu_scheduler = _FallbackGPUScheduler()


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


def normalize_layout_label(provider_label: str) -> str:
    normalized = provider_label.strip().lower().replace("-", "_").replace(" ", "_")
    compact = normalized.replace("_", "")
    return _LAYOUT_LABEL_MAP.get(normalized, _LAYOUT_LABEL_MAP.get(compact, normalized))


def _normalize_model_name(model_name: str) -> str:
    stripped = model_name.strip()
    if not stripped:
        return "PP-DocLayout_plus-L"
    short_name = stripped.split("/", 1)[-1]
    if short_name.endswith("_safetensors"):
        short_name = short_name[: -len("_safetensors")]
    if short_name in {"PP-DocLayoutV3", "PP-DocLayoutV3_safetensors"}:
        return "PP-DocLayout_plus-L"
    return short_name


@dataclass(slots=True)
class ServiceLayoutRegion:
    region_type: str
    bbox: list[int]
    confidence: float
    reading_order: int
    provider_label: str
    polygon: list[list[int]] | None = None


@dataclass(slots=True)
class ServiceLayoutResult:
    page_id: str
    regions: list[ServiceLayoutRegion]
    layout_confidence: float | None


@dataclass(slots=True)
class PaddleLayoutConfig:
    model_name: str = "PP-DocLayoutV3"
    img_size: int | list[int] | None = None
    threshold: float | None = None
    formula_threshold: float | None = None
    layout_nms: bool | None = None
    layout_unclip_ratio: float | list[float] | None = None
    layout_merge_bboxes_mode: str | None = None


class LayoutRuntime(Protocol):
    @property
    def loaded(self) -> bool: ...
    def load(self, config: PaddleLayoutConfig | None = None) -> None: ...
    def detect(self, image_bytes: bytes) -> ServiceLayoutResult: ...
    def offload(self) -> None: ...


class PaddleOCRLayoutRuntime:
    """Lazy PaddleOCR runtime used inside the isolated layout service process."""

    def __init__(self) -> None:
        self._predictor = None
        self._config = PaddleLayoutConfig()

    @property
    def loaded(self) -> bool:
        return self._predictor is not None

    def load(self, config: PaddleLayoutConfig | None = None) -> None:
        if config is not None:
            self._config = config
        if self._predictor is not None:
            return
        try:
            from paddleocr import LayoutDetection
        except ImportError as exc:
            raise RuntimeError("PaddleOCR is not installed in the layout service environment") from exc

        kwargs: dict[str, object] = {
            "model_name": _normalize_model_name(self._config.model_name),
            "device": "gpu" if _gpu_available() else "cpu",
            "enable_hpi": _gpu_available(),
        }
        if self._config.layout_merge_bboxes_mode is not None:
            kwargs["layout_merge_bboxes_mode"] = self._config.layout_merge_bboxes_mode
        with gpu_scheduler.claim("layout"):
            self._predictor = LayoutDetection(**kwargs)

    def detect(self, image_bytes: bytes) -> ServiceLayoutResult:
        if self._predictor is None:
            self.load()

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        image_np = np.array(image)
        predict_kwargs: dict[str, object] = {"input": image_np, "batch_size": 1}
        if self._config.threshold is not None:
            predict_kwargs["threshold"] = self._config.threshold
        if self._config.img_size is not None:
            predict_kwargs["img_size"] = self._config.img_size
        if self._config.layout_nms is not None:
            predict_kwargs["layout_nms"] = self._config.layout_nms

        with gpu_scheduler.claim("layout"):
            prediction = _predict_with_supported_kwargs(self._predictor, predict_kwargs)

        raw_result = next(iter(prediction), None)
        raw_boxes = _extract_layout_boxes(raw_result)
        regions: list[ServiceLayoutRegion] = []
        confidences: list[float] = []
        for idx, raw_box in enumerate(raw_boxes, 1):
            provider_label = str(raw_box.get("label") or raw_box.get("cls_name") or raw_box.get("type") or "text")
            confidence = float(raw_box.get("score") or raw_box.get("confidence") or 0.0)
            region_type = normalize_layout_label(provider_label)
            if self._config.threshold is not None and confidence < self._config.threshold:
                continue
            if region_type == "formula" and self._config.formula_threshold is not None and confidence < self._config.formula_threshold:
                continue
            polygon = _normalize_polygon(raw_box.get("coordinate") or raw_box.get("points") or raw_box.get("polygon"))
            bbox = _bbox_from_payload(raw_box, polygon)
            if bbox is None:
                continue
            confidences.append(confidence)
            regions.append(
                ServiceLayoutRegion(
                    region_type=region_type,
                    bbox=list(bbox),
                    confidence=confidence,
                    reading_order=idx - 1,
                    provider_label=provider_label,
                    polygon=[[x, y] for x, y in polygon] if polygon else None,
                )
            )
        return ServiceLayoutResult(
            page_id="page",
            regions=regions,
            layout_confidence=(sum(confidences) / len(confidences)) if confidences else None,
        )

    def offload(self) -> None:
        self._predictor = None
        gc.collect()
        try:
            import paddle
            if paddle.device.is_compiled_with_cuda():
                paddle.device.cuda.empty_cache()
        except Exception:
            pass


def _gpu_available() -> bool:
    try:
        import paddle
        return bool(paddle.device.is_compiled_with_cuda())
    except Exception:
        return False


def _normalize_polygon(raw_polygon: object) -> list[tuple[int, int]] | None:
    if raw_polygon is None:
        return None
    points: list[tuple[int, int]] = []
    if isinstance(raw_polygon, dict):
        raw_polygon = raw_polygon.get("points") or raw_polygon.get("polygon")
    if not isinstance(raw_polygon, list):
        return None
    if raw_polygon and all(isinstance(value, (int, float)) for value in raw_polygon):
        if len(raw_polygon) % 2 != 0:
            return None
        raw_polygon = [raw_polygon[idx:idx + 2] for idx in range(0, len(raw_polygon), 2)]
    for raw_point in raw_polygon:
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) != 2:
            continue
        try:
            points.append((int(float(raw_point[0])), int(float(raw_point[1]))))
        except (TypeError, ValueError):
            continue
    return points if len(points) >= 3 else None


def _predict_with_supported_kwargs(predictor, kwargs: dict[str, object]):
    current_kwargs = dict(kwargs)
    while True:
        try:
            return predictor.predict(**current_kwargs)
        except TypeError as exc:
            message = str(exc)
            marker = "unexpected keyword argument "
            if marker not in message:
                raise
            keyword = message.split(marker, 1)[1].strip().replace("'", "").replace('"', "")
            if keyword not in current_kwargs:
                raise
            current_kwargs.pop(keyword, None)


def _bbox_from_payload(raw_box: dict[str, Any], polygon: list[tuple[int, int]] | None) -> tuple[int, int, int, int] | None:
    bbox = raw_box.get("bbox") or raw_box.get("box")
    if isinstance(bbox, dict):
        try:
            return (
                int(float(bbox.get("xmin", bbox.get("x0", 0)))),
                int(float(bbox.get("ymin", bbox.get("y0", 0)))),
                int(float(bbox.get("xmax", bbox.get("x1", 0)))),
                int(float(bbox.get("ymax", bbox.get("y1", 0)))),
            )
        except (TypeError, ValueError):
            return None
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            return tuple(int(float(value)) for value in bbox)
        except (TypeError, ValueError):
            return None
    if polygon is None:
        return None
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def _extract_layout_boxes(raw_result: object) -> list[dict[str, Any]]:
    candidate = raw_result
    if hasattr(candidate, "res"):
        candidate = getattr(candidate, "res")
    if hasattr(candidate, "json") and not isinstance(candidate, (dict, list)):
        try:
            json_candidate = getattr(candidate, "json")
            candidate = json.loads(json_candidate if isinstance(json_candidate, str) else json_candidate())
        except Exception:
            pass
    if isinstance(candidate, dict):
        if isinstance(candidate.get("boxes"), list):
            return [item for item in candidate["boxes"] if isinstance(item, dict)]
        if isinstance(candidate.get("res"), dict) and isinstance(candidate["res"].get("boxes"), list):
            return [item for item in candidate["res"]["boxes"] if isinstance(item, dict)]
    if isinstance(candidate, list):
        return [item for item in candidate if isinstance(item, dict)]
    return []


@dataclass(slots=True)
class ServiceState:
    runtime: LayoutRuntime


class PaddleOCRLayoutRequestHandler(BaseHTTPRequestHandler):
    state: ServiceState

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"ok": True, "provider": "paddleocr-layout-service", "loaded": self.state.runtime.loaded})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/load":
                self.state.runtime.load(_config_from_payload(payload))
                self._send_json({"ok": True, "provider": "paddleocr-layout-service"})
                return
            if self.path == "/offload":
                self.state.runtime.offload()
                self._send_json({"ok": True, "provider": "paddleocr-layout-service"})
                return
            if self.path == "/detect":
                image = _decode_image_payload(payload, key="image")
                result = self.state.runtime.detect(image)
                self._send_json(asdict(result))
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length) if length else b"{}"
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _config_from_payload(payload: dict[str, Any]) -> PaddleLayoutConfig:
    return PaddleLayoutConfig(
        model_name=str(payload.get("model_name") or "PP-DocLayoutV3"),
        img_size=payload.get("img_size") if isinstance(payload.get("img_size"), (int, list)) else None,
        threshold=float(payload["threshold"]) if payload.get("threshold") is not None else None,
        formula_threshold=float(payload["formula_threshold"]) if payload.get("formula_threshold") is not None else None,
        layout_nms=bool(payload["layout_nms"]) if payload.get("layout_nms") is not None else None,
        layout_unclip_ratio=payload.get("layout_unclip_ratio") if isinstance(payload.get("layout_unclip_ratio"), (int, float, list)) else None,
        layout_merge_bboxes_mode=str(payload["layout_merge_bboxes_mode"]) if payload.get("layout_merge_bboxes_mode") is not None else None,
    )


def _decode_image_payload(payload: dict[str, Any], *, key: str) -> bytes:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("image payload must be non-empty base64 text")
    return base64.b64decode(value, validate=True)


def serve(host: str = "127.0.0.1", port: int = 8003) -> None:
    PaddleOCRLayoutRequestHandler.state = ServiceState(runtime=PaddleOCRLayoutRuntime())
    server = ThreadingHTTPServer((host, port), PaddleOCRLayoutRequestHandler)
    try:
        server.serve_forever()
    finally:
        PaddleOCRLayoutRequestHandler.state.runtime.offload()
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the PaddleOCR layout extraction microservice")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
