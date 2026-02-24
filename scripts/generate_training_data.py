"""
Generate fax_label_example training records from verified pipeline output.

Reads verified fax_job extraction results from the database and creates
fax_label_example records that point to the corresponding page images in MinIO.
These records drive LayoutLM fine-tuning via export_training_data.py.

Two sources supported:
  batch3 / jobs — Any completed fax_job in the DB with a known payer.
                  Uses extraction_json as ground truth (confidence-filtered).
  templates     — Jobs whose original_filename matches a known template PDF,
                  used to create high-quality synthetic examples directly from
                  the seeded templates.  Requires the template PDFs to have
                  been uploaded and processed via the API first.

Usage:
    python scripts/generate_training_data.py --source batch3
    python scripts/generate_training_data.py --source templates
    python scripts/generate_training_data.py --source all
    python scripts/generate_training_data.py --source all --dry-run
    python scripts/generate_training_data.py --source all --min-confidence 0.6
    python scripts/generate_training_data.py --clear            # wipe existing records
"""

import argparse
import io
import json
import logging
import os
import sys
from pathlib import Path

# ── bootstrap ────────────────────────────────────────────────────────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://faxadmin:faxpass123@127.0.0.1:5432/fax_processor",
)
os.environ.setdefault("MINIO_ENDPOINT", "127.0.0.1:9000")
os.environ.setdefault("MINIO_ACCESS_KEY", "minioadmin")
os.environ.setdefault("MINIO_SECRET_KEY", "minioadmin123")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SECRET_KEY", "dev-secret-key-batch3-test")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("generate_training_data")

# ── constants ─────────────────────────────────────────────────────────────────
# Fields worth training LayoutLM on (skip internal/low-value fields)
TRAINABLE_FIELDS = {
    "member_id",
    "prior_auth_number",
    "patient_name",
    "patient_dob",
    "auth_effective_date",
    "auth_expiration_date",
    "next_review_date",
    "provider_name",
    "provider_npi",
    "provider_phone",
    "provider_fax",
    "service_code",
    "units_requested",
    "diagnosis_code",
}

# Template PDF basenames that indicate template sources
TEMPLATE_PDF_PATTERNS = [
    "humana", "anthem", "molina", "caresource", "buckeye",
    "amerihealth", "paramount", "promedica",
]

# Methods we trust as ground truth (skip UNKNOWN / low-quality methods)
TRUSTED_METHODS = {
    "TEMPLATE_OCR",
    "OCR_LABEL",
    "LAYOUTLM",
    "HUMAN_REVIEW",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_db():
    """Open a SQLAlchemy session."""
    from libs.shared.db.session import get_db_session
    return get_db_session()


def _verify_storage(key: str) -> bool:
    """Return True if the MinIO key actually exists."""
    try:
        from libs.shared.storage.s3_adapter import S3StorageAdapter
        return S3StorageAdapter().exists(key)
    except Exception:
        return False


def _find_best_page(pages, target_page: int = 1):
    """
    Return the fax_page record best suited as a LayoutLM input image.

    Prefers:
    1. Non-cover page matching target_page
    2. First non-cover page
    3. First page overall (fallback)
    """
    non_cover = [p for p in pages if not p.is_cover_page]
    if not non_cover:
        non_cover = list(pages)

    # Try exact target page number first
    exact = [p for p in non_cover if p.page_number == target_page]
    if exact:
        return exact[0]

    # Fall back to lowest page number
    return sorted(non_cover, key=lambda p: p.page_number)[0]


def _page_image_key(page) -> str | None:
    """
    Return the MinIO key for a page image.
    Prefer preprocessed_storage_key (deskewed/enhanced), else raw page_storage_key.
    """
    if page.preprocessed_storage_key:
        return page.preprocessed_storage_key
    return page.page_storage_key


def _is_template_source(filename: str) -> bool:
    """Return True if the filename suggests it is from a seeded template PDF."""
    fname_lower = (filename or "").lower()
    return any(pat in fname_lower for pat in TEMPLATE_PDF_PATTERNS)


# ── source A: any verified job ─────────────────────────────────────────────

def process_jobs(
    db,
    min_confidence: float,
    source_filter: str,  # "all", "batch3", or "templates"
    dry_run: bool,
    stats: dict,
) -> int:
    """
    Create fax_label_example records from fax_extraction data.

    Returns count of new records created (or would create in dry_run).
    """
    from sqlalchemy import text

    from libs.shared.db.models.enums import DocTypeEnum, PayerNameEnum
    from libs.shared.db.models.fax_extraction import FaxExtraction  # noqa: F401
    from libs.shared.db.models.fax_job import FaxJob  # noqa: F401
    from libs.shared.db.models.fax_page import FaxPage  # noqa: F401
    from libs.shared.db.repositories.label_example_repo import LabelExampleRepository

    label_repo = LabelExampleRepository(db)

    # ── query jobs ────────────────────────────────────────────────────────────
    rows = db.execute(
        text("""
            SELECT
                j.fax_job_id,
                j.original_filename,
                j.payer_hint,
                j.doc_type,
                j.status,
                e.extraction_json
            FROM fax_job j
            JOIN fax_extraction e ON e.fax_job_id = j.fax_job_id
            WHERE j.status IN ('COMPLETED', 'NEEDS_REVIEW', 'REVIEW_REQUIRED')
              AND j.payer_hint IS NOT NULL
              AND j.payer_hint NOT IN ('UNKNOWN', '')
              AND e.extraction_json IS NOT NULL
            ORDER BY j.created_at DESC
        """)
    ).fetchall()

    logger.info("Found %d jobs with extraction data", len(rows))

    created_total = 0
    skipped_total = 0

    for row in rows:
        fname = row.original_filename or ""
        is_template = _is_template_source(fname)

        # apply source filter
        if source_filter == "templates" and not is_template:
            continue
        if source_filter == "batch3" and is_template:
            continue

        job_id = row.fax_job_id
        payer_str = (row.payer_hint or "").upper()
        doc_type_str = (row.doc_type or "").upper()

        # parse extraction_json
        raw_json = row.extraction_json
        if isinstance(raw_json, str):
            try:
                raw_json = json.loads(raw_json)
            except Exception:
                continue
        if not isinstance(raw_json, dict):
            continue

        # load pages for this job
        pages = db.execute(
            text("""
                SELECT fax_page_id, page_number, is_cover_page,
                       page_storage_key, preprocessed_storage_key
                FROM fax_page
                WHERE fax_job_id = :jid
                ORDER BY page_number
            """),
            {"jid": job_id},
        ).fetchall()

        if not pages:
            logger.warning("Job %s has no pages — skipping", job_id)
            continue

        # payer / doc_type enums (best-effort)
        try:
            payer_enum = PayerNameEnum(payer_str)
        except ValueError:
            payer_enum = None
        try:
            doc_type_enum = DocTypeEnum(doc_type_str)
        except ValueError:
            doc_type_enum = None

        # determine source tag
        source_tag = "synthetic_template" if is_template else "batch3_extraction"

        # check how many label_examples already exist for this job
        existing_count = db.execute(
            text(
                "SELECT COUNT(*) FROM fax_label_example WHERE fax_job_id = :jid"
            ),
            {"jid": job_id},
        ).scalar()

        job_created = 0
        job_skipped = 0

        for field_key, field_data in raw_json.items():
            if field_key not in TRAINABLE_FIELDS:
                continue
            if not isinstance(field_data, dict):
                continue

            value = (field_data.get("value") or "").strip()
            if not value:
                continue

            confidence = float(field_data.get("confidence") or 0.0)
            if confidence < min_confidence:
                continue

            method = (field_data.get("method") or "").upper()
            if method and method not in TRUSTED_METHODS:
                # accept if confidence is high enough even for unknown method
                if confidence < 0.80:
                    continue

            # skip clearly wrong placeholder values
            if value.upper() in {"N/A", "NONE", "NULL", "UNKNOWN", "-", ""}:
                continue

            # pick best page: most templates put all fields on page 1 content
            # but we can refine per-field if needed
            best_pg = _find_best_page(pages, target_page=1)
            pg_key = best_pg.preprocessed_storage_key or best_pg.page_storage_key

            if not pg_key:
                logger.debug("No storage key for page %s", best_pg.fax_page_id)
                job_skipped += 1
                continue

            # check label_example doesn't already exist for this (job, field)
            dup = db.execute(
                text("""
                    SELECT 1 FROM fax_label_example
                    WHERE fax_job_id = :jid AND field_key = :fk
                    LIMIT 1
                """),
                {"jid": job_id, "fk": field_key},
            ).fetchone()
            if dup:
                job_skipped += 1
                continue

            if dry_run:
                logger.info(
                    "  [DRY-RUN] Would create: job=%s field=%s value=%r conf=%.2f page=%s",
                    str(job_id)[:8],
                    field_key,
                    value,
                    confidence,
                    best_pg.page_number,
                )
                job_created += 1
                continue

            # create the record
            try:
                label_repo.create_label(
                    fax_job_id=job_id,
                    fax_page_id=best_pg.fax_page_id,
                    field_key=field_key,
                    ground_truth_value=value,
                    page_storage_key=pg_key,
                    payer_name=payer_enum,
                    doc_type=doc_type_enum,
                    source=source_tag,
                    created_by="generate_training_data",
                )
                job_created += 1
                stats.setdefault(payer_str, {}).setdefault(field_key, 0)
                stats[payer_str][field_key] += 1
            except Exception as exc:
                logger.warning("Failed to create label for job=%s field=%s: %s", job_id, field_key, exc)
                job_skipped += 1

        if not dry_run and job_created > 0:
            db.flush()

        logger.info(
            "  %s | payer=%-15s | created=%d skipped=%d",
            fname[:40],
            payer_str,
            job_created,
            job_skipped,
        )
        created_total += job_created
        skipped_total += job_skipped

    if not dry_run and created_total > 0:
        db.commit()

    return created_total


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate fax_label_example training records from verified pipeline output"
    )
    parser.add_argument(
        "--source",
        choices=["all", "batch3", "templates"],
        default="all",
        help=(
            "all = all verified jobs; "
            "batch3 = non-template jobs only; "
            "templates = template-PDF jobs only"
        ),
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="Minimum extraction confidence to include (default 0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without writing to DB",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="DELETE all existing fax_label_example rows before generating",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("generate_training_data.py")
    logger.info("  source        : %s", args.source)
    logger.info("  min_confidence: %.2f", args.min_confidence)
    logger.info("  dry_run       : %s", args.dry_run)
    logger.info("  clear         : %s", args.clear)
    logger.info("=" * 60)

    from libs.shared.db.session import get_db_session
    from sqlalchemy import text

    with get_db_session() as db:

        # optional: wipe existing records
        if args.clear and not args.dry_run:
            cnt = db.execute(
                text("SELECT COUNT(*) FROM fax_label_example")
            ).scalar()
            logger.info("Clearing %d existing fax_label_example rows…", cnt)
            db.execute(text("DELETE FROM fax_label_example"))
            db.commit()
            logger.info("Cleared.")

        # check how many records exist already
        pre_count = db.execute(
            text("SELECT COUNT(*) FROM fax_label_example")
        ).scalar()
        logger.info("Existing fax_label_example records: %d", pre_count)

        stats: dict = {}
        created = process_jobs(
            db=db,
            min_confidence=args.min_confidence,
            source_filter=args.source,
            dry_run=args.dry_run,
            stats=stats,
        )

        post_count = db.execute(
            text("SELECT COUNT(*) FROM fax_label_example")
        ).scalar()

    # ── summary ───────────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 60)
    logger.info("SUMMARY%s", " (DRY RUN — nothing written)" if args.dry_run else "")
    logger.info("  Records before : %d", pre_count)
    logger.info("  Records created: %d", created)
    logger.info("  Records after  : %d", post_count)
    logger.info("")

    if stats:
        logger.info("  Breakdown by payer:")
        for payer in sorted(stats):
            fields = stats[payer]
            field_summary = ", ".join(
                f"{k}={v}" for k, v in sorted(fields.items())
            )
            logger.info("    %-20s → %d fields  [%s]", payer, sum(fields.values()), field_summary)
    else:
        logger.info("  (no new records generated)")

    logger.info("")
    if not args.dry_run and created > 0:
        logger.info("Done! Next step:")
        logger.info(
            "  python scripts/export_training_data.py --output-dir data/training"
        )
        logger.info("  python scripts/finetune_layoutlm.py --data-dir data/training")
    elif args.dry_run:
        logger.info("Dry run complete — re-run without --dry-run to write records.")
    else:
        logger.info(
            "No new records generated.  "
            "Have you processed any fax jobs with a known payer?"
        )


if __name__ == "__main__":
    main()
