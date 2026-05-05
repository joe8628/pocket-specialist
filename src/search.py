"""Semantic search over the ChromaDB collection."""
import sys
from db import client, embedding_fn


def search(query: str, n_results: int = 5, collection_name: str = "default") -> list[str]:
    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    results = collection.query(query_texts=[query], n_results=n_results)
    docs = results["documents"]
    return docs[0] if docs else []


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/search.py \"<query>\"")
        sys.exit(1)
    query = sys.argv[1]
    try:
        hits = search(query)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    if not hits:
        print("No results found.")
        sys.exit(0)
    for i, doc in enumerate(hits, 1):
        print(f"[{i}] {doc}")
