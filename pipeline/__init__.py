"""Top-level pipeline exports for the Phase A architecture transition."""

from .enrichment import enrich_document
from .export import correct_pages
from .serialization import assemble_document

__all__ = ["assemble_document", "correct_pages", "enrich_document"]
