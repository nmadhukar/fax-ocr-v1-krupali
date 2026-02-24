"""
Mismatch monitoring Celery beat task.

Runs periodically (default: hourly) to aggregate field extraction
mismatch metrics.  A "mismatch" occurs when multiple extraction
candidates for the same field disagree on the value.

Metrics are stored in `fax_mismatch_metric` and an audit_log alert
is inserted when the mismatch rate for any field exceeds the threshold.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import text

from libs.shared.db.session import get_db_session

logger = logging.getLogger(__name__)

# Mismatch rate above this triggers an alert
ALERT_THRESHOLD = 0.30  # 30%


@shared_task(bind=True, max_retries=1, default_retry_delay=300)
def aggregate_mismatch_metrics(self) -> dict:
    """Aggregate field extraction mismatch metrics for the last period.

    Scans fax_extracted_field for jobs completed in the last hour,
    groups by (payer, template, field_key), and counts how many jobs
    had multiple conflicting candidates vs. total.

    Returns:
        Summary dict with metrics_inserted and alerts_triggered counts.
    """
    now = datetime.now(timezone.utc)
    period_end = now
    period_start = now - timedelta(hours=1)

    metrics_inserted = 0
    alerts_triggered = 0

    with get_db_session() as db:
        # Find fields with multiple candidates that have different values
        # A mismatch = same (job, field_key) has > 1 distinct field_value
        rows = db.execute(
            text("""
                SELECT
                    j.payer_hint AS payer_name,
                    j.matched_template_version_id,
                    f.field_key,
                    COUNT(DISTINCT f.fax_job_id) AS total_jobs,
                    COUNT(DISTINCT f.fax_job_id) FILTER (
                        WHERE f.fax_job_id IN (
                            SELECT f2.fax_job_id
                            FROM fax_extracted_field f2
                            WHERE f2.field_key = f.field_key
                              AND f2.fax_job_id = f.fax_job_id
                            GROUP BY f2.fax_job_id, f2.field_key
                            HAVING COUNT(DISTINCT f2.field_value) > 1
                        )
                    ) AS mismatch_jobs
                FROM fax_extracted_field f
                JOIN fax_job j ON j.fax_job_id = f.fax_job_id
                WHERE j.processing_completed_at >= :period_start
                  AND j.processing_completed_at < :period_end
                  AND j.status IN ('completed', 'needs_review')
                GROUP BY j.payer_hint, j.matched_template_version_id, f.field_key
                HAVING COUNT(DISTINCT f.fax_job_id) >= 1
            """),
            {"period_start": period_start, "period_end": period_end},
        )
        results = rows.fetchall()

        for row in results:
            payer_name = row[0]
            template_version_id = row[1]
            field_key = row[2]
            total_count = row[3]
            mismatch_count = row[4]

            mismatch_rate = mismatch_count / total_count if total_count > 0 else 0
            alert_triggered = mismatch_rate > ALERT_THRESHOLD and mismatch_count > 0

            # Look up template_id from version_id
            template_id = None
            if template_version_id:
                tv_row = db.execute(
                    text("""
                        SELECT template_id FROM fax_template_version
                        WHERE template_version_id = :vid
                    """),
                    {"vid": str(template_version_id)},
                ).fetchone()
                if tv_row:
                    template_id = tv_row[0]

            # Insert metric
            db.execute(
                text("""
                    INSERT INTO fax_mismatch_metric
                        (payer_name, template_id, field_key,
                         mismatch_count, total_count,
                         period_start, period_end, alert_triggered)
                    VALUES
                        (:payer, :tmpl_id, :field_key,
                         :mismatch, :total,
                         :p_start, :p_end, :alert)
                """),
                {
                    "payer": payer_name,
                    "tmpl_id": str(template_id) if template_id else None,
                    "field_key": field_key,
                    "mismatch": mismatch_count,
                    "total": total_count,
                    "p_start": period_start,
                    "p_end": period_end,
                    "alert": alert_triggered,
                },
            )
            metrics_inserted += 1

            if alert_triggered:
                alerts_triggered += 1
                db.execute(
                    text("""
                        INSERT INTO audit_log
                            (event_type, event_data)
                        VALUES
                            ('MISMATCH_ALERT', :data::jsonb)
                    """),
                    {
                        "data": json.dumps({
                            "payer": payer_name,
                            "field": field_key,
                            "mismatch_rate": round(mismatch_rate, 2),
                            "mismatch_count": mismatch_count,
                            "total_count": total_count,
                        }),
                    },
                )
                logger.warning(
                    "Mismatch alert: payer=%s field=%s rate=%.1f%% (%d/%d)",
                    payer_name,
                    field_key,
                    mismatch_rate * 100,
                    mismatch_count,
                    total_count,
                )

        db.commit()

    logger.info(
        "Mismatch monitoring: %d metrics inserted, %d alerts triggered",
        metrics_inserted,
        alerts_triggered,
    )

    return {
        "metrics_inserted": metrics_inserted,
        "alerts_triggered": alerts_triggered,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
