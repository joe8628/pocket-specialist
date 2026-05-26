"""Checkpoint-aware DAG task executor for extraction workloads."""

from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import threading
from typing import Callable, Protocol

from pocket_specialist.core.config import get_settings
from pocket_specialist.core.tasks import ExtractionTask, ProcessingUnit
from pocket_specialist.storage.checkpoint import get_node_state, record_node_done, record_node_failed, record_node_start, should_process_node


@dataclass(frozen=True, slots=True)
class TaskRunResult:
    path: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphExecutionResult:
    completed_task_ids: list[str] = field(default_factory=list)
    failed_task_ids: list[str] = field(default_factory=list)
    skipped_task_ids: list[str] = field(default_factory=list)
    blocked_task_ids: list[str] = field(default_factory=list)


class TaskRunner(Protocol):
    def __call__(self, task: ExtractionTask, unit: ProcessingUnit) -> TaskRunResult | None: ...


@dataclass(slots=True)
class _TaskState:
    task: ExtractionTask
    unit: ProcessingUnit
    retry_limit: int


class DAGExecutor:
    def __init__(
        self,
        *,
        max_workers: int | None = None,
        default_resource_limits: dict[str, int] | None = None,
        max_retries: int | None = None,
    ) -> None:
        settings = get_settings()
        self.max_workers = max_workers or max(1, min(32, (os.cpu_count() or 1) + 4))
        self.max_retries = settings.runtime.max_retries if max_retries is None else max_retries
        self.default_resource_limits = {"layout": 1, "ocr": 1, "formula": 1, **(default_resource_limits or {})}
        self._resource_semaphores: dict[str, threading.Semaphore] = {}
        self._resource_lock = threading.Lock()

    def run(
        self,
        *,
        document: str,
        tasks: list[ExtractionTask],
        units: dict[str, ProcessingUnit],
        runner: TaskRunner,
    ) -> GraphExecutionResult:
        states = self._validate_and_prepare(tasks, units)
        dependents = self._dependents(states)
        completed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        blocked: set[str] = set()
        unresolved: dict[str, int] = {}
        ready: deque[str] = deque()

        for task_id, state in states.items():
            checkpoint_status = self._checkpoint_status(document, state)
            if checkpoint_status == "done":
                completed.add(task_id)
                skipped.add(task_id)
            elif checkpoint_status == "failed":
                failed.add(task_id)

        for task_id, state in states.items():
            if task_id in completed or task_id in failed:
                continue
            if any(dep in failed or dep in blocked for dep in state.task.dependencies):
                blocked.add(task_id)
                continue
            unresolved_count = sum(1 for dep in state.task.dependencies if dep not in completed)
            unresolved[task_id] = unresolved_count
            if unresolved_count == 0:
                ready.append(task_id)

        in_flight: dict[Future[tuple[str, str]], str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            while ready or in_flight:
                while ready and len(in_flight) < self.max_workers:
                    task_id = ready.popleft()
                    if task_id in blocked or task_id in failed or task_id in completed:
                        continue
                    future = pool.submit(self._run_task, document, states[task_id], runner)
                    in_flight[future] = task_id

                if not in_flight:
                    break

                done_futures, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for future in done_futures:
                    task_id = in_flight.pop(future)
                    _, status = future.result()
                    if status == "done":
                        completed.add(task_id)
                    elif status == "skipped":
                        completed.add(task_id)
                        skipped.add(task_id)
                    else:
                        failed.add(task_id)
                        self._block_descendants(task_id, dependents, blocked, completed, failed)

                    for dependent in dependents.get(task_id, []):
                        if dependent in completed or dependent in failed or dependent in blocked:
                            continue
                        if any(dep in failed or dep in blocked for dep in states[dependent].task.dependencies):
                            blocked.add(dependent)
                            self._block_descendants(dependent, dependents, blocked, completed, failed)
                            continue
                        unresolved[dependent] = unresolved.get(dependent, sum(1 for dep in states[dependent].task.dependencies if dep not in completed)) - (1 if status in {"done", "skipped"} else 0)
                        if unresolved[dependent] <= 0:
                            ready.append(dependent)

        return GraphExecutionResult(
            completed_task_ids=sorted(task_id for task_id in completed if task_id not in skipped),
            failed_task_ids=sorted(failed),
            skipped_task_ids=sorted(skipped),
            blocked_task_ids=sorted(blocked),
        )

    def _validate_and_prepare(self, tasks: list[ExtractionTask], units: dict[str, ProcessingUnit]) -> dict[str, _TaskState]:
        states: dict[str, _TaskState] = {}
        for task in tasks:
            if task.task_id in states:
                raise ValueError(f"Duplicate task_id: {task.task_id}")
            if task.task_id not in units:
                raise ValueError(f"Missing processing unit for task_id: {task.task_id}")
            retry_limit = task.retry_count if task.retry_count > 0 else self.max_retries
            states[task.task_id] = _TaskState(task=task, unit=units[task.task_id], retry_limit=retry_limit)
        for task in tasks:
            for dependency in task.dependencies:
                if dependency not in states:
                    raise ValueError(f"Unknown dependency {dependency!r} for task {task.task_id!r}")
        return states

    def _dependents(self, states: dict[str, _TaskState]) -> dict[str, list[str]]:
        dependents: dict[str, list[str]] = defaultdict(list)
        for task_id, state in states.items():
            for dependency in state.task.dependencies:
                dependents[dependency].append(task_id)
        return dependents

    def _checkpoint_status(self, document: str, state: _TaskState) -> str | None:
        page = _unit_page(state.unit)
        node_state = get_node_state(document, page, state.task.task_id)
        if node_state is None:
            return None
        if node_state.status == "done":
            return "done"
        if node_state.status == "failed" and not should_process_node(document, page, state.task.task_id, max_retries=state.retry_limit):
            return "failed"
        return None

    def _run_task(self, document: str, state: _TaskState, runner: TaskRunner) -> tuple[str, str]:
        page = _unit_page(state.unit)
        metadata = {
            **state.unit.metadata,
            "doc_id": state.unit.doc_id,
            "unit_id": state.unit.unit_id,
            "task_id": state.task.task_id,
            "task_type": state.task.task_type,
            "dependencies": list(state.task.dependencies),
            "input_refs": list(state.task.input_refs),
            "resource_requirements": dict(state.task.resource_requirements),
            "source_stage": state.task.task_type,
        }

        while should_process_node(document, page, state.task.task_id, max_retries=state.retry_limit):
            record_node_start(document, page, state.task.task_id, metadata=metadata)
            try:
                with self._resource_claim(state.task):
                    result = runner(state.task, state.unit)
            except Exception as exc:
                error_text = str(exc)
                record_node_failed(document, page, state.task.task_id, error_text, metadata=metadata)
                if not should_process_node(document, page, state.task.task_id, max_retries=state.retry_limit):
                    return state.task.task_id, "failed"
                continue

            normalized = result if isinstance(result, TaskRunResult) else TaskRunResult()
            done_metadata = {**metadata, **normalized.metadata}
            record_node_done(document, page, state.task.task_id, path=normalized.path, metadata=done_metadata)
            return state.task.task_id, "done"

        checkpoint_state = get_node_state(document, page, state.task.task_id)
        if checkpoint_state is not None and checkpoint_state.status == "done":
            return state.task.task_id, "skipped"
        return state.task.task_id, "failed"

    @contextmanager
    def _resource_claim(self, task: ExtractionTask):
        resource = _resource_name(task)
        if resource is None:
            yield
            return
        semaphore = self._semaphore_for(resource, task)
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    def _semaphore_for(self, resource: str, task: ExtractionTask) -> threading.Semaphore:
        capacity_obj = task.resource_requirements.get("max_workers")
        capacity = int(capacity_obj) if isinstance(capacity_obj, int) and capacity_obj > 0 else self.default_resource_limits.get(resource, 1)
        key = f"{resource}:{capacity}"
        with self._resource_lock:
            semaphore = self._resource_semaphores.get(key)
            if semaphore is None:
                semaphore = threading.Semaphore(capacity)
                self._resource_semaphores[key] = semaphore
        return semaphore

    def _block_descendants(
        self,
        task_id: str,
        dependents: dict[str, list[str]],
        blocked: set[str],
        completed: set[str],
        failed: set[str],
    ) -> None:
        queue = deque(dependents.get(task_id, []))
        while queue:
            dependent = queue.popleft()
            if dependent in blocked or dependent in completed or dependent in failed:
                continue
            blocked.add(dependent)
            queue.extend(dependents.get(dependent, []))


def _resource_name(task: ExtractionTask) -> str | None:
    resource = task.resource_requirements.get("resource")
    if isinstance(resource, str) and resource.strip():
        return resource.strip()
    resources = task.resource_requirements.get("resources")
    if isinstance(resources, list):
        for candidate in resources:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _unit_page(unit: ProcessingUnit) -> int:
    page = unit.metadata.get("page")
    return int(page) if isinstance(page, int) else 0
