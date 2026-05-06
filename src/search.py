"""Semantic search over the ChromaDB collection, with optional graph expansion."""
import sys
import argparse
from db import client, embedding_fn
import graph as kg


def search(query: str, n_results: int = 5, collection_name: str = "default") -> list[str]:
    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    results = collection.query(query_texts=[query], n_results=n_results)
    docs = results["documents"]
    return docs[0] if docs else []


def search_graph(query: str, n_results: int = 5, hops: int = 2, collection_name: str = "default") -> list[str]:
    """Expand query via knowledge graph before searching ChromaDB."""
    related = kg.expand(query, hops=hops)
    expanded = query + " " + " ".join(related) if related else query
    return search(expanded, n_results=n_results, collection_name=collection_name)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search the RAG corpus.")
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "--mode",
        choices=["vector", "graph", "map"],
        default="vector",
        help="vector: direct similarity (default) | graph: concept-expanded search | map: show related concepts",
    )
    parser.add_argument("--hops", type=int, default=2, help="Graph traversal depth (default: 2)")
    parser.add_argument("--n", type=int, default=5, help="Number of results (default: 5)")
    args = parser.parse_args()

    if args.mode == "map":
        cmap = kg.concept_map(args.query, hops=args.hops)
        if not cmap:
            print("No concepts found. Run ingestion first.")
            sys.exit(0)
        for concept, neighbours in cmap.items():
            print(f"\n{concept}")
            for n in neighbours:
                print(f"  → {n}")
        sys.exit(0)

    try:
        if args.mode == "graph":
            related = kg.expand(args.query, hops=args.hops)
            if related:
                print(f"Graph expansion: {', '.join(related[:5])}\n")
            hits = search_graph(args.query, n_results=args.n, hops=args.hops)
        else:
            hits = search(args.query, n_results=args.n)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not hits:
        print("No results found.")
        sys.exit(0)

    for i, doc in enumerate(hits, 1):
        print(f"[{i}] {doc}")
