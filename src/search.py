"""Semantic search over the ChromaDB collection."""
import chromadb

client = chromadb.PersistentClient(path="./chroma_db")


def search(query: str, n_results: int = 5, collection_name: str = "default") -> list[str]:
    collection = client.get_or_create_collection(collection_name)
    results = collection.query(query_texts=[query], n_results=n_results)
    docs = results["documents"]
    return docs[0] if docs else []
