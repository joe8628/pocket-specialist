"""Isolated Surya layout microservice.

This service keeps Surya and its compatible runtime outside the main
pipeline environment. It exposes small JSON endpoints consumed by
``SuryaLayoutServiceProvider``.
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

from PIL import Image

from pocket_specialist.core.gpu import gpu_scheduler


_LAYOUT_LABEL_MAP = {
    "abstract": "text",
    "bibliography": "text",
    "blankpage": "text",
    "caption": "text",
    "chemicalblock": "formula",
    "code": "code",
    "diagram": "figure",
    "equation": "formula",
    "figure": "figure",
    "footnote": "footer",
    "form": "key_value",
    "formula": "formula",
    "header": "header",
    "keyvalue": "key_value",
    "key_value": "key_value",
    "listgroup": "list",
    "listitem": "list",
    "list_item": "list",
    "pagefooter": "footer",
    "pageheader": "header",
    "picture": "figure",
    "sectionheader": "heading",
    "section_header": "heading",
    "table": "table",
    "tableofcontents": "text",
    "text": "text",
    "text_inline_math": "formula",
    "title": "heading",
}


def _normalize_layout_label(provider_label: str) -> str:
    normalized = provider_label.strip().lower().replace("-", "_").replace(" ", "_")
    compact = normalized.replace("_", "")
    return _LAYOUT_LABEL_MAP.get(normalized, _LAYOUT_LABEL_MAP.get(compact, normalized))


@dataclass(slots=True)
class ServiceLayoutRegion:
    region_type: str
    bbox: list[int]
    confidence: float
    reading_order: int
    provider_label: str
    raw_label: str | None = None
    polygon: list[list[int]] | None = None
    count: int | None = None


@dataclass(slots=True)
class ServiceLayoutResult:
    page_id: str
    regions: list[ServiceLayoutRegion]
    layout_confidence: float | None


class LayoutRuntime(Protocol):
    @property
    def loaded(self) -> bool: ...
    def load(self) -> None: ...
    def detect(self, image_bytes: bytes) -> ServiceLayoutResult: ...
    def offload(self) -> None: ...


class SuryaLayoutRuntime:
    """Lazy Surya runtime used inside the isolated layout service process."""

    def __init__(self) -> None:
        self._predictor = None
        self._manager = None

    @property
    def loaded(self) -> bool:
        return self._predictor is not None and self._manager is not None

    def load(self) -> None:
        if self.loaded:
            return
        try:
            from surya.inference import SuryaInferenceManager
            from surya.layout import LayoutPredictor
        except ImportError as exc:
            raise RuntimeError("Surya is not installed in the layout service environment") from exc

        with gpu_scheduler.claim("layout"):
            self._manager = SuryaInferenceManager()
            self._predictor = LayoutPredictor(self._manager)

    def detect(self, image_bytes: bytes) -> ServiceLayoutResult:
        if not self.loaded:
            self.load()

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        with gpu_scheduler.claim("layout"):
            result = self._predictor([image])[0]
        if bool(_get_value(result, "error", default=False)):
            raise RuntimeError("Surya layout inference returned an error result")

        raw_regions = list(_get_value(result, "bboxes", default=[]))
        raw_regions.sort(key=lambda item: int(_get_value(item, "position", default=0)))
        regions: list[ServiceLayoutRegion] = []
        confidences: list[float] = []
        for idx, raw_region in enumerate(raw_regions):
            bbox = _get_value(raw_region, "bbox", default=[0, 0, 0, 0])
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            provider_label = str(_get_value(raw_region, "label", default="Text"))
            raw_label = _get_value(raw_region, "raw_label", default=None)
            confidence = float(_get_value(raw_region, "confidence", default=1.0) or 1.0)
            count_value = _get_value(raw_region, "count", default=None)
            polygon = _normalize_polygon(_get_value(raw_region, "polygon", default=None))
            confidences.append(confidence)
            regions.append(
                ServiceLayoutRegion(
                    region_type=_normalize_layout_label(provider_label),
                    bbox=[int(float(value)) for value in bbox],
                    confidence=confidence,
                    reading_order=int(_get_value(raw_region, "position", default=idx)),
                    provider_label=provider_label,
                    raw_label=str(raw_label) if raw_label is not None else None,
                    polygon=polygon,
                    count=int(count_value) if count_value is not None else None,
                )
            )

        return ServiceLayoutResult(
            page_id="page",
            regions=regions,
            layout_confidence=(sum(confidences) / len(confidences)) if confidences else None,
        )

    def offload(self) -> None:
        self._predictor = None
        manager = self._manager
        self._manager = None
        if manager is not None:
            for method_name in ("shutdown", "close", "terminate"):
                method = getattr(manager, method_name, None)
                if callable(method):
                    method()
                    break
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


@dataclass(slots=True)
class ServiceState:
    runtime: LayoutRuntime


class SuryaLayoutRequestHandler(BaseHTTPRequestHandler):
    state: ServiceState

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json({"ok": True, "provider": "surya-layout-service", "loaded": self.state.runtime.loaded})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/load":
                self.state.runtime.load()
                self._send_json({"ok": True, "provider": "surya-layout-service"})
                return
            if self.path == "/offload":
                self.state.runtime.offload()
                self._send_json({"ok": True, "provider": "surya-layout-service"})
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


def _decode_image_payload(payload: dict[str, Any], *, key: str) -> bytes:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("image payload must be non-empty base64 text")
    return base64.b64decode(value, validate=True)


def _get_value(obj: Any, key: str, *, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_polygon(raw_polygon: Any) -> list[list[int]] | None:
    if not isinstance(raw_polygon, (list, tuple)):
        return None
    points: list[list[int]] = []
    for point in raw_polygon:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        points.append([int(float(point[0])), int(float(point[1]))])
    return points if len(points) >= 3 else None


def serve(host: str = "127.0.0.1", port: int = 8002) -> None:
    SuryaLayoutRequestHandler.state = ServiceState(runtime=SuryaLayoutRuntime())
    server = ThreadingHTTPServer((host, port), SuryaLayoutRequestHandler)
    try:
        server.serve_forever()
    finally:
        SuryaLayoutRequestHandler.state.runtime.offload()
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Surya layout extraction microservice")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
