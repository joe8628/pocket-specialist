from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pocket_specialist.storage import checkpoint


class CheckpointObservationTests(unittest.TestCase):
    def test_stage_status_is_mirrored_to_dag_node_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pipeline.db"
            with patch.object(checkpoint, "DB_PATH", db_path):
                checkpoint.init_db()

                checkpoint.set_status("layout", "doc-1", 1, "done", "/tmp/layout-1.json")

                state = checkpoint.get_node_state("doc-1", 1, "layout")
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(state.status, "done")
                self.assertEqual(state.path, "/tmp/layout-1.json")
                self.assertEqual(state.attempts, 1)
                self.assertEqual(state.metadata["source_stage"], "layout")
                self.assertEqual(state.metadata["task_type"], "layout")
                self.assertEqual(state.metadata["doc_id"], "doc-1")
                self.assertEqual(state.metadata["artifact_uri"], "/tmp/layout-1.json")

    def test_resume_state_returns_first_unfinished_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pipeline.db"
            with patch.object(checkpoint, "DB_PATH", db_path):
                checkpoint.init_db()
                checkpoint.record_node_done("doc-1", 1, "structured", path="page_0001.json")
                checkpoint.record_node_done("doc-1", 2, "structured", path="page_0002.json")

                resume = checkpoint.get_resume_state("doc-1", "structured", total_pages=4)

                self.assertEqual(resume.last_done_page, 2)
                self.assertEqual(resume.next_page, 3)
                self.assertEqual(resume.done_pages, [1, 2])

    def test_retry_exhausted_failed_node_is_not_processable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pipeline.db"
            with patch.object(checkpoint, "DB_PATH", db_path), patch.object(checkpoint, "MAX_RETRIES", 2):
                checkpoint.init_db()
                checkpoint.record_node_failed("doc-1", 1, "ocr", "first failure")
                self.assertTrue(checkpoint.should_process_node("doc-1", 1, "ocr", max_retries=2))

                checkpoint.record_node_failed("doc-1", 1, "ocr", "second failure")

                self.assertFalse(checkpoint.should_process_node("doc-1", 1, "ocr", max_retries=2))
                resume = checkpoint.get_resume_state("doc-1", "ocr", total_pages=1)
                self.assertIsNone(resume.next_page)
                self.assertEqual(resume.failed_pages, [1])


if __name__ == "__main__":
    unittest.main()
