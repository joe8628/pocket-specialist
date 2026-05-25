from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pocket_specialist.core.tasks import RegionUnit
from pocket_specialist.ocr.providers import OllamaOCRProvider, SuryaOCRProvider


class SequencedResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class SequencedSession:
    def __init__(self, payloads: list[object]) -> None:
        self._payloads = payloads
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, json: object | None = None, timeout: int | float | None = None) -> SequencedResponse:
        del url, timeout
        assert isinstance(json, dict)
        self.posts.append(json)
        return SequencedResponse(self._payloads.pop(0))

    def close(self) -> None:
        return None


def _settings(*, batch_size: int = 4):
    ocr = SimpleNamespace(ollama_base_url="http://ollama.local", timeout_seconds=30, batch_size=batch_size, language="en")
    return SimpleNamespace(ocr=ocr)


def _png_bytes() -> bytes:
    image = Image.new("RGB", (12, 12), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _surya_prediction(text: str = "hello") -> object:
    line = SimpleNamespace(text=text, bbox=(0, 0, 10, 10), confidence=0.9)
    return SimpleNamespace(text_lines=[line])


def test_ollama_provider_retries_with_constrained_prompt_on_validation_error() -> None:
    session = SequencedSession(
        [
            {"response": "not json"},
            {
                "response": '{"blocks": [{"bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}, "raw_text": "ok", "confidence": 1.0, "block_type": "text"}]}'
            },
        ]
    )

    with patch("pocket_specialist.ocr.providers.get_settings", return_value=_settings()), \
         patch("pocket_specialist.ocr.providers.requests.Session", return_value=session):
        provider = OllamaOCRProvider("glm-ocr")
        provider.load()
        result = provider.extract(_png_bytes(), "text")

    assert result.typed_content["blocks"][0]["raw_text"] == "ok"
    assert result.extraction_metadata["attempt_count"] == 2
    assert result.extraction_metadata["retry_strategy"] == "constrained_prompt"
    assert result.extraction_metadata["constrained_prompt"] is True
    assert len(session.posts) == 2
    assert "Do not emit markdown fences" in str(session.posts[1]["prompt"])


def test_surya_provider_splits_batch_on_timeout() -> None:
    class FakePredictor:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def __call__(self, images, det_predictor=None):
            del det_predictor
            self.calls.append(len(images))
            if len(images) > 1:
                raise TimeoutError("timed out")
            return [_surya_prediction()]

    predictor = FakePredictor()
    provider = SuryaOCRProvider()
    provider._det_predictor = object()
    provider._rec_predictor = predictor

    units = [
        RegionUnit(unit_id="u1", doc_id="doc", region_type="text", image_bytes=_png_bytes()),
        RegionUnit(unit_id="u2", doc_id="doc", region_type="text", image_bytes=_png_bytes()),
    ]

    with patch("pocket_specialist.ocr.providers.get_settings", return_value=_settings(batch_size=4)):
        results = provider.extract_batch(units)

    assert len(results) == 2
    assert predictor.calls == [2, 1, 1]


def test_surya_provider_splits_batch_on_oom() -> None:
    class FakePredictor:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def __call__(self, images, det_predictor=None):
            del det_predictor
            self.calls.append(len(images))
            if len(images) > 1:
                raise RuntimeError("CUDA out of memory")
            return [_surya_prediction()]

    predictor = FakePredictor()
    provider = SuryaOCRProvider()
    provider._det_predictor = object()
    provider._rec_predictor = predictor

    units = [
        RegionUnit(unit_id="u1", doc_id="doc", region_type="text", image_bytes=_png_bytes()),
        RegionUnit(unit_id="u2", doc_id="doc", region_type="text", image_bytes=_png_bytes()),
    ]

    with patch("pocket_specialist.ocr.providers.get_settings", return_value=_settings(batch_size=4)):
        results = provider.extract_batch(units)

    assert len(results) == 2
    assert predictor.calls == [2, 1, 1]
