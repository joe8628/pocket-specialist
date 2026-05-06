"""Shared ChromaDB client and embedding function — imported by ingest and search."""
import os
from pathlib import Path
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "chroma_db"

EMBEDDING_MODEL = "BAAI/bge-large-en-v1.5"


def _model_is_cached(model_name: str) -> bool:
    cache_root = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    folder = "models--" + model_name.replace("/", "--")
    return (cache_root / folder).exists()


if not _model_is_cached(EMBEDDING_MODEL):
    print(f"Embedding model '{EMBEDDING_MODEL}' is not downloaded.")
    print(f"To download it manually run:")
    print(f"  python -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('{EMBEDDING_MODEL}')\"")
    print("Downloading now...\n")

embedding_fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
client = chromadb.PersistentClient(path=str(DB_PATH))
