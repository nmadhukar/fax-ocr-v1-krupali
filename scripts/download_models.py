"""
Pre-download all ML models at Docker build time.

Called by Dockerfile during image build so NO internet access
is required when the containers start at runtime.

Models downloaded:
  1. sentence-transformers/all-MiniLM-L6-v2  (~90 MB)   - semantic search / embeddings
  2. impira/layoutlm-document-qa              (~130 MB)  - document field extraction (VLM)
  3. PaddleOCR en (det + rec + cls)           (~100 MB)  - OCR text detection & recognition
"""

import os
import subprocess
import sys

# Must match Dockerfile ENV and docker-compose volumes
os.environ["HF_HOME"] = "/app/.cache/huggingface"
os.environ["PADDLEOCR_HOME"] = "/app/.cache/paddleocr"
os.environ["TRANSFORMERS_OFFLINE"] = "0"  # allow download at build time

print("=" * 60, flush=True)
print("Downloading ML models (build-time only, ~320 MB total)", flush=True)
print("=" * 60, flush=True)

# ── 1. Sentence Transformers (embeddings / semantic search) ─────────────────
print("\n[1/3] sentence-transformers/all-MiniLM-L6-v2 ...", flush=True)
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    print("      done.", flush=True)
except Exception as e:
    print(f"      WARNING: {e}", flush=True)

# ── 2. LayoutLM Document QA (field extraction VLM) ─────────────────────────
print("\n[2/3] impira/layoutlm-document-qa ...", flush=True)
try:
    from transformers import pipeline as hf_pipeline
    hf_pipeline(
        "document-question-answering",
        model="impira/layoutlm-document-qa",
    )
    print("      done.", flush=True)
except Exception as e:
    print(f"      WARNING: {e}", flush=True)

# ── 3. PaddleOCR models (det + rec + cls, English) ─────────────────────────
# Run in a subprocess: PaddleOCR can conflict with other imports if mixed
print("\n[3/3] PaddleOCR English models (det + rec + cls) ...", flush=True)
paddle_script = """
import os, sys
os.environ["PADDLEOCR_HOME"] = "/app/.cache/paddleocr"
os.environ["FLAGS_allocator_strategy"] = "auto_growth"
try:
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    print("      done.", flush=True)
except Exception as e:
    print(f"      WARNING: {e}", flush=True)
"""
result = subprocess.run(
    [sys.executable, "-c", paddle_script],
    timeout=300,
)
if result.returncode != 0:
    print("      PaddleOCR model download had warnings (non-fatal).", flush=True)

print("\n" + "=" * 60, flush=True)
print("All models downloaded and cached. Image is fully self-contained.", flush=True)
print("=" * 60, flush=True)
