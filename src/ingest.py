"""Embed documents and store them in ChromaDB."""
import chromadb

client = chromadb.PersistentClient(path="./chroma_db")


def ingest(documents: list[str], ids: list[str], collection_name: str = "default") -> None:
    collection = client.get_or_create_collection(collection_name)
    collection.add(documents=documents, ids=ids)
