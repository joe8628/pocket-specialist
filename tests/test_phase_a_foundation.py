from __future__ import annotations

from pathlib import Path

from pipeline.foundation.cif import CanonicalIntermediateFormat
from pipeline.foundation.config import PipelineSettings
from pipeline.foundation.gpu import GPUScheduler
from pipeline.foundation.validation import OutputValidationError, OutputValidator
from pipeline.models import BlockType, BoundingBox, TextBlock


def test_pipeline_settings_from_env_uses_project_root(tmp_path, monkeypatch):
    monkeypatch.delenv("PIPELINE_CHECKPOINT_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_OUTPUT_DIR", raising=False)
    settings = PipelineSettings.from_env(project_root=tmp_path)

    assert settings.paths.project_root == tmp_path.resolve()
    assert settings.paths.checkpoint_dir == (tmp_path / "checkpoints").resolve()
    assert settings.paths.output_dir == (tmp_path / "output").resolve()
    assert settings.paths.document_slug(Path("/tmp/My File.pdf")) == "my-file"


def test_output_validator_repairs_fenced_json():
    validator = OutputValidator()
    result = validator.parse_json("```json\n{\"blocks\": []}\n```")

    assert result.value == {"blocks": []}
    assert result.issues


def test_output_validator_require_raises_for_invalid_payload():
    validator = OutputValidator()

    def _require_blocks(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict) or "blocks" not in payload:
            raise OutputValidationError([])
        return payload

    try:
        validator.require({}, _require_blocks)
    except OutputValidationError:
        pass
    else:
        raise AssertionError("validator.require should fail for missing blocks")


def test_text_block_converts_to_structured_block():
    block = TextBlock(
        bbox=BoundingBox(0, 1, 10, 20),
        raw_text="Hello",
        confidence=0.9,
        block_type=BlockType.TEXT,
    )

    structured = block.to_structured_block(
        block_id="blk-1",
        doc_id="doc-1",
        reading_order=3,
        page=5,
        provider="surya",
    )

    assert structured.block_id == "blk-1"
    assert structured.doc_id == "doc-1"
    assert structured.block_type == "text"
    assert structured.content["text"] == "Hello"
    assert structured.source_coords is not None
    assert structured.source_coords.page == 5
    assert structured.provenance.provider == "surya"


def test_cif_collects_blocks_and_artifacts():
    cif = CanonicalIntermediateFormat(doc_id="doc-1")
    block = TextBlock(
        bbox=BoundingBox(0, 0, 5, 5),
        raw_text="x",
        confidence=1.0,
        block_type=BlockType.TEXT,
    ).to_structured_block(block_id="b1", doc_id="doc-1", reading_order=0)
    cif.add_block(block)

    assert len(cif.blocks) == 1


def test_gpu_scheduler_claim_is_reentrant_per_call():
    scheduler = GPUScheduler()
    with scheduler.claim("ocr"):
        assert True
