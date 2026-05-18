"""Central configuration — paths and tuneable constants for every pipeline stage."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
CORPUS_DIR    = PROJECT_ROOT.parent / "RAG-corpus"

CHECKPOINT_DIR = PROJECT_ROOT / "checkpoints"
OUTPUT_DIR     = PROJECT_ROOT / "output"
MODELS_DIR     = PROJECT_ROOT / "models"

# ── Stage 1: Render ───────────────────────────────────────────────────────────
RENDER_DIR  = CHECKPOINT_DIR / "rendered"
RENDER_ZOOM = 2.0          # 2× → ~150 DPI at standard A4/letter size

# ── Stage 2: OCR ──────────────────────────────────────────────────────────────
OCR_DIR  = CHECKPOINT_DIR / "ocr"
OCR_LANG = "en"

# ── Stage 3: Equations ────────────────────────────────────────────────────────
EQUATIONS_DIR         = CHECKPOINT_DIR / "equations"
CROPS_DIR             = CHECKPOINT_DIR / "crops"
EQUATION_CONF_THRESHOLD = 0.5

# YOLO layout detection model — download from HuggingFace before Stage 3.
# Recommended: YOLO trained on DocLayNet (has a 'formula' class).
#   huggingface-cli download nickmuchi/yolos-base-finetuned-DocLayNet \
#       --local-dir models/layout
LAYOUT_MODEL_PATH = MODELS_DIR / "layout"

# ── Stage 4: LLM Correction (Ollama) ─────────────────────────────────────────
CORRECTION_DIR   = CHECKPOINT_DIR / "corrected"
OLLAMA_MODEL     = "qwen2.5:7b"
OLLAMA_BASE      = "http://localhost:11434"

# ── Shared ────────────────────────────────────────────────────────────────────
MAX_RETRIES = 3
DB_PATH     = CHECKPOINT_DIR / "pipeline.db"
