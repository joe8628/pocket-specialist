"""Lightweight UniMERNet formula extraction microservice.

The service intentionally keeps UniMERNet imports out of the main ingestion runtime. It exposes
small JSON endpoints consumed by ``UniMERNetFormulaExtractor``.
"""

from __future__ import annotations

import argparse
import base64
import gc
import json
import os
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from pocket_specialist.formula.providers import FormulaResult


class FormulaRuntime(Protocol):
    def load(self, model_size: str) -> None: ...
    def extract(self, image_bytes: bytes, *, model_size: str) -> FormulaResult: ...
    def offload(self) -> None: ...


class UniMERNetRuntime:
    """Lazy UniMERNet runtime used inside the isolated formula service process."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path
        self._model_size: str | None = None
        self._task: Any = None
        self._model: Any = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self, model_size: str) -> None:
        if self._model is not None and self._model_size == model_size:
            return
        self.offload()
        try:
            from unimernet.common.config import Config
            from unimernet.tasks import setup_task
        except ImportError as exc:
            raise RuntimeError("UniMERNet is not installed in the formula service environment") from exc

        config_path = self._resolve_config_path(model_size)
        cfg = Config.from_file(str(config_path))
        self._task = setup_task(cfg)
        self._model = self._task.build_model(cfg)
        if hasattr(self._model, "eval"):
            self._model.eval()
        self._model_size = model_size

    def extract(self, image_bytes: bytes, *, model_size: str) -> FormulaResult:
        if self._model is None or self._model_size != model_size:
            self.load(model_size)

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        started = time.monotonic()
        latex = self._predict_latex(image)
        latency_ms = int((time.monotonic() - started) * 1000)
        return FormulaResult(
            latex=latex.strip(),
            mathml=None,
            provider="unimernet",
            confidence=None,
            is_inline=False,
            raw_response=latex,
            latency_ms=latency_ms,
        )

    def offload(self) -> None:
        self._model = None
        self._task = None
        self._model_size = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def _resolve_config_path(self, model_size: str) -> Path:
        if self._config_path is not None:
            return self._config_path
        env_path = os.getenv("UNIMERNET_CONFIG") or os.getenv("PIPELINE_UNIMERNET_CONFIG")
        if env_path:
            return Path(env_path).expanduser().resolve()
        return Path("models") / f"unimernet_{model_size}.yaml"

    def _predict_latex(self, image: Image.Image) -> str:
        if self._task is not None:
            for method_name in ("inference", "predict", "generate"):
                method = getattr(self._task, method_name, None)
                if callable(method):
                    result = method([image])
                    return _coerce_latex(result)
        if self._model is not None:
            for method_name in ("inference", "predict", "generate"):
                method = getattr(self._model, method_name, None)
                if callable(method):
                    result = method([image])
                    return _coerce_latex(result)
        raise RuntimeError("Loaded UniMERNet runtime does not expose a supported inference method")


def _coerce_latex(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("latex", "pred", "prediction", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    if isinstance(value, list) or isinstance(value, tuple):
        if not value:
            return ""
        return _coerce_latex(value[0])
    return str(value)


@dataclass(slots=True)
class ServiceState:
    runtime: FormulaRuntime
    default_model_size: str = "base"


class UniMERNetRequestHandler(BaseHTTPRequestHandler):
    state: ServiceState

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        loaded = bool(getattr(self.state.runtime, "loaded", False))
        self._send_json({"ok": True, "provider": "unimernet", "loaded": loaded, "model_size": self.state.default_model_size})

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/load":
                model_size = str(payload.get("model_size") or self.state.default_model_size)
                self.state.runtime.load(model_size)
                self.state.default_model_size = model_size
                self._send_json({"ok": True, "provider": "unimernet", "model_size": model_size})
                return
            if self.path == "/offload":
                self.state.runtime.offload()
                self._send_json({"ok": True, "provider": "unimernet"})
                return
            if self.path == "/extract":
                image = _decode_image_payload(payload, key="image")
                model_size = str(payload.get("model_size") or self.state.default_model_size)
                result = self.state.runtime.extract(image, model_size=model_size)
                self._send_json(asdict(result))
                return
            if self.path == "/extract_batch":
                images = payload.get("images")
                if not isinstance(images, list):
                    raise ValueError("images must be a list")
                model_size = str(payload.get("model_size") or self.state.default_model_size)
                results = [asdict(self.state.runtime.extract(_decode_b64_image(item), model_size=model_size)) for item in images]
                self._send_json({"results": results})
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
    return _decode_b64_image(value)


def _decode_b64_image(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("image payload must be non-empty base64 text")
    return base64.b64decode(value, validate=True)


def serve(host: str = "127.0.0.1", port: int = 8001, model_size: str = "base", config_path: Path | None = None) -> None:
    UniMERNetRequestHandler.state = ServiceState(runtime=UniMERNetRuntime(config_path=config_path), default_model_size=model_size)
    server = ThreadingHTTPServer((host, port), UniMERNetRequestHandler)
    try:
        server.serve_forever()
    finally:
        UniMERNetRequestHandler.state.runtime.offload()
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the UniMERNet formula extraction microservice")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--model-size", default="base", choices=["base", "small", "tiny"])
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port, model_size=args.model_size, config_path=args.config)


if __name__ == "__main__":
    main()
