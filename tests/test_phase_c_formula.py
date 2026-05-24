from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pocket_specialist.formula.providers import FormulaResult, UniMERNetFormulaExtractor
from pocket_specialist.layout.providers import LayoutRegion, LayoutResult
from pocket_specialist.formula.symbolic import inline_formula_candidates, looks_symbolic
from pocket_specialist.phases.extract import _ocr_page_blocks


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self) -> None:
        self.posts: list[tuple[str, object]] = []
        self.closed = False

    def post(self, url: str, json: object | None = None, timeout: int | float | None = None) -> FakeResponse:
        self.posts.append((url, json))
        if url.endswith("/load") or url.endswith("/offload"):
            return FakeResponse({"ok": True})
        if url.endswith("/extract"):
            return FakeResponse(
                {
                    "latex": "E = mc^2",
                    "mathml": None,
                    "provider": "unimernet",
                    "confidence": 0.91,
                    "is_inline": False,
                    "latency_ms": 12,
                }
            )
        raise AssertionError(url)

    def close(self) -> None:
        self.closed = True


class FakeFormulaExtractor:
    def extract(self, image_bytes: bytes) -> FormulaResult:
        return FormulaResult(
            latex="x^2 + y^2",
            mathml=None,
            provider="unimernet",
            confidence=0.88,
            is_inline=False,
            raw_response="x^2 + y^2",
            latency_ms=9,
        )


class FailingFormulaExtractor:
    def extract(self, image_bytes: bytes) -> FormulaResult:
        raise RuntimeError("formula service unavailable")


class FakeOCRProvider:
    name = "fake-ocr"

    def __init__(self, text: str) -> None:
        self.text = text

    def extract(self, image_bytes: bytes, region_type: str):
        return SimpleNamespace(
            provider=self.name,
            confidence=0.72,
            typed_content={"blocks": [{"raw_text": self.text, "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}]},
            extraction_metadata={"mode": "REGION_GUIDED"},
        )


class UnusedOCRProvider:
    def extract(self, image_bytes: bytes, region_type: str):  # pragma: no cover - should not be called
        raise AssertionError("OCR should not be called for formula-only layout regions")


class PhaseCFormulaTests(unittest.TestCase):
    def test_unimernet_client_normalizes_extract_response(self) -> None:
        session = FakeSession()
        with patch("pocket_specialist.formula.providers.requests.Session", return_value=session):
            extractor = UniMERNetFormulaExtractor(base_url="http://formula.local", model_size="small")
            extractor.load()
            result = extractor.extract(b"image-bytes")
            extractor.offload()

        self.assertEqual(result.latex, "E = mc^2")
        self.assertEqual(result.provider, "unimernet")
        self.assertEqual(result.confidence, 0.91)
        self.assertEqual(result.latency_ms, 12)
        self.assertTrue(session.closed)
        self.assertEqual(session.posts[0][0], "http://formula.local/load")
        self.assertEqual(session.posts[1][0], "http://formula.local/extract")
        self.assertEqual(session.posts[2][0], "http://formula.local/offload")

    def test_formula_layout_region_becomes_formula_block(self) -> None:
        image = Image.new("RGB", (24, 24), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[LayoutRegion("region-0001", "formula", (0, 0, 24, 24), 0.97, 0)],
            layout_confidence=0.97,
        )

        blocks = _ocr_page_blocks(
            "doc",
            1,
            buffer.getvalue(),
            layout,
            UnusedOCRProvider(),
            None,
            FakeFormulaExtractor(),
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].block_type, "FormulaBlock")
        self.assertEqual(blocks[0].content["type"], "FormulaBlock")
        self.assertEqual(blocks[0].content["latex"], "x^2 + y^2")
        self.assertEqual(blocks[0].provenance.source_stage, "formula_extract")
        self.assertEqual(blocks[0].provenance.provider, "unimernet")

    def test_inline_symbolic_heuristic_detects_delimited_math(self) -> None:
        candidates = inline_formula_candidates("Energy is $E=mc^2$ in relativity.")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].latex, "E=mc^2")
        self.assertTrue(looks_symbolic(candidates[0].latex))

    def test_ocr_text_region_emits_inline_formula_block(self) -> None:
        image = Image.new("RGB", (24, 24), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[LayoutRegion("region-0001", "text", (0, 0, 24, 24), 0.97, 0)],
            layout_confidence=0.97,
        )

        blocks = _ocr_page_blocks(
            "doc",
            1,
            buffer.getvalue(),
            layout,
            FakeOCRProvider("Energy is $E=mc^2$ in relativity."),
            None,
            None,
        )

        formula_blocks = [block for block in blocks if block.block_type == "FormulaBlock"]
        self.assertEqual(len(formula_blocks), 1)
        self.assertEqual(formula_blocks[0].content["latex"], "E=mc^2")
        self.assertEqual(formula_blocks[0].content["inline"], True)
        self.assertEqual(formula_blocks[0].provenance.metadata["route"], "inline_heuristic")

    def test_formula_failure_uses_symbolic_ocr_fallback(self) -> None:
        image = Image.new("RGB", (24, 24), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[LayoutRegion("region-0001", "formula", (0, 0, 24, 24), 0.97, 0)],
            layout_confidence=0.97,
        )

        blocks = _ocr_page_blocks(
            "doc",
            1,
            buffer.getvalue(),
            layout,
            FakeOCRProvider("E = mc^2"),
            None,
            FailingFormulaExtractor(),
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].content["type"], "FormulaBlock")
        self.assertEqual(blocks[0].content["provider"], "symbolic-fallback")
        self.assertEqual(blocks[0].content["latex"], "E = mc^2")
        self.assertEqual(blocks[0].provenance.metadata["fallback_reason"], "formula service unavailable")


if __name__ == "__main__":
    unittest.main()
