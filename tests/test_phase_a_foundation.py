from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

from pocket_specialist.core.cif import CanonicalIntermediateFormat
from pocket_specialist.core.config import PipelineSettings
from pocket_specialist.core.dag import DAGExecutor, TaskRunResult
from pocket_specialist.core.gpu import GPUScheduler
from pocket_specialist.core.tasks import ExtractionTask, PageUnit
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


def test_dag_executor_retries_failed_task_and_then_runs_dependent(tmp_path):
    from pocket_specialist.storage import checkpoint

    layout_task = ExtractionTask(task_id="doc:layout:0001", task_type="layout", resource_requirements={"resource": "layout"}, retry_count=2)
    structured_task = ExtractionTask(task_id="doc:structured:0001", task_type="structured", dependencies=[layout_task.task_id])
    units = {
        layout_task.task_id: PageUnit(unit_id="doc:page:0001", doc_id="doc", metadata={"page": 1}),
        structured_task.task_id: PageUnit(unit_id="doc:page:0001", doc_id="doc", metadata={"page": 1}),
    }
    attempts = {layout_task.task_id: 0, structured_task.task_id: 0}

    def runner(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del unit
        attempts[task.task_id] += 1
        if task.task_id == layout_task.task_id and attempts[task.task_id] == 1:
            raise RuntimeError("transient failure")
        return TaskRunResult(path=f"/tmp/{task.task_id}.json")

    with patch.object(checkpoint, "DB_PATH", tmp_path / "pipeline.db"):
        checkpoint.init_db()
        result = DAGExecutor(max_workers=2, max_retries=2).run(document="doc", tasks=[layout_task, structured_task], units=units, runner=runner)
        layout_state = checkpoint.get_node_state("doc", 1, layout_task.task_id)
        structured_state = checkpoint.get_node_state("doc", 1, structured_task.task_id)

    assert attempts[layout_task.task_id] == 2
    assert attempts[structured_task.task_id] == 1
    assert result.failed_task_ids == []
    assert result.blocked_task_ids == []
    assert set(result.completed_task_ids) == {layout_task.task_id, structured_task.task_id}
    assert layout_state is not None and layout_state.status == "done" and layout_state.attempts == 2
    assert structured_state is not None and structured_state.status == "done"


def test_dag_executor_skips_checkpointed_task_and_still_runs_dependent(tmp_path):
    from pocket_specialist.storage import checkpoint

    layout_path = tmp_path / "layout.json"
    layout_path.write_text("{}", encoding="utf-8")
    layout_task = ExtractionTask(task_id="doc:layout:0001", task_type="layout", resource_requirements={"resource": "layout"})
    structured_task = ExtractionTask(task_id="doc:structured:0001", task_type="structured", dependencies=[layout_task.task_id])
    units = {
        layout_task.task_id: PageUnit(unit_id="doc:page:0001", doc_id="doc", metadata={"page": 1}),
        structured_task.task_id: PageUnit(unit_id="doc:page:0001", doc_id="doc", metadata={"page": 1}),
    }
    seen: list[str] = []

    def runner(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del unit
        seen.append(task.task_id)
        return TaskRunResult(path=f"/tmp/{task.task_id}.json")

    with patch.object(checkpoint, "DB_PATH", tmp_path / "pipeline.db"):
        checkpoint.init_db()
        checkpoint.record_node_done("doc", 1, layout_task.task_id, path=str(layout_path), metadata={"task_type": "layout"})
        result = DAGExecutor(max_workers=2, max_retries=2).run(document="doc", tasks=[layout_task, structured_task], units=units, runner=runner)

    assert seen == [structured_task.task_id]
    assert result.skipped_task_ids == [layout_task.task_id]
    assert result.completed_task_ids == [structured_task.task_id]


def test_dag_executor_reruns_done_checkpoint_when_artifact_is_missing(tmp_path):
    from pocket_specialist.storage import checkpoint

    task = ExtractionTask(task_id="doc:structured:0001", task_type="structured")
    units = {task.task_id: PageUnit(unit_id="doc:page:0001", doc_id="doc", metadata={"page": 1})}
    seen: list[str] = []
    output_path = tmp_path / "fresh.json"

    def runner(current: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del unit
        seen.append(current.task_id)
        output_path.write_text("{}", encoding="utf-8")
        return TaskRunResult(path=str(output_path))

    with patch.object(checkpoint, "DB_PATH", tmp_path / "pipeline.db"):
        checkpoint.init_db()
        checkpoint.record_node_done("doc", 1, task.task_id, path=str(tmp_path / "missing.json"), metadata={"task_type": "structured"})
        result = DAGExecutor(max_workers=1, max_retries=1).run(document="doc", tasks=[task], units=units, runner=runner)
        state = checkpoint.get_node_state("doc", 1, task.task_id)

    assert seen == [task.task_id]
    assert result.completed_task_ids == [task.task_id]
    assert result.skipped_task_ids == []
    assert state is not None and state.path == str(output_path)


def test_dag_executor_reruns_done_checkpoint_when_resume_handler_rejects_it(tmp_path):
    from pocket_specialist.storage import checkpoint

    stale_path = tmp_path / "stale.json"
    stale_path.write_text("{}", encoding="utf-8")
    fresh_path = tmp_path / "fresh.json"
    task = ExtractionTask(task_id="doc:structured:0001", task_type="structured")
    units = {task.task_id: PageUnit(unit_id="doc:page:0001", doc_id="doc", metadata={"page": 1})}
    seen: list[str] = []

    def runner(current: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del unit
        seen.append(current.task_id)
        fresh_path.write_text("{}", encoding="utf-8")
        return TaskRunResult(path=str(fresh_path))

    with patch.object(checkpoint, "DB_PATH", tmp_path / "pipeline.db"):
        checkpoint.init_db()
        checkpoint.record_node_done("doc", 1, task.task_id, path=str(stale_path), metadata={"task_type": "structured"})
        result = DAGExecutor(max_workers=1, max_retries=1).run(
            document="doc",
            tasks=[task],
            units=units,
            runner=runner,
            resume=lambda current, unit, state: False,
        )

    assert seen == [task.task_id]
    assert result.completed_task_ids == [task.task_id]
    assert result.skipped_task_ids == []


def test_dag_executor_allows_cpu_parallelism_and_serializes_layout_resource(tmp_path):
    from pocket_specialist.storage import checkpoint

    tasks: list[ExtractionTask] = []
    units: dict[str, PageUnit] = {}
    for idx in range(1, 3):
        cpu_task = ExtractionTask(task_id=f"doc:cpu:{idx:04d}", task_type="cpu", resource_requirements={"resource": "cpu"})
        layout_task = ExtractionTask(task_id=f"doc:layout:{idx:04d}", task_type="layout", resource_requirements={"resource": "layout"})
        tasks.extend([cpu_task, layout_task])
        units[cpu_task.task_id] = PageUnit(unit_id=f"doc:page:{idx:04d}", doc_id="doc", metadata={"page": idx})
        units[layout_task.task_id] = PageUnit(unit_id=f"doc:page:{idx:04d}", doc_id="doc", metadata={"page": idx})

    active = {"cpu": 0, "layout": 0}
    max_seen = {"cpu": 0, "layout": 0}
    lock = threading.Lock()

    def runner(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del unit
        resource = str(task.resource_requirements["resource"])
        with lock:
            active[resource] += 1
            max_seen[resource] = max(max_seen[resource], active[resource])
        time.sleep(0.05)
        with lock:
            active[resource] -= 1
        return TaskRunResult()

    with patch.object(checkpoint, "DB_PATH", tmp_path / "pipeline.db"):
        checkpoint.init_db()
        result = DAGExecutor(max_workers=4, max_retries=1).run(document="doc", tasks=tasks, units=units, runner=runner)

    assert len(result.completed_task_ids) == 4
    assert max_seen["cpu"] >= 2
    assert max_seen["layout"] == 1
