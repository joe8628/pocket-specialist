"""Shared ChromaDB client and embedding function — imported by ingest and search."""
import os
from pathlib import Path
from typing import cast
import chromadb
from chromadb.api.types import EmbeddingFunction, Embeddable
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

# cast: SentenceTransformerEmbeddingFunction is EmbeddingFunction[Documents] but
# get_or_create_collection expects EmbeddingFunction[Embeddable]; the contravariant
# type parameter makes the narrower type fail statically even though it works at runtime.
embedding_fn: EmbeddingFunction[Embeddable] = cast(
    EmbeddingFunction[Embeddable],
    SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL),
)
client = chromadb.PersistentClient(path=str(DB_PATH))
