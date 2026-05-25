from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from PIL import Image

from pocket_specialist.core.config import PipelineSettings
from pocket_specialist.phases.extract import extract_structured_document
from pocket_specialist.handlers.intake import (
    PDFContentType,
    SourceKind,
    classify_document,
    detect_mime_type,
    ingest_docx_to_cif,
    ingest_epub_to_cif,
    ingest_html_to_cif,
    ingest_tabular_to_cif,
    ingest_xlsx_to_cif,
)


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

    def test_classify_document_detects_csv_docx_xlsx_epub_and_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            csv_path = root / "sample.csv"
            csv_path.write_text("name,value\na,1\n", encoding="utf-8")
            self.assertEqual(classify_document(csv_path).source_kind, SourceKind.CSV)

            image_path = root / "scan.png"
            Image.new("RGB", (12, 12), "white").save(image_path)
            self.assertEqual(classify_document(image_path).source_kind, SourceKind.IMAGE)

            docx_path = root / "sample.docx"
            with zipfile.ZipFile(docx_path, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    """<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body></w:document>""",
                )
            self.assertEqual(classify_document(docx_path).source_kind, SourceKind.DOCX)

            xlsx_path = root / "sample.xlsx"
            with zipfile.ZipFile(xlsx_path, "w") as archive:
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    """<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData><row r='1'><c r='A1' t='inlineStr'><is><t>h1</t></is></c><c r='B1' t='inlineStr'><is><t>h2</t></is></c></row><row r='2'><c r='A2' t='inlineStr'><is><t>v1</t></is></c><c r='B2' t='inlineStr'><is><t>v2</t></is></c></row></sheetData></worksheet>""",
                )
            self.assertEqual(classify_document(xlsx_path).source_kind, SourceKind.XLSX)

            epub_path = root / "sample.epub"
            with zipfile.ZipFile(epub_path, "w") as archive:
                archive.writestr("META-INF/container.xml", """<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>""")
                archive.writestr("OEBPS/content.opf", """<package xmlns='http://www.idpf.org/2007/opf'><manifest><item id='c1' href='chapter1.xhtml' media-type='application/xhtml+xml'/></manifest><spine><itemref idref='c1'/></spine></package>""")
                archive.writestr("OEBPS/chapter1.xhtml", "<html><body><h1>Chapter</h1><p>Hello EPUB</p></body></html>")
            self.assertEqual(classify_document(epub_path).source_kind, SourceKind.EPUB)

    def test_ingest_tabular_docx_xlsx_and_epub_to_cif(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            csv_path = root / "sample.csv"
            csv_path.write_text("name,value\na,1\n", encoding="utf-8")
            csv_cif = ingest_tabular_to_cif(csv_path)
            self.assertEqual(csv_cif.blocks[0].block_type, "TableBlock")
            self.assertEqual(csv_cif.blocks[0].content["headers"], ["name", "value"])
            self.assertEqual(csv_cif.blocks[0].content["rows"], [{"name": "a", "value": "1"}])
            self.assertEqual(csv_cif.blocks[0].content["schema"], {"columns": ["name", "value"], "row_count": 1})

            docx_path = root / "sample.docx"
            with zipfile.ZipFile(docx_path, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    """<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body></w:document>""",
                )
            docx_cif = ingest_docx_to_cif(docx_path)
            self.assertEqual(docx_cif.blocks[0].content["text"], "Hello DOCX")

            xlsx_path = root / "sample.xlsx"
            with zipfile.ZipFile(xlsx_path, "w") as archive:
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    """<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData><row r='1'><c r='A1' t='inlineStr'><is><t>h1</t></is></c><c r='B1' t='inlineStr'><is><t>h2</t></is></c></row><row r='2'><c r='A2' t='inlineStr'><is><t>v1</t></is></c><c r='B2' t='inlineStr'><is><t>v2</t></is></c></row></sheetData></worksheet>""",
                )
            xlsx_cif = ingest_xlsx_to_cif(xlsx_path)
            self.assertEqual(xlsx_cif.blocks[0].content["headers"], ["h1", "h2"])
            self.assertEqual(xlsx_cif.blocks[0].content["rows"], [{"h1": "v1", "h2": "v2"}])
            self.assertEqual(xlsx_cif.blocks[0].content["sheet_name"], "sheet1")

            epub_path = root / "sample.epub"
            with zipfile.ZipFile(epub_path, "w") as archive:
                archive.writestr("META-INF/container.xml", """<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles></container>""")
                archive.writestr("OEBPS/content.opf", """<package xmlns='http://www.idpf.org/2007/opf'><manifest><item id='c1' href='chapter1.xhtml' media-type='application/xhtml+xml'/></manifest><spine><itemref idref='c1'/></spine></package>""")
                archive.writestr("OEBPS/chapter1.xhtml", "<html><body><h1>Chapter</h1><p>Hello EPUB</p></body></html>")
            epub_cif = ingest_epub_to_cif(epub_path)
            self.assertEqual(epub_cif.blocks[0].content["text"], "Chapter")
            self.assertEqual(epub_cif.blocks[1].content["text"], "Hello EPUB")

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

    def test_extract_structured_document_for_image_uses_page_ocr(self) -> None:
        class FakeOCRProvider:
            name = "fake-ocr"

            def load(self) -> None:
                return None

            def offload(self) -> None:
                return None

            def extract(self, image_bytes: bytes, region_type: str):
                del image_bytes, region_type
                return SimpleNamespace(
                    provider=self.name,
                    confidence=0.91,
                    typed_content={
                        "blocks": [
                            {"raw_text": "image text", "bbox": {"x0": 0, "y0": 0, "x1": 5, "y1": 5}, "confidence": 1.0, "block_type": "text"}
                        ]
                    },
                    extraction_metadata={"mode": "PAGE_STRUCTURED"},
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_path = root / "scan.png"
            structured_dir = root / "structured"
            Image.new("RGB", (12, 12), "white").save(image_path)
            with patch("pocket_specialist.phases.extract.build_primary_ocr_provider", return_value=FakeOCRProvider()),                  patch("pocket_specialist.phases.extract.build_fallback_ocr_provider", return_value=None),                  patch("pocket_specialist.phases.extract.init_db"),                  patch("pocket_specialist.phases.extract.set_status"):
                done, failed, cif = extract_structured_document(image_path, structured_output_dir=structured_dir, layout_output_dir=root / "layout")

            page_payload = json.loads((structured_dir / "page_0001.json").read_text(encoding="utf-8"))

        self.assertEqual(done, 1)
        self.assertEqual(failed, 0)
        self.assertEqual(cif.blocks[0].content["text"], "image text")
        self.assertEqual(cif.metadata["source_kind"], "image")
        self.assertEqual(page_payload["type"], "PageLayout")
        self.assertEqual(page_payload["blocks"][0]["content"]["text"], "image text")
        self.assertEqual(page_payload["reading_order"], [page_payload["blocks"][0]["block_id"]])


if __name__ == "__main__":
    unittest.main()
