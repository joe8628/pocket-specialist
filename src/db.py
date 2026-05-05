"""Shared ChromaDB client and embedding function — imported by ingest and search."""
from pathlib import Path
import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / "chroma_db"

EMBEDDING_MODEL = "nomic-embed-text-v1.5"

embedding_fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
client = chromadb.PersistentClient(path=str(DB_PATH))
