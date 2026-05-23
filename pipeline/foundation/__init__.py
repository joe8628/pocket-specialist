"""Phase A foundation scaffold for the document intelligence pipeline."""

from .cif import CanonicalIntermediateFormat, ProcessingArtifact, ProvenanceRecord, SourceCoords, StructuredBlock
from .config import PipelineSettings, get_settings
from .gpu import GPUScheduler, gpu_scheduler
from .ocr import OCRProvider, OCRResult, SuryaOCRProvider
from .tasks import ExtractionTask, FormulaUnit, PageUnit, RegionUnit, RowBatchUnit, TextSectionUnit
from .validation import OutputValidationError, OutputValidator, ValidationIssue, ValidationResult

__all__ = [
    "CanonicalIntermediateFormat",
    "ExtractionTask",
    "FormulaUnit",
    "GPUScheduler",
    "OCRProvider",
    "OCRResult",
    "OutputValidationError",
    "OutputValidator",
    "PageUnit",
    "PipelineSettings",
    "ProcessingArtifact",
    "ProvenanceRecord",
    "RegionUnit",
    "RowBatchUnit",
    "SourceCoords",
    "StructuredBlock",
    "SuryaOCRProvider",
    "TextSectionUnit",
    "ValidationIssue",
    "ValidationResult",
    "get_settings",
    "gpu_scheduler",
]
