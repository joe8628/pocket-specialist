"""Isolated Surya layout microservice.

This service keeps Surya and its compatible Transformers stack outside the main
pipeline runtime. It exposes small JSON endpoints consumed by
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


@dataclass(slots=True)
class ServiceLayoutResult:
    page_id: str
    regions: list[ServiceLayoutRegion]
    layout_confidence: float | None


class LayoutRuntime(Protocol):
    def load(self) -> None: ...
    def detect(self, image_bytes: bytes) -> ServiceLayoutResult: ...
    def offload(self) -> None: ...


class SuryaLayoutRuntime:
    """Lazy Surya runtime used inside the isolated layout service process."""

    def __init__(self) -> None:
        self._predictor = None
        self._foundation = None

    @property
    def loaded(self) -> bool:
        return self._predictor is not None

    def load(self) -> None:
        if self._predictor is not None:
            return
        try:
            from surya.foundation import FoundationPredictor
            from surya.layout import LayoutPredictor
            from surya.settings import settings
        except ImportError as exc:
            raise RuntimeError("Surya is not installed in the layout service environment") from exc

        with gpu_scheduler.claim("layout"):
            self._foundation = FoundationPredictor(checkpoint=settings.LAYOUT_MODEL_CHECKPOINT)
            self._predictor = LayoutPredictor(self._foundation)

    def detect(self, image_bytes: bytes) -> ServiceLayoutResult:
        if self._predictor is None:
            self.load()

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        with gpu_scheduler.claim("layout"):
            result = self._predictor([image])[0]

        raw_regions = sorted(getattr(result, "bboxes", []), key=lambda item: getattr(item, "position", 0))
        regions: list[ServiceLayoutRegion] = []
        confidences: list[float] = []
        for idx, raw_region in enumerate(raw_regions):
            x0, y0, x1, y1 = getattr(raw_region, "bbox")
            provider_label = str(getattr(raw_region, "label", "Text"))
            confidence = float(getattr(raw_region, "confidence", 1.0) or 1.0)
            confidences.append(confidence)
            regions.append(
                ServiceLayoutRegion(
                    region_type=_normalize_layout_label(provider_label),
                    bbox=[int(x0), int(y0), int(x1), int(y1)],
                    confidence=confidence,
                    reading_order=int(getattr(raw_region, "position", idx)),
                    provider_label=provider_label,
                )
            )

        return ServiceLayoutResult(
            page_id="page",
            regions=regions,
            layout_confidence=(sum(confidences) / len(confidences)) if confidences else None,
        )

    def offload(self) -> None:
        self._predictor = None
        self._foundation = None
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
