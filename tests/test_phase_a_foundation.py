from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from pocket_specialist.core.cif import CanonicalIntermediateFormat
from pocket_specialist.core.config import PipelineSettings
from pocket_specialist.core.gpu import GPUScheduler
from pocket_specialist.core.validation import OutputValidationError, OutputValidator
from pocket_specialist.core.models import BlockType, BoundingBox, TextBlock


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


def test_gpu_scheduler_claim_times_out_when_lock_is_held(tmp_path, monkeypatch):
    scheduler = GPUScheduler(lock_path=tmp_path / "gpu.lock")
    holder = GPUScheduler(lock_path=tmp_path / "gpu.lock")

    settings = PipelineSettings.from_env(project_root=tmp_path)
    settings = settings.__class__(
        paths=settings.paths,
        pipeline_version=settings.pipeline_version,
        rendering=settings.rendering,
        ocr=settings.ocr,
        layout=settings.layout,
        formula=settings.formula,
        embedding=settings.embedding,
        gpu=settings.gpu,
        chunking=settings.chunking,
        storage=settings.storage,
        equations=settings.equations,
        ollama=settings.ollama,
        runtime=settings.runtime.__class__(
            max_retries=settings.runtime.max_retries,
            gpu_serialization_timeout_s=0.05,
            gpu_enabled=settings.runtime.gpu_enabled,
        ),
    )
    monkeypatch.setattr("pocket_specialist.core.gpu.get_settings", lambda: settings)

    release = threading.Event()

    def hold_lock() -> None:
        with holder.claim("ocr"):
            release.wait(timeout=1)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        while not (tmp_path / "gpu.lock").exists():
            pass
        try:
            with scheduler.claim("ocr"):
                raise AssertionError("lock acquisition should have timed out")
        except TimeoutError:
            pass
    finally:
        release.set()
        thread.join(timeout=1)


async def _acquire_scheduler_once(scheduler: GPUScheduler) -> None:
    async with scheduler.acquire("ocr"):
        return None


def test_gpu_scheduler_acquire_uses_async_file_lock(tmp_path):
    scheduler = GPUScheduler(lock_path=tmp_path / "gpu.lock")
    asyncio.run(_acquire_scheduler_once(scheduler))
