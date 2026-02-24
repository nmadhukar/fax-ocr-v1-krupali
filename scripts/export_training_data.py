"""
Export ground-truth training data from fax_label_example for LayoutLM fine-tuning.

Reads verified labels from the database, downloads page images from MinIO,
and writes them to a local directory:

    output_dir/
      metadata.jsonl        # {file_name, ground_truth: {gt_parse: {field: val}}}
      images/
        <uuid>.png          # Page images

Usage:
    python scripts/export_training_data.py --output-dir data/training
    python scripts/export_training_data.py --payer ANTHEM --output-dir data/training
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://faxadmin:faxpass123@localhost:5432/fax_processor")
os.environ.setdefault("MINIO_ENDPOINT", "localhost:9000")
os.environ.setdefault("MINIO_ACCESS_KEY", "minioadmin")
os.environ.setdefault("MINIO_SECRET_KEY", "minioadmin123")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("export_training")


def main():
    parser = argparse.ArgumentParser(description="Export training data for LayoutLM fine-tuning")
    parser.add_argument("--output-dir", default="data/training", help="Output directory")
    parser.add_argument("--payer", help="Filter by payer name (e.g., ANTHEM)")
    parser.add_argument("--limit", type=int, default=10000, help="Max examples to export")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    from libs.shared.db.models.enums import PayerNameEnum
    from libs.shared.db.repositories.label_example_repo import LabelExampleRepository
    from libs.shared.db.session import get_db_session
    from libs.shared.storage.s3_adapter import S3StorageAdapter

    payer_filter = None
    if args.payer:
        try:
            payer_filter = PayerNameEnum(args.payer)
        except ValueError:
            logger.error("Invalid payer: %s", args.payer)
            sys.exit(1)

    with get_db_session() as db:
        label_repo = LabelExampleRepository(db)
        export_data = label_repo.export_for_training(
            payer_name=payer_filter,
            limit=args.limit,
        )

    if not export_data:
        logger.warning("No training data found. Have you submitted any reviews?")
        sys.exit(0)

    logger.info("Found %d page groups to export", len(export_data))

    # Download images and write metadata
    storage = S3StorageAdapter()
    metadata_path = output_dir / "metadata.jsonl"
    exported = 0

    with open(metadata_path, "w") as f:
        for item in export_data:
            page_key = item["page_storage_key"]
            if not page_key:
                continue

            # Generate local filename
            safe_name = page_key.replace("/", "_").replace("\\", "_")
            if not safe_name.endswith(".png"):
                safe_name += ".png"
            local_path = images_dir / safe_name

            # Download image from MinIO
            try:
                if not local_path.exists():
                    image_data = storage.download(page_key)
                    local_path.write_bytes(image_data)
            except Exception as e:
                logger.warning("Failed to download %s: %s", page_key, e)
                continue

            # Write metadata line
            metadata_line = {
                "file_name": str(local_path.relative_to(output_dir)),
                "ground_truth": json.dumps(item["ground_truth"]),
            }
            f.write(json.dumps(metadata_line) + "\n")
            exported += 1

    logger.info("Exported %d examples to %s", exported, output_dir)
    logger.info("Metadata: %s", metadata_path)
    logger.info("Images: %s", images_dir)


if __name__ == "__main__":
    main()
