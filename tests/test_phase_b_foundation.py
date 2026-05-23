from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import fitz

from pocket_specialist.core.config import PipelineSettings
from pocket_specialist.phases.extract import extract_structured_document
from pocket_specialist.handlers.intake import PDFContentType, SourceKind, classify_document, detect_mime_type, ingest_html_to_cif


class PhaseBFoundationTests(unittest.TestCase):
    def test_pipeline_settings_reads_pipeline_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pipeline.toml").write_text(
                """
[pipeline]
version = "0.5.1-beta"

[ocr]
provider = "glm-ocr"
fallback_provider = "deepseek-ocr"
batch_size = 7

[layout]
provider = "pp-doclayout-v3"

[storage]
sqlite_path = "./data/spec.db"
artifact_path = "./data/artifacts"
chroma_path = "./data/chroma"
                """.strip(),
                encoding="utf-8",
            )

            settings = PipelineSettings.from_env(project_root=root)

            self.assertEqual(settings.pipeline_version, "0.5.1-beta")
            self.assertEqual(settings.ocr.provider, "glm-ocr")
            self.assertEqual(settings.ocr.fallback_provider, "deepseek-ocr")
            self.assertEqual(settings.ocr.batch_size, 7)
            self.assertEqual(settings.layout.provider, "pp-doclayout-v3")
            self.assertEqual(settings.paths.database_path, (root / "data/spec.db").resolve())

    def test_detect_mime_type_uses_content_not_only_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_html = Path(tmpdir) / "sample.txt"
            fake_html.write_text("<html><body><p>Hello</p></body></html>", encoding="utf-8")
            self.assertEqual(detect_mime_type(fake_html), "text/html")

    def test_classify_document_detects_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.html"
            path.write_text("<html><body><h1>Title</h1><p>Hello</p></body></html>", encoding="utf-8")

            profile = classify_document(path)

            self.assertEqual(profile.source_kind, SourceKind.HTML)
            self.assertTrue(profile.text_extractable)
            self.assertEqual(profile.page_count, 1)

    def test_ingest_html_to_cif_extracts_semantic_blocks_and_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.html"
            path.write_text(
                "<html><body><h1>Title</h1><p>Paragraph text.</p><img src='fig.png' alt='diagram'><ul><li>Item one</li></ul></body></html>",
                encoding="utf-8",
            )

            cif = ingest_html_to_cif(path)

            self.assertEqual(cif.doc_id, "sample")
            self.assertGreaterEqual(len(cif.blocks), 4)
            self.assertEqual(cif.blocks[0].block_type, "TextBlock")
            self.assertEqual(cif.blocks[0].content["heading_level"], 1)
            self.assertEqual(cif.blocks[1].content["text"], "Paragraph text.")
            self.assertEqual(cif.blocks[2].block_type, "FigureBlock")
            self.assertEqual(cif.blocks[2].content["alt_text"], "diagram")
            self.assertEqual(cif.blocks[3].block_type, "ListBlock")

    def test_classify_document_detects_digital_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "digital.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "This is a digital PDF page with native text for classification.")
            doc.save(path)
            doc.close()

            profile = classify_document(path)

            self.assertEqual(profile.source_kind, SourceKind.PDF)
            self.assertEqual(profile.pdf_content_type, PDFContentType.DIGITAL)
            self.assertTrue(profile.text_extractable)
            self.assertEqual(profile.page_count, 1)

    def test_extract_structured_document_for_html_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "sample.html"
            source.write_text("<html><body><h1>Title</h1><p>Hello world.</p></body></html>", encoding="utf-8")
            structured_dir = root / "structured"
            layout_dir = root / "layout"

            done, failed, cif = extract_structured_document(source, structured_output_dir=structured_dir, layout_output_dir=layout_dir)

            self.assertEqual(done, 1)
            self.assertEqual(failed, 0)
            self.assertGreaterEqual(len(cif.blocks), 2)
            payload = json.loads((structured_dir / "document.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["doc_id"], "sample")
            self.assertEqual(payload["metadata"]["source_kind"], "html")


if __name__ == "__main__":
    unittest.main()
