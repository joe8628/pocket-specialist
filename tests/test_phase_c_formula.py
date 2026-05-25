from __future__ import annotations

import json
import io
from contextlib import contextmanager
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pocket_specialist.core.config import PipelineSettings
from pocket_specialist.formula.providers import FormulaResult, UniMERNetFormulaExtractor
from pocket_specialist.layout.providers import LayoutRegion, LayoutResult
from pocket_specialist.handlers.intake import DocumentProfile, NativeTextBlock, PDFContentType, SourceKind
from pocket_specialist.formula.symbolic import inline_formula_candidates, looks_symbolic
from pocket_specialist.phases.extract import _matched_layout_region_ids, _native_page_blocks, _ocr_page_blocks, extract_structured_document


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


class FakeLayoutProvider:
    def __init__(self, layout: LayoutResult) -> None:
        self._layout = layout

    def load(self) -> None:
        return None

    def detect(self, image_bytes: bytes) -> LayoutResult:
        return self._layout

    def offload(self) -> None:
        return None


class FakeFormulaExtractor:
    def load(self) -> None:
        return None

    def offload(self) -> None:
        return None

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
    def load(self) -> None:
        return None

    def offload(self) -> None:
        return None

    def extract(self, image_bytes: bytes) -> FormulaResult:
        raise RuntimeError("formula service unavailable")


class FakeOCRProvider:
    name = "fake-ocr"

    def __init__(self, text: str) -> None:
        self.text = text

    def load(self) -> None:
        return None

    def offload(self) -> None:
        return None

    def extract(self, image_bytes: bytes, region_type: str):
        return SimpleNamespace(
            provider=self.name,
            confidence=0.72,
            typed_content={"blocks": [{"raw_text": self.text, "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}]},
            extraction_metadata={"mode": "REGION_GUIDED"},
        )


class FakeStructuredTableOCRProvider:
    name = "fake-structured-table-ocr"

    def load(self) -> None:
        return None

    def offload(self) -> None:
        return None

    def extract(self, image_bytes: bytes, region_type: str):
        return SimpleNamespace(
            provider=self.name,
            confidence=0.83,
            typed_content={
                "type": "TableBlock",
                "headers": ["name", "value"],
                "rows": [{"name": "alpha", "value": "1"}, {"name": "beta", "value": "2"}],
                "caption": "Sample table",
                "blocks": [{"raw_text": "name | value\nalpha | 1\nbeta | 2", "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}}],
            },
            extraction_metadata={"mode": "REGION_GUIDED"},
        )


class UnusedOCRProvider:
    def load(self) -> None:
        return None

    def offload(self) -> None:
        return None

    def extract(self, image_bytes: bytes, region_type: str):  # pragma: no cover - should not be called
        raise AssertionError("OCR should not be called for formula-only layout regions")


class ClaimRecorder:
    def __init__(self) -> None:
        self.claims: list[str] = []

    @contextmanager
    def claim(self, resource_type: str):
        self.claims.append(resource_type)
        yield


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

    def test_native_table_region_emits_normalized_table_rows(self) -> None:
        layout = LayoutResult(
            page_id="page-0001",
            regions=[LayoutRegion("region-0001", "table", (0, 0, 30, 10), 0.97, 0)],
            layout_confidence=0.97,
        )
        native_blocks = [NativeTextBlock(text="name | value\nalpha | 1\nbeta | 2", bbox=(0, 0, 30, 10), block_no=0)]

        blocks = _native_page_blocks("doc", 1, native_blocks, layout)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].block_type, "TableBlock")
        self.assertEqual(blocks[0].content["headers"], ["name", "value"])
        self.assertEqual(blocks[0].content["rows"], [{"name": "alpha", "value": "1"}, {"name": "beta", "value": "2"}])
        self.assertNotIn("text", blocks[0].content)

    def test_ocr_figure_region_writes_artifact_uri(self) -> None:
        image = Image.new("RGB", (24, 24), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[LayoutRegion("region-0001", "figure", (0, 0, 24, 24), 0.97, 0)],
            layout_confidence=0.97,
        )
        settings = PipelineSettings.from_env(project_root=Path(tempfile.mkdtemp()))

        with patch("pocket_specialist.phases.extract.get_settings", return_value=settings):
            blocks = _ocr_page_blocks(
                "doc",
                1,
                buffer.getvalue(),
                layout,
                FakeOCRProvider("figure text"),
                None,
                None,
                artifact_root=settings.paths.artifact_path,
                project_root=settings.paths.project_root,
            )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].block_type, "FigureBlock")
        artifact_uri = blocks[0].content["artifact_uri"]
        self.assertTrue(isinstance(artifact_uri, str) and artifact_uri)
        self.assertTrue((settings.paths.project_root / artifact_uri).exists())
        self.assertEqual(blocks[0].provenance.metadata["artifact_uri"], artifact_uri)

    def test_ocr_table_region_preserves_structured_table_payload(self) -> None:
        image = Image.new("RGB", (24, 24), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[LayoutRegion("region-0001", "table", (0, 0, 24, 24), 0.97, 0)],
            layout_confidence=0.97,
        )

        blocks = _ocr_page_blocks(
            "doc",
            1,
            buffer.getvalue(),
            layout,
            FakeStructuredTableOCRProvider(),
            None,
            None,
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].content["headers"], ["name", "value"])
        self.assertEqual(blocks[0].content["rows"], [{"name": "alpha", "value": "1"}, {"name": "beta", "value": "2"}])
        self.assertEqual(blocks[0].content["caption"], "Sample table")
        self.assertNotIn("text", blocks[0].content)

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
        self.assertEqual(blocks[0].content["provider"], "ocr-fallback")
        self.assertEqual(blocks[0].content["latex"], "E = mc^2")
        self.assertEqual(blocks[0].provenance.metadata["fallback_reason"], "formula service unavailable")

    def test_native_region_coverage_leaves_unmatched_formula_for_followup_routing(self) -> None:
        layout = LayoutResult(
            page_id="page-0001",
            regions=[
                LayoutRegion("region-0001", "text", (0, 0, 30, 10), 0.97, 0),
                LayoutRegion("region-0002", "formula", (40, 0, 60, 20), 0.97, 1),
            ],
            layout_confidence=0.97,
        )
        native_blocks = [NativeTextBlock(text="native paragraph", bbox=(0, 0, 30, 10), block_no=0)]

        matched = _matched_layout_region_ids(native_blocks, layout)

        self.assertEqual(matched, {"region-0001"})

    def test_extract_structured_document_collects_pdf_figure_artifacts(self) -> None:
        image = Image.new("RGB", (64, 32), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[LayoutRegion("region-0001", "figure", (0, 0, 24, 24), 0.97, 0)],
            layout_confidence=0.97,
        )
        profile = DocumentProfile(
            doc_id="doc",
            source_path=Path("/tmp/doc.pdf"),
            source_kind=SourceKind.PDF,
            mime_type="application/pdf",
            pdf_content_type=PDFContentType.SCANNED,
            page_count=1,
            text_extractable=False,
            metadata={"page_modes": [PDFContentType.SCANNED.value]},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            structured_dir = root / "structured-out"
            settings = PipelineSettings.from_env(project_root=root)
            with patch("pocket_specialist.phases.extract.classify_document", return_value=profile), \
                 patch("pocket_specialist.phases.extract.get_settings", return_value=settings), \
                 patch("pocket_specialist.phases.extract.render_pdf_page_to_bytes", return_value=buffer.getvalue()), \
                 patch("pocket_specialist.phases.extract.build_layout_provider", return_value=FakeLayoutProvider(layout)), \
                 patch("pocket_specialist.phases.extract.get_pdf_native_blocks", return_value=[]), \
                 patch("pocket_specialist.phases.extract.build_primary_ocr_provider", return_value=FakeOCRProvider("figure text")), \
                 patch("pocket_specialist.phases.extract.build_fallback_ocr_provider", return_value=None), \
                 patch("pocket_specialist.phases.extract.init_db"), \
                 patch("pocket_specialist.phases.extract.set_status"), \
                 patch("pocket_specialist.phases.extract.should_process", return_value=True):
                _, _, cif = extract_structured_document(
                    Path("/tmp/doc.pdf"),
                    structured_output_dir=structured_dir,
                    layout_output_dir=root / "layout-out",
                )

        self.assertEqual(len(cif.blocks), 1)
        self.assertEqual(cif.blocks[0].block_type, "FigureBlock")
        self.assertEqual(len(cif.artifacts), 1)
        self.assertEqual(cif.artifacts[0].artifact_type, "figure")
        self.assertEqual(cif.artifacts[0].uri, cif.blocks[0].content["artifact_uri"])
        self.assertTrue((settings.paths.project_root / cif.artifacts[0].uri).exists())

    def test_extract_structured_document_skips_layout_provider_when_disabled_for_digital_pdf(self) -> None:
        image = Image.new("RGB", (64, 32), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        profile = DocumentProfile(
            doc_id="doc",
            source_path=Path("/tmp/doc.pdf"),
            source_kind=SourceKind.PDF,
            mime_type="application/pdf",
            pdf_content_type=PDFContentType.DIGITAL,
            page_count=1,
            text_extractable=True,
            metadata={"page_modes": [PDFContentType.DIGITAL.value]},
        )
        native_blocks = [NativeTextBlock(text="native paragraph", bbox=(0, 0, 30, 10), block_no=0)]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base = PipelineSettings.from_env(project_root=root)
            settings = base.__class__(
                paths=base.paths,
                pipeline_version=base.pipeline_version,
                rendering=base.rendering,
                ocr=base.ocr,
                layout=base.layout.__class__(provider=base.layout.provider, enabled=False),
                formula=base.formula,
                embedding=base.embedding,
                gpu=base.gpu,
                chunking=base.chunking,
                storage=base.storage,
                equations=base.equations,
                ollama=base.ollama,
                runtime=base.runtime,
            )
            with patch("pocket_specialist.phases.extract.classify_document", return_value=profile),                  patch("pocket_specialist.phases.extract.get_settings", return_value=settings),                  patch("pocket_specialist.phases.extract.render_pdf_page_to_bytes", return_value=buffer.getvalue()),                  patch("pocket_specialist.phases.extract.get_pdf_native_blocks", return_value=native_blocks),                  patch("pocket_specialist.phases.extract.build_layout_provider", side_effect=AssertionError("layout should be disabled")),                  patch("pocket_specialist.phases.extract.init_db"),                  patch("pocket_specialist.phases.extract.set_status"),                  patch("pocket_specialist.phases.extract.should_process", return_value=True):
                done, failed, cif = extract_structured_document(
                    Path("/tmp/doc.pdf"),
                    structured_output_dir=root / "structured-out",
                    layout_output_dir=root / "layout-out",
                )

        self.assertEqual(done, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(cif.blocks[0].content["text"], "native paragraph")
        self.assertEqual(cif.metadata["layout_enabled"], False)

    def test_extract_structured_document_uses_full_page_ocr_when_layout_disabled_for_scanned_pdf(self) -> None:
        image = Image.new("RGB", (64, 32), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        profile = DocumentProfile(
            doc_id="doc",
            source_path=Path("/tmp/doc.pdf"),
            source_kind=SourceKind.PDF,
            mime_type="application/pdf",
            pdf_content_type=PDFContentType.SCANNED,
            page_count=1,
            text_extractable=False,
            metadata={"page_modes": [PDFContentType.SCANNED.value]},
        )

        class PageOCRProvider(FakeOCRProvider):
            def extract(self, image_bytes: bytes, region_type: str):
                self.last_region_type = region_type
                return SimpleNamespace(
                    provider=self.name,
                    confidence=0.72,
                    typed_content={"blocks": [{"raw_text": self.text, "bbox": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}, "confidence": 1.0, "block_type": "text"}]},
                    extraction_metadata={"mode": "PAGE_STRUCTURED"},
                )

        provider = PageOCRProvider("scanned page text")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            structured_dir = root / "structured-out"
            base = PipelineSettings.from_env(project_root=root)
            settings = base.__class__(
                paths=base.paths,
                pipeline_version=base.pipeline_version,
                rendering=base.rendering,
                ocr=base.ocr,
                layout=base.layout.__class__(provider=base.layout.provider, enabled=False),
                formula=base.formula,
                embedding=base.embedding,
                gpu=base.gpu,
                chunking=base.chunking,
                storage=base.storage,
                equations=base.equations,
                ollama=base.ollama,
                runtime=base.runtime,
            )
            with patch("pocket_specialist.phases.extract.classify_document", return_value=profile),                  patch("pocket_specialist.phases.extract.get_settings", return_value=settings),                  patch("pocket_specialist.phases.extract.render_pdf_page_to_bytes", return_value=buffer.getvalue()),                  patch("pocket_specialist.phases.extract.get_pdf_native_blocks", return_value=[]),                  patch("pocket_specialist.phases.extract.build_layout_provider", side_effect=AssertionError("layout should be disabled")),                  patch("pocket_specialist.phases.extract.build_primary_ocr_provider", return_value=provider),                  patch("pocket_specialist.phases.extract.build_fallback_ocr_provider", return_value=None),                  patch("pocket_specialist.phases.extract.init_db"),                  patch("pocket_specialist.phases.extract.set_status"),                  patch("pocket_specialist.phases.extract.should_process", return_value=True):
                done, failed, cif = extract_structured_document(
                    Path("/tmp/doc.pdf"),
                    structured_output_dir=structured_dir,
                    layout_output_dir=root / "layout-out",
                )

            page_payload = json.loads((structured_dir / "page_0001.json").read_text(encoding="utf-8"))

        self.assertEqual(done, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(provider.last_region_type, "page")
        self.assertEqual(cif.blocks[0].content["text"], "scanned page text")
        self.assertEqual(cif.metadata["layout_enabled"], False)
        self.assertEqual(page_payload["type"], "PageLayout")
        self.assertEqual(page_payload["metadata"]["layout_enabled"], False)
        self.assertEqual(page_payload["blocks"][0]["content"]["text"], "scanned page text")
        self.assertEqual(page_payload["reading_order"], [page_payload["blocks"][0]["block_id"]])

    def test_extract_structured_document_claims_gpu_scheduler_for_active_phase_b_c_resources(self) -> None:
        image = Image.new("RGB", (64, 32), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[
                LayoutRegion("region-0001", "text", (0, 0, 30, 10), 0.97, 0),
                LayoutRegion("region-0002", "formula", (40, 0, 60, 20), 0.97, 1),
            ],
            layout_confidence=0.97,
        )
        profile = DocumentProfile(
            doc_id="doc",
            source_path=Path("/tmp/doc.pdf"),
            source_kind=SourceKind.PDF,
            mime_type="application/pdf",
            pdf_content_type=PDFContentType.DIGITAL,
            page_count=1,
            text_extractable=True,
            metadata={"page_modes": [PDFContentType.DIGITAL.value]},
        )
        native_blocks = [NativeTextBlock(text="native paragraph", bbox=(0, 0, 30, 10), block_no=0)]
        recorder = ClaimRecorder()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("pocket_specialist.phases.extract.classify_document", return_value=profile), \
                 patch("pocket_specialist.phases.extract.render_pdf_page_to_bytes", return_value=buffer.getvalue()), \
                 patch("pocket_specialist.phases.extract.build_layout_provider", return_value=FakeLayoutProvider(layout)), \
                 patch("pocket_specialist.phases.extract.get_pdf_native_blocks", return_value=native_blocks), \
                 patch("pocket_specialist.phases.extract.build_primary_ocr_provider", return_value=UnusedOCRProvider()), \
                 patch("pocket_specialist.phases.extract.build_fallback_ocr_provider", return_value=None), \
                 patch("pocket_specialist.phases.extract.build_formula_extractor", return_value=FakeFormulaExtractor()), \
                 patch("pocket_specialist.phases.extract.gpu_scheduler", recorder), \
                 patch("pocket_specialist.phases.extract.init_db"), \
                 patch("pocket_specialist.phases.extract.set_status"), \
                 patch("pocket_specialist.phases.extract.should_process", return_value=True):
                extract_structured_document(
                    Path("/tmp/doc.pdf"),
                    structured_output_dir=root / "structured-out",
                    layout_output_dir=root / "layout-out",
                )

        assert recorder.claims == ["layout", "layout", "ocr", "ocr"]

    def test_layout_provider_detect_claims_gpu_scheduler(self) -> None:
        class FakePredictor:
            def __call__(self, images):
                return [SimpleNamespace(bboxes=[])]

        recorder = ClaimRecorder()
        from pocket_specialist.layout.providers import SuryaLayoutProvider

        provider = SuryaLayoutProvider()
        provider._predictor = FakePredictor()

        image = Image.new("RGB", (24, 24), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        with patch("pocket_specialist.layout.providers.gpu_scheduler", recorder):
            provider.detect(buffer.getvalue())

        assert recorder.claims == ["layout"]

    def test_ollama_formula_client_claims_gpu_scheduler(self) -> None:
        session = FakeSession()
        recorder = ClaimRecorder()
        with patch("pocket_specialist.formula.providers.requests.Session", return_value=session), \
             patch("pocket_specialist.formula.providers.gpu_scheduler", recorder):
            extractor = UniMERNetFormulaExtractor(base_url="http://formula.local", model_size="small")
            extractor.load()
            extractor.extract(b"image-bytes")
            extractor.offload()

        assert recorder.claims == ["formula", "formula", "formula"]

    def test_digital_page_routes_uncovered_formula_region_through_formula_path(self) -> None:
        image = Image.new("RGB", (64, 32), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        layout = LayoutResult(
            page_id="page-0001",
            regions=[
                LayoutRegion("region-0001", "text", (0, 0, 30, 10), 0.97, 0),
                LayoutRegion("region-0002", "formula", (40, 0, 60, 20), 0.97, 1),
            ],
            layout_confidence=0.97,
        )
        profile = DocumentProfile(
            doc_id="doc",
            source_path=Path("/tmp/doc.pdf"),
            source_kind=SourceKind.PDF,
            mime_type="application/pdf",
            pdf_content_type=PDFContentType.DIGITAL,
            page_count=1,
            text_extractable=True,
            metadata={"page_modes": [PDFContentType.DIGITAL.value]},
        )
        native_blocks = [NativeTextBlock(text="native paragraph", bbox=(0, 0, 30, 10), block_no=0)]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch("pocket_specialist.phases.extract.classify_document", return_value=profile), \
                 patch("pocket_specialist.phases.extract.render_pdf_page_to_bytes", return_value=buffer.getvalue()), \
                 patch("pocket_specialist.phases.extract.build_layout_provider", return_value=FakeLayoutProvider(layout)), \
                 patch("pocket_specialist.phases.extract.get_pdf_native_blocks", return_value=native_blocks), \
                 patch("pocket_specialist.phases.extract.build_primary_ocr_provider", return_value=UnusedOCRProvider()), \
                 patch("pocket_specialist.phases.extract.build_fallback_ocr_provider", return_value=None), \
                 patch("pocket_specialist.phases.extract.build_formula_extractor", return_value=FakeFormulaExtractor()), \
                 patch("pocket_specialist.phases.extract.init_db"), \
                 patch("pocket_specialist.phases.extract.set_status"), \
                 patch("pocket_specialist.phases.extract.should_process", return_value=True):
                done, failed, cif = extract_structured_document(
                    Path("/tmp/doc.pdf"),
                    structured_output_dir=root / "structured-out",
                    layout_output_dir=root / "layout-out",
                )

        self.assertEqual(done, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(len(cif.blocks), 2)
        self.assertEqual(cif.blocks[0].block_type, "TextBlock")
        self.assertEqual(cif.blocks[1].block_type, "FormulaBlock")
        self.assertEqual(cif.blocks[1].content["type"], "FormulaBlock")
        self.assertEqual(cif.blocks[1].content["latex"], "x^2 + y^2")


if __name__ == "__main__":
    unittest.main()
