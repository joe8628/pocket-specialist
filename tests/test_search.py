"""Basic smoke tests for ingest and search — no API required."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.ingest import ingest
from src.search import search


def test_ingest_and_search():
    ingest(
        documents=["The sky is blue.", "The grass is green.", "The ocean is deep."],
        ids=["1", "2", "3"],
        collection_name="test",
    )
    results = search("color of the sky", n_results=1, collection_name="test")
    assert len(results) == 1
    assert "sky" in results[0].lower()


if __name__ == "__main__":
    test_ingest_and_search()
    print("All tests passed.")
