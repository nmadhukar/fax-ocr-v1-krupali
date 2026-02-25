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

_failures: list[str] = []
_warnings: list[str] = []

# ── 1. Sentence Transformers (embeddings / semantic search) ─────────────────
print("\n[1/3] sentence-transformers/all-MiniLM-L6-v2 ...", flush=True)
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    print("      done.", flush=True)
except Exception as e:
    print(f"      FAILED: {e}", flush=True)
    _failures.append("sentence-transformers")

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
    print(f"      FAILED: {e}", flush=True)
    _failures.append("layoutlm-document-qa")

# ── 3. PaddleOCR models (det + rec + cls, English) ─────────────────────────
# Run in a subprocess: PaddleOCR can conflict with other imports if mixed
print("\n[3/3] PaddleOCR English models (det + rec + cls) ...", flush=True)
paddle_script = """
import os, sys
os.environ["PADDLEOCR_HOME"] = "/app/.cache/paddleocr"
os.environ["FLAGS_allocator_strategy"] = "auto_growth"
from paddleocr import PaddleOCR
ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
print("      done.", flush=True)
"""
result = subprocess.run(
    [sys.executable, "-c", paddle_script],
    timeout=300,
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    # Some CPU environments cannot initialize Paddle (SIGSEGV / illegal instruction).
    # In that case we keep build working and allow runtime to use pre-baked models
    # when compatible hardware is available.
    if result.returncode in (-11, -4):
        _warnings.append("paddleocr")
        print(
            "      WARN: PaddleOCR initialization crashed in this build environment "
            f"(returncode={result.returncode}). Skipping Paddle pre-download.",
            flush=True,
        )
    else:
        if result.stdout:
            print(result.stdout, flush=True)
        if result.stderr:
            print(result.stderr, flush=True)
        print(
            f"      FAILED: PaddleOCR model download returned {result.returncode}.",
            flush=True,
        )
        _failures.append("paddleocr")

print("\n" + "=" * 60, flush=True)
if _failures:
    print(f"FAILED to download: {', '.join(_failures)}", flush=True)
    print("Docker build should NOT continue with missing models.", flush=True)
    print("=" * 60, flush=True)
    sys.exit(1)
else:
    if _warnings:
        print(
            "Model pre-download completed with warnings: "
            f"{', '.join(_warnings)}.",
            flush=True,
        )
        print(
            "Image build succeeded, but some models may download at runtime "
            "when compatible hardware is available.",
            flush=True,
        )
    else:
        print("All models downloaded and cached. Image is fully self-contained.", flush=True)
    print("=" * 60, flush=True)
