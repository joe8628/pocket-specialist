from __future__ import annotations

import io
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import requests

from pocket_specialist.layout.providers import SuryaLayoutServiceProvider
from pocket_specialist.layout.service import SuryaLayoutRuntime


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
        self.posts: list[tuple[str, object | None]] = []
        self.closed = False

    def post(self, url: str, json: object | None = None, timeout: int | float | None = None) -> FakeResponse:
        self.posts.append((url, json))
        raise AssertionError(url)

    def close(self) -> None:
        self.closed = True


class ClaimRecorder:
    def __init__(self) -> None:
        self.claims: list[str] = []

    @contextmanager
    def claim(self, resource_type: str):
        self.claims.append(resource_type)
        yield


def test_surya_layout_service_provider_normalizes_v2_payload() -> None:
    class LayoutSession(FakeSession):
        def post(self, url: str, json: object | None = None, timeout: int | float | None = None) -> FakeResponse:
            self.posts.append((url, json))
            if url.endswith("/load") or url.endswith("/offload"):
                return FakeResponse({"ok": True})
            if url.endswith("/detect"):
                return FakeResponse(
                    {
                        "page_id": "page",
                        "layout_confidence": 0.89,
                        "regions": [
                            {
                                "region_type": "heading",
                                "bbox": [0, 0, 10, 10],
                                "confidence": 0.94,
                                "reading_order": 0,
                                "provider_label": "SectionHeader",
                                "raw_label": "SectionHeader",
                                "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
                                "count": 50,
                            }
                        ],
                    }
                )
            raise AssertionError(url)

    session = LayoutSession()
    with patch("pocket_specialist.layout.providers.requests.Session", return_value=session):
        provider = SuryaLayoutServiceProvider(base_url="http://layout.local")
        provider.load()
        result = provider.detect(b"image-bytes")
        provider.offload()

    assert result.page_id == "page"
    assert result.layout_confidence == 0.89
    assert [region.region_type for region in result.regions] == ["heading"]
    assert result.regions[0].metadata["provider"] == "surya-layout-service"
    assert result.regions[0].metadata["provider_label"] == "SectionHeader"


def test_surya_layout_service_provider_detect_times_out_after_sixty_seconds() -> None:
    class TimeoutSession(FakeSession):
        def post(self, url: str, json: object | None = None, timeout: int | float | None = None) -> FakeResponse:
            self.posts.append((url, json))
            if url.endswith("/load"):
                return FakeResponse({"ok": True})
            if url.endswith("/detect"):
                self.detect_timeout = timeout
                raise requests.Timeout("layout backend took too long")
            if url.endswith("/offload"):
                return FakeResponse({"ok": True})
            raise AssertionError(url)

    session = TimeoutSession()
    with patch("pocket_specialist.layout.providers.requests.Session", return_value=session):
        provider = SuryaLayoutServiceProvider(base_url="http://layout.local")
        provider.load()
        with patch("pocket_specialist.layout.providers._LAYOUT_SERVICE_TIMEOUT_SECONDS", 60):
            try:
                provider.detect(b"image-bytes")
            except RuntimeError as exc:
                assert "Surya layout service detect failed" in str(exc)
            else:  # pragma: no cover - defensive
                raise AssertionError("expected runtime error")

    assert session.detect_timeout == 60
    assert [post[0] for post in session.posts[:2]] == ["http://layout.local/load", "http://layout.local/detect"]


def test_surya_layout_runtime_uses_v2_manager_and_claims_gpu_scheduler() -> None:
    recorder = ClaimRecorder()
    image = Image.new("RGB", (24, 24), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    class FakeManager:
        def __init__(self) -> None:
            self.closed = False

        def shutdown(self) -> None:
            self.closed = True

    class FakePredictor:
        def __init__(self, manager) -> None:
            self.manager = manager

        def __call__(self, images):
            assert len(images) == 1
            return [
                SimpleNamespace(
                    error=False,
                    bboxes=[
                        SimpleNamespace(
                            bbox=[1, 2, 10, 12],
                            label="SectionHeader",
                            raw_label="SectionHeader",
                            position=0,
                            confidence=0.91,
                            count=50,
                            polygon=[[1, 2], [10, 2], [10, 12], [1, 12]],
                        )
                    ],
                )
            ]

    with patch.dict(
        "sys.modules",
        {
            "surya.inference": SimpleNamespace(SuryaInferenceManager=FakeManager),
            "surya.layout": SimpleNamespace(LayoutPredictor=FakePredictor),
        },
    ), patch("pocket_specialist.layout.service.gpu_scheduler", recorder):
        runtime = SuryaLayoutRuntime()
        runtime.load()
        result = runtime.detect(buffer.getvalue())
        manager = runtime._manager
        runtime.offload()

    assert recorder.claims == ["layout", "layout"]
    assert isinstance(manager, FakeManager)
    assert manager.closed is True
    assert result.regions[0].region_type == "heading"
    assert result.regions[0].provider_label == "SectionHeader"


def test_surya_layout_runtime_import_failures_raise_runtime_errors() -> None:
    with patch.dict(
        "sys.modules",
        {
            "surya": SimpleNamespace(),
            "surya.inference": None,
            "surya.layout": None,
            "surya.settings": None,
        },
    ):
        with patch("pocket_specialist.layout.service._default_surya_backend", return_value="vllm"):
            try:
                SuryaLayoutRuntime().load()
            except RuntimeError as exc:
                assert "Surya is not installed in the layout service environment" in str(exc)
            else:  # pragma: no cover - defensive
                raise AssertionError("expected runtime error")


def test_surya_layout_runtime_offload_stops_spawned_vllm_container() -> None:
    stopped = {"manager": False, "docker": None}

    class FakeHandle:
        base_url = "http://127.0.0.1:57275/v1"
        spawned_by_us = True

    class FakeBackend:
        name = "vllm"

        def __init__(self) -> None:
            self.handle = FakeHandle()

    class FakeManager:
        def __init__(self) -> None:
            self.backend = FakeBackend()

        def stop(self) -> None:
            stopped["manager"] = True

    runtime = SuryaLayoutRuntime()
    runtime._predictor = object()
    runtime._manager = FakeManager()

    def fake_run(cmd, check=False, capture_output=False, timeout=None):
        stopped["docker"] = cmd
        class Result:
            returncode = 0
        return Result()

    with patch("pocket_specialist.layout.service.shutil.which", return_value="/usr/bin/docker"), \
         patch("pocket_specialist.layout.service.subprocess.run", side_effect=fake_run):
        runtime.offload()

    assert runtime._manager is None
    assert runtime._predictor is None
    assert stopped["manager"] is True
    assert stopped["docker"] == ["/usr/bin/docker", "stop", "surya-vllm-57275"]
