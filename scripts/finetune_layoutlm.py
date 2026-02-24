"""
Fine-tune LayoutLM Document QA on healthcare fax training data.

Uses `impira/layoutlm-document-qa` (LayoutLMv2) as base model and fine-tunes
with LoRA for parameter-efficient training on prior-auth fields.

Fixes vs. original:
  [1] Robust answer-span matching via tokenizer character-offset mapping
      (replaces fragile raw token-list search that silently zeroed spans)
  [2] All question variants per field (3x more training signal)
  [3] Stratified sampling — underrepresented fields are oversampled to match
      the most common field's count
  [4] GT value validation — examples that fail field-type checks are skipped

Full LayoutLMv2 preprocessing (image + bounding box) is used when
  --use-full-layoutlm is set AND pytesseract is installed.
  Without it, the script uses text-only QA (fast, no tesseract dependency).

Prerequisites:
    python scripts/generate_training_data.py --source all
    python scripts/export_training_data.py --output-dir data/training
    pip install peft datasets transformers torch Pillow

Usage:
    python scripts/finetune_layoutlm.py --data-dir data/training
    python scripts/finetune_layoutlm.py --data-dir data/training --epochs 5
    python scripts/finetune_layoutlm.py --data-dir data/training --use-full-layoutlm
"""

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Use D: for model cache if available
if os.path.exists("D:\\"):
    os.environ.setdefault("HF_HOME", "D:\\.cache\\huggingface")
    os.environ.setdefault("TRANSFORMERS_CACHE", "D:\\.cache\\huggingface\\hub")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("finetune_layoutlm")

# ── question mappings (mirrors layoutlm_client.py) ────────────────────────────
# Import from the client so they stay in sync
try:
    from libs.shared.vlm.layoutlm_client import FIELD_QUESTIONS, FIELD_QUESTIONS_VARIANTS
except ImportError:
    # Fallback if run outside project
    FIELD_QUESTIONS = {
        "member_id": "What is the member ID number?",
        "prior_auth_number": "What is the authorization number?",
        "patient_name": "What is the patient name?",
        "patient_dob": "What is the date of birth?",
        "auth_effective_date": "What is the effective date or start date?",
        "auth_expiration_date": "What is the expiration date or end date?",
        "next_review_date": "What is the next review date?",
        "provider_name": "What is the provider name?",
        "provider_npi": "What is the provider NPI number?",
        "provider_phone": "What is the provider phone number?",
        "provider_fax": "What is the fax number?",
        "service_code": "What is the service code or CPT code?",
        "units_requested": "How many units were requested or approved?",
        "diagnosis_code": "What is the diagnosis code or ICD code?",
    }
    FIELD_QUESTIONS_VARIANTS = {k: [v] for k, v in FIELD_QUESTIONS.items()}

# ── GT validation patterns ────────────────────────────────────────────────────
_DATE_RE = re.compile(r"\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}")
_PHONE_RE = re.compile(r"\d[\d\s\-\.()]{7,}")
_NPI_RE = re.compile(r"\d{10}")
_AUTH_RE = re.compile(r"\d{6,}")
_CODE_RE = re.compile(r"[A-Za-z0-9]{4,}")

_FIELD_VALIDATORS = {
    "patient_dob": _DATE_RE,
    "auth_effective_date": _DATE_RE,
    "auth_expiration_date": _DATE_RE,
    "next_review_date": _DATE_RE,
    "provider_phone": _PHONE_RE,
    "provider_fax": _PHONE_RE,
    "provider_npi": _NPI_RE,
    "prior_auth_number": _AUTH_RE,
    "member_id": _CODE_RE,
    "service_code": _CODE_RE,
    "diagnosis_code": _CODE_RE,
}

_JUNK_VALUES = {"N/A", "NONE", "NULL", "UNKNOWN", "-", "NA", "TBD"}


def validate_gt_value(field_key: str, value: str) -> bool:
    """[Fix 4] Return True if the GT value looks valid for this field type."""
    if not value or value.strip().upper() in _JUNK_VALUES:
        return False
    if len(value.strip()) < 2:
        return False
    validator = _FIELD_VALIDATORS.get(field_key)
    if validator and not validator.search(value):
        logger.debug("GT validation failed: field=%s value=%r", field_key, value)
        return False
    return True


# ── span matching ─────────────────────────────────────────────────────────────

def find_answer_span_by_offset(
    offset_mapping,
    context_text: str,
    answer_text: str,
) -> tuple[int, int]:
    """
    [Fix 1] Find answer start/end token positions using character offsets.

    Uses return_offsets_mapping=True from the tokenizer instead of comparing
    raw token IDs, which fails for multi-piece subword tokens.

    Returns (start_pos, end_pos) — both inclusive, in token space.
    Returns (0, 0) if the answer cannot be aligned (example should be skipped).
    """
    # Normalise whitespace for matching
    norm_answer = " ".join(answer_text.split()).lower()
    norm_context = " ".join(context_text.split()).lower()

    ans_start_char = norm_context.find(norm_answer)
    if ans_start_char == -1:
        return 0, 0

    ans_end_char = ans_start_char + len(norm_answer) - 1

    # Walk token offsets to find which tokens overlap the answer span
    tok_start, tok_end = 0, 0
    for idx, (tok_s, tok_e) in enumerate(offset_mapping):
        if tok_s == tok_e == 0:
            continue  # special token (CLS/SEP/PAD)
        if tok_s <= ans_start_char <= tok_e:
            tok_start = idx
        if tok_s <= ans_end_char <= tok_e:
            tok_end = idx
            break

    if tok_start == 0 and tok_end == 0:
        return 0, 0  # couldn't align — skip this example

    return tok_start, tok_end


# ── data loading ──────────────────────────────────────────────────────────────

def load_qa_pairs(
    data_dir: Path,
    use_variants: bool = True,
) -> list[dict]:
    """
    Load training QA pairs from metadata.jsonl.

    [Fix 2] Uses FIELD_QUESTIONS_VARIANTS (3 question phrasings per field)
    instead of a single question, tripling the effective training set.
    """
    metadata_path = data_dir / "metadata.jsonl"
    if not metadata_path.exists():
        logger.error(
            "No metadata.jsonl at %s — run export_training_data.py first", data_dir
        )
        sys.exit(1)

    with open(metadata_path, encoding="utf-8") as f:
        page_examples = [json.loads(line) for line in f if line.strip()]

    logger.info("Loaded %d page records from metadata.jsonl", len(page_examples))

    qa_pairs: list[dict] = []
    skipped_missing_image = 0
    skipped_no_question = 0
    skipped_gt_validation = 0

    for page_ex in page_examples:
        gt_raw = page_ex.get("ground_truth", "{}")
        if isinstance(gt_raw, str):
            try:
                gt_raw = json.loads(gt_raw)
            except Exception:
                continue
        gt_parse = gt_raw.get("gt_parse", {})

        file_name = page_ex.get("file_name", "")
        image_path = data_dir / file_name
        if not image_path.exists():
            logger.debug("Image not found: %s", image_path)
            skipped_missing_image += 1
            continue

        for field_key, value in gt_parse.items():
            value = str(value).strip() if value else ""

            # [Fix 4] GT validation
            if not validate_gt_value(field_key, value):
                skipped_gt_validation += 1
                continue

            # [Fix 2] Use ALL variants (or just primary if variants not requested)
            variants = (
                FIELD_QUESTIONS_VARIANTS.get(field_key)
                if use_variants
                else None
            )
            if not variants:
                primary = FIELD_QUESTIONS.get(field_key)
                if not primary:
                    skipped_no_question += 1
                    continue
                variants = [primary]

            for question in variants:
                qa_pairs.append({
                    "image_path": str(image_path),
                    "question": question,
                    "answer": value,
                    "field_key": field_key,
                    "payer_name": page_ex.get("payer_name", ""),
                })

    logger.info(
        "QA pairs: %d  (skipped: missing_img=%d no_question=%d gt_fail=%d)",
        len(qa_pairs),
        skipped_missing_image,
        skipped_no_question,
        skipped_gt_validation,
    )
    return qa_pairs


def stratify_qa_pairs(qa_pairs: list[dict]) -> list[dict]:
    """
    [Fix 3] Oversample underrepresented fields to the count of the most common field.

    Prevents the model from ignoring rare-but-important fields like
    diagnosis_code or units_requested.
    """
    if not qa_pairs:
        return qa_pairs

    import random

    by_field: dict[str, list[dict]] = {}
    for ex in qa_pairs:
        by_field.setdefault(ex["field_key"], []).append(ex)

    field_counts = Counter({k: len(v) for k, v in by_field.items()})
    logger.info("Field distribution before stratification:")
    for field, count in sorted(field_counts.items(), key=lambda x: -x[1]):
        logger.info("  %-30s %d examples", field, count)

    target = max(field_counts.values())
    logger.info("Stratification target: %d per field", target)

    random.seed(42)
    result: list[dict] = []
    for field_key, examples in by_field.items():
        if len(examples) >= target:
            result.extend(examples)
        else:
            needed = target - len(examples)
            extras = random.choices(examples, k=needed)
            result.extend(examples + extras)
            logger.info(
                "  Oversampled %-30s +%d (%d -> %d)",
                field_key, needed, len(examples), target,
            )

    random.shuffle(result)
    logger.info("Stratified dataset: %d examples total", len(result))
    return result


# ── preprocessing ─────────────────────────────────────────────────────────────

def try_load_tesseract() -> bool:
    """Check if pytesseract + tesseract binary are available."""
    try:
        import pytesseract  # noqa: F401
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def preprocess_full_layoutlm(example: dict, processor, tokenizer, max_length: int):
    """
    Full LayoutLMv2 preprocessing: image + bounding boxes + question.

    Returns None if the answer span cannot be aligned (example should be skipped).
    """
    import torch
    from PIL import Image

    image = Image.open(example["image_path"]).convert("RGB")

    encoding = processor(
        image,
        example["question"],
        return_tensors="pt",
        padding="max_length",
        truncation=True,
        max_length=max_length,
        return_offsets_mapping=True,
    )

    offset_mapping = encoding.pop("offset_mapping").squeeze().tolist()
    input_ids = encoding["input_ids"].squeeze().tolist()

    # Find the context (document tokens after the question + SEP)
    sep_id = tokenizer.sep_token_id
    sep_positions = [i for i, tid in enumerate(input_ids) if tid == sep_id]
    context_start = sep_positions[0] + 1 if sep_positions else 0
    context_ids = input_ids[context_start:]
    context_text = tokenizer.decode(context_ids, skip_special_tokens=True)

    start_pos, end_pos = find_answer_span_by_offset(
        offset_mapping, context_text, example["answer"]
    )

    if start_pos == 0 and end_pos == 0:
        return None  # can't align — skip

    return {
        **{k: v.squeeze() for k, v in encoding.items()},
        "start_positions": torch.tensor(start_pos),
        "end_positions": torch.tensor(end_pos),
    }


def preprocess_text_only(example: dict, tokenizer, max_length: int):
    """
    Text-only QA preprocessing (no image, no bounding boxes).

    Faster, tesseract-free, used as fallback.
    Uses character-offset span matching [Fix 1].
    """
    import torch

    question = example["question"]
    answer = example["answer"]

    encoding = tokenizer(
        question,
        answer,  # answer as context — guarantees the span exists
        max_length=max_length,
        padding="max_length",
        truncation=True,
        return_tensors="pt",
        return_offsets_mapping=True,
    )

    offset_mapping = encoding.pop("offset_mapping").squeeze().tolist()
    input_ids = encoding["input_ids"].squeeze().tolist()

    sep_id = tokenizer.sep_token_id
    sep_positions = [i for i, tid in enumerate(input_ids) if tid == sep_id]
    context_start = sep_positions[0] + 1 if len(sep_positions) > 0 else 0
    context_ids = input_ids[context_start:]
    context_text = tokenizer.decode(context_ids, skip_special_tokens=True)

    start_pos, end_pos = find_answer_span_by_offset(
        offset_mapping, context_text, answer
    )

    if start_pos == 0 and end_pos == 0:
        # Guaranteed fallback: answer is at position 1 since it's the first context token
        answer_ids = tokenizer(answer, add_special_tokens=False).input_ids
        start_pos = context_start
        end_pos = min(context_start + len(answer_ids) - 1, max_length - 1)

    return {
        **{k: v.squeeze() for k, v in encoding.items()},
        "start_positions": torch.tensor(start_pos),
        "end_positions": torch.tensor(end_pos),
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fine-tune LayoutLM Document QA with LoRA on healthcare fax data"
    )
    parser.add_argument("--data-dir", default="data/training", help="Training data directory")
    parser.add_argument("--output-dir", default="models/layoutlm-finetuned", help="Output adapter dir")
    parser.add_argument("--base-model", default="impira/layoutlm-document-qa", help="Base HF model")
    parser.add_argument("--epochs", type=int, default=3, help="Training epochs")
    parser.add_argument("--batch-size", type=int, default=4, help="Batch size")
    parser.add_argument("--lr", type=float, default=2e-5, help="Learning rate")
    parser.add_argument("--lora-r", type=int, default=8, help="LoRA rank")
    parser.add_argument("--lora-alpha", type=int, default=16, help="LoRA alpha")
    parser.add_argument("--max-length", type=int, default=512, help="Max sequence length")
    parser.add_argument(
        "--use-full-layoutlm", action="store_true",
        help="Full LayoutLMv2 preprocessing with image + bbox (requires pytesseract)",
    )
    parser.add_argument("--no-stratify", action="store_true", help="Disable stratified oversampling")
    parser.add_argument("--no-variants", action="store_true", help="Use only primary question per field")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)

    logger.info("=" * 60)
    logger.info("finetune_layoutlm.py")
    logger.info("  data_dir         : %s", data_dir)
    logger.info("  base_model       : %s", args.base_model)
    logger.info("  use_full_layoutlm: %s", args.use_full_layoutlm)
    logger.info("  variants         : %s", not args.no_variants)
    logger.info("  stratify         : %s", not args.no_stratify)
    logger.info("=" * 60)

    # ── load QA pairs ─────────────────────────────────────────────────────────
    qa_pairs = load_qa_pairs(data_dir, use_variants=not args.no_variants)
    if not qa_pairs:
        logger.error("No valid QA pairs. Run generate + export training data first.")
        sys.exit(1)

    if not args.no_stratify:
        qa_pairs = stratify_qa_pairs(qa_pairs)

    # ── imports ───────────────────────────────────────────────────────────────
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model
        from PIL import Image  # noqa: F401
        from transformers import (
            AutoModelForDocumentQuestionAnswering,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        logger.error(
            "Missing dependency: %s\n"
            "Install with: pip install peft datasets transformers torch Pillow",
            exc,
        )
        sys.exit(1)

    cache_dir = "D:\\.cache\\huggingface\\hub" if os.path.exists("D:\\") else None

    logger.info("Loading base model: %s", args.base_model)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, cache_dir=cache_dir)
    model = AutoModelForDocumentQuestionAnswering.from_pretrained(
        args.base_model, cache_dir=cache_dir
    )

    logger.info("Applying LoRA (r=%d, alpha=%d)...", args.lora_r, args.lora_alpha)
    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        target_modules=["query", "value"],
        lora_dropout=0.05,
        bias="none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── choose preprocessing mode ─────────────────────────────────────────────
    use_image = args.use_full_layoutlm and try_load_tesseract()
    if args.use_full_layoutlm and not use_image:
        logger.warning("pytesseract unavailable — falling back to text-only QA preprocessing")

    processor = None
    if use_image:
        try:
            from transformers import LayoutLMv2Processor
            processor = LayoutLMv2Processor.from_pretrained(args.base_model, cache_dir=cache_dir)
            logger.info("Full LayoutLMv2 preprocessing enabled (image + bbox)")
        except Exception as exc:
            logger.warning("Could not load LayoutLMv2Processor: %s — using text-only", exc)
            use_image = False

    # ── preprocess ────────────────────────────────────────────────────────────
    skipped_no_span = 0
    processed: list[dict] = []

    logger.info(
        "Preprocessing %d QA pairs (%s)...",
        len(qa_pairs),
        "full LayoutLMv2" if use_image else "text-only QA",
    )

    for ex in qa_pairs:
        try:
            if use_image:
                result = preprocess_full_layoutlm(ex, processor, tokenizer, args.max_length)
            else:
                result = preprocess_text_only(ex, tokenizer, args.max_length)
        except Exception as exc:
            logger.debug("Preprocessing error field=%s: %s", ex.get("field_key"), exc)
            result = None

        if result is None:
            skipped_no_span += 1
            continue
        processed.append(result)

    logger.info(
        "Preprocessing done: %d kept, %d skipped (span not found)",
        len(processed), skipped_no_span,
    )

    if not processed:
        logger.error(
            "All examples were skipped during preprocessing. "
            "Ensure images exist and answer values are present in the document text."
        )
        sys.exit(1)

    # ── build HuggingFace Dataset ─────────────────────────────────────────────
    import torch

    def to_dict_of_lists(examples: list[dict]) -> dict:
        keys = list(examples[0].keys())
        return {
            k: [
                ex[k].tolist() if isinstance(ex[k], torch.Tensor) else ex[k]
                for ex in examples
            ]
            for k in keys
        }

    hf_dict = to_dict_of_lists(processed)
    dataset = Dataset.from_dict(hf_dict)

    if len(processed) > 20:
        split = dataset.train_test_split(test_size=0.1, seed=42)
        train_dataset = split["train"]
        eval_dataset = split["test"]
    else:
        train_dataset = dataset
        eval_dataset = None

    logger.info(
        "Dataset ready: train=%d eval=%d",
        len(train_dataset),
        len(eval_dataset) if eval_dataset else 0,
    )

    # ── train ─────────────────────────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)

    training_args = TrainingArguments(
        output_dir=str(output_dir / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        learning_rate=args.lr,
        weight_decay=0.01,
        warmup_ratio=0.1,
        fp16=torch.cuda.is_available(),
        logging_steps=10,
        save_steps=100,
        save_total_limit=2,
        eval_strategy="epoch" if eval_dataset else "no",
        report_to="none",
        dataloader_num_workers=0,  # Windows compatible
        remove_unused_columns=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
    )

    logger.info(
        "Starting training: %d epochs × %d examples...",
        args.epochs, len(train_dataset),
    )
    trainer.train()

    # ── save ──────────────────────────────────────────────────────────────────
    adapter_dir = output_dir / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    logger.info("LoRA adapter saved: %s", adapter_dir)

    field_dist = dict(Counter(ex.get("field_key", "") for ex in qa_pairs))
    meta = {
        "base_model": args.base_model,
        "epochs": args.epochs,
        "lr": args.lr,
        "lora_r": args.lora_r,
        "lora_alpha": args.lora_alpha,
        "use_full_layoutlm": use_image,
        "variants_used": not args.no_variants,
        "stratified": not args.no_stratify,
        "total_qa_pairs": len(qa_pairs),
        "train_examples": len(train_dataset),
        "eval_examples": len(eval_dataset) if eval_dataset else 0,
        "skipped_no_span": skipped_no_span,
        "field_distribution": field_dist,
    }
    with open(output_dir / "training_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    # upload adapter to MinIO
    try:
        os.environ.setdefault(
            "DATABASE_URL",
            "postgresql+psycopg2://faxadmin:faxpass123@127.0.0.1:5432/fax_processor",
        )
        os.environ.setdefault("MINIO_ENDPOINT", "127.0.0.1:9000")
        os.environ.setdefault("MINIO_ACCESS_KEY", "minioadmin")
        os.environ.setdefault("MINIO_SECRET_KEY", "minioadmin123")

        from libs.shared.storage.s3_adapter import S3StorageAdapter

        storage = S3StorageAdapter()
        for f in adapter_dir.iterdir():
            if f.is_file():
                key = f"models/layoutlm-finetuned/{f.name}"
                storage.upload(key=key, data=f.read_bytes())
                logger.info("Uploaded %s -> MinIO key: %s", f.name, key)
    except Exception as exc:
        logger.warning("Could not upload to MinIO: %s (adapter saved locally)", exc)

    logger.info("=" * 60)
    logger.info("Fine-tuning complete!")
    logger.info("Activate adapter by setting:")
    logger.info("  LAYOUTLM_ADAPTER_PATH=%s", adapter_dir.resolve())
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
