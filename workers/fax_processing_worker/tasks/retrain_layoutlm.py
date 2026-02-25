"""
Scheduled LayoutLM re-training Celery task.

Runs weekly (configurable) to check whether enough new human-review
corrections have accumulated in `fax_label_example`.  When the threshold
is met, it triggers the full fine-tuning pipeline as a subprocess:

    1. generate_training_data.py --source all      (populate fax_label_example)
    2. export_training_data.py   --output-dir ...  (download images)
    3. finetune_layoutlm.py      --data-dir   ...  (LoRA fine-tuning)

After successful training:
    - Registers a new ModelVersion record (inactive, for human review)
    - Logs the training run to the audit_log table
    - The adapter file is saved locally under models/layoutlm-finetuned/adapter/
      and uploaded to MinIO by the fine-tuning script

Environment variables:
    RETRAIN_MIN_NEW_LABELS  Minimum new fax_label_example rows since last
                            training to trigger re-training.  Default: 20.
    RETRAIN_DATA_DIR        Where export_training_data writes images.
                            Default: data/training
    RETRAIN_OUTPUT_DIR      Where finetune_layoutlm writes the adapter.
                            Default: models/layoutlm-finetuned

Note: The new adapter is registered as INACTIVE so a human operator can
promote it via the model_version API after reviewing accuracy metrics.
To auto-promote, set RETRAIN_AUTO_PROMOTE=true.
"""

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from celery import shared_task
from sqlalchemy import text

from libs.shared.db.session import get_db_session

logger = logging.getLogger(__name__)

# ── configuration ─────────────────────────────────────────────────────────────
_MIN_NEW_LABELS = int(os.environ.get("RETRAIN_MIN_NEW_LABELS", "20"))
_DATA_DIR = os.environ.get("RETRAIN_DATA_DIR", "data/training")
_OUTPUT_DIR = os.environ.get("RETRAIN_OUTPUT_DIR", "models/layoutlm-finetuned")
_AUTO_PROMOTE = os.environ.get("RETRAIN_AUTO_PROMOTE", "false").lower() == "true"
_TRAIN_CANDIDATE_RANKER = (
    os.environ.get("RETRAIN_TRAIN_CANDIDATE_RANKER", "true").lower() == "true"
)
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
_PYTHON = sys.executable


# ── task ──────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=0)
def check_and_retrain_layoutlm(self) -> dict:
    """
    Check if re-training is needed and trigger it if so.

    Runs weekly via Celery beat.  Checks the count of new
    fax_label_example rows since the last successful training
    run and triggers the fine-tuning pipeline when the threshold
    is reached.

    Returns:
        Dict with triggered, skipped, new_label_count, version_tag fields.
    """
    now = datetime.now(timezone.utc)
    result = {
        "triggered": False,
        "skipped": True,
        "new_label_count": 0,
        "reason": "",
        "version_tag": None,
        "timestamp": now.isoformat(),
    }

    with get_db_session() as db:

        # ── count new labels since last training ──────────────────────────
        last_train_row = db.execute(
            text("""
                SELECT MAX(created_at) AS last_trained
                FROM model_version
                WHERE model_type = 'layoutlm'
            """)
        ).fetchone()

        last_trained_at = last_train_row.last_trained if last_train_row else None

        if last_trained_at:
            new_count = db.execute(
                text("""
                    SELECT COUNT(*) FROM fax_label_example
                    WHERE created_at > :since
                """),
                {"since": last_trained_at},
            ).scalar()
        else:
            new_count = db.execute(
                text("SELECT COUNT(*) FROM fax_label_example")
            ).scalar()

        new_count = int(new_count or 0)
        result["new_label_count"] = new_count

        logger.info(
            "Re-training check: %d new labels (threshold=%d, last_train=%s)",
            new_count,
            _MIN_NEW_LABELS,
            last_trained_at.isoformat() if last_trained_at else "never",
        )

        if new_count < _MIN_NEW_LABELS:
            result["reason"] = (
                f"Only {new_count} new labels (need {_MIN_NEW_LABELS}). Skipping."
            )
            logger.info(result["reason"])
            return result

        # ── threshold met — trigger training pipeline ─────────────────────
        result["triggered"] = True
        result["skipped"] = False
        version_tag = now.strftime("auto-%Y%m%d-%H%M")
        result["version_tag"] = version_tag

        logger.info(
            "Triggering LayoutLM re-training: %d new labels → version %s",
            new_count,
            version_tag,
        )

        _audit_log(db, "RETRAIN_STARTED", {
            "new_label_count": new_count,
            "version_tag": version_tag,
            "threshold": _MIN_NEW_LABELS,
        })
        db.commit()

    # ── run pipeline as subprocesses ──────────────────────────────────────
    # Inherit the worker's environment — all credentials must be set via
    # env vars (DATABASE_URL, MINIO_*, SECRET_KEY) before the worker starts.
    # Never embed credentials here; fail loudly if required vars are missing.
    env = {**os.environ}
    missing = [v for v in ("DATABASE_URL", "MINIO_ENDPOINT") if not env.get(v)]
    if missing:
        logger.error(
            "Re-training aborted: required env vars not set: %s", ", ".join(missing)
        )
        result["reason"] = f"Missing required env vars: {', '.join(missing)}"
        return result

    try:
        _run_step("generate_training_data.py", ["--source", "all"], env)
        _run_step(
            "export_training_data.py",
            ["--output-dir", _DATA_DIR],
            env,
        )
        _run_step(
            "finetune_layoutlm.py",
            ["--data-dir", _DATA_DIR, "--output-dir", _OUTPUT_DIR],
            env,
        )
        if _TRAIN_CANDIDATE_RANKER:
            _run_step(
                "train_candidate_ranker.py",
                ["--output", "models/candidate_ranker/model.json"],
                env,
            )
    except subprocess.CalledProcessError as exc:
        logger.error("Re-training pipeline failed at step: %s", exc)
        with get_db_session() as db:
            _audit_log(db, "RETRAIN_FAILED", {
                "version_tag": version_tag,
                "error": str(exc),
            })
            db.commit()
        result["reason"] = f"Pipeline failed: {exc}"
        return result

    # ── register model version ────────────────────────────────────────────
    adapter_path = str(Path(_OUTPUT_DIR) / "adapter")

    with get_db_session() as db:
        from libs.shared.db.repositories.model_version_repo import ModelVersionRepository

        mv_repo = ModelVersionRepository(db)
        version = mv_repo.register_version(
            model_type="layoutlm",
            version_tag=version_tag,
            model_path=adapter_path,
            notes=(
                f"Auto-retrained on {new_count} new labels. "
                f"Triggered at {now.isoformat()}."
            ),
            config={
                "training_labels": new_count,
                "data_dir": _DATA_DIR,
                "output_dir": _OUTPUT_DIR,
                "auto_promote": _AUTO_PROMOTE,
            },
        )

        if _AUTO_PROMOTE:
            mv_repo.promote(version.model_version_id, promoted_by="celery-beat")
            logger.info("Auto-promoted adapter to production: %s", version_tag)

        _audit_log(db, "RETRAIN_COMPLETED", {
            "version_tag": version_tag,
            "adapter_path": adapter_path,
            "new_label_count": new_count,
            "auto_promoted": _AUTO_PROMOTE,
        })
        db.commit()

    logger.info(
        "Re-training complete: version=%s adapter=%s auto_promote=%s",
        version_tag,
        adapter_path,
        _AUTO_PROMOTE,
    )

    if not _AUTO_PROMOTE:
        logger.info(
            "Adapter registered as INACTIVE — promote via the model_version API "
            "after reviewing accuracy, or set RETRAIN_AUTO_PROMOTE=true."
        )

    result["reason"] = "Training pipeline completed successfully."
    return result


# ── helpers ───────────────────────────────────────────────────────────────────

def _run_step(script_name: str, args: list[str], env: dict) -> None:
    """Run a training pipeline script as a subprocess, raising on failure."""
    script_path = _SCRIPTS_DIR / script_name
    cmd = [_PYTHON, str(script_path)] + args
    logger.info("Running: %s", " ".join(cmd))

    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max per step
    )

    if proc.stdout:
        for line in proc.stdout.splitlines()[-30:]:  # last 30 lines
            logger.info("  [%s] %s", script_name, line)
    if proc.stderr:
        for line in proc.stderr.splitlines()[-20:]:
            logger.warning("  [%s stderr] %s", script_name, line)

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout, proc.stderr
        )


def _audit_log(db, event_type: str, data: dict) -> None:
    """Insert an entry into audit_log."""
    import json

    try:
        db.execute(
            text("""
                INSERT INTO audit_log (event_type, event_data)
                VALUES (:etype, :edata::jsonb)
            """),
            {
                "etype": event_type,
                "edata": json.dumps(data),
            },
        )
    except Exception as exc:
        logger.warning("audit_log insert failed: %s", exc)
