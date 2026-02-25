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
        # H1-FIX: Use a CTE instead of a correlated subquery for O(n) perf.
        # M5-FIX: Join template_id in the main query instead of N+1 lookups.
        rows = db.execute(
            text("""
                WITH mismatches AS (
                    SELECT f2.fax_job_id, f2.field_key
                    FROM fax_extracted_field f2
                    JOIN fax_job j2 ON j2.fax_job_id = f2.fax_job_id
                    WHERE j2.processing_completed_at >= :period_start
                      AND j2.processing_completed_at < :period_end
                      AND j2.status IN ('COMPLETED', 'NEEDS_REVIEW')
                    GROUP BY f2.fax_job_id, f2.field_key
                    HAVING COUNT(DISTINCT f2.field_value) > 1
                )
                SELECT
                    j.payer_hint AS payer_name,
                    j.matched_template_version_id,
                    tv.template_id,
                    f.field_key,
                    COUNT(DISTINCT f.fax_job_id) AS total_jobs,
                    COUNT(DISTINCT m.fax_job_id) AS mismatch_jobs
                FROM fax_extracted_field f
                JOIN fax_job j ON j.fax_job_id = f.fax_job_id
                LEFT JOIN fax_template_version tv
                    ON tv.template_version_id = j.matched_template_version_id
                LEFT JOIN mismatches m
                    ON m.fax_job_id = f.fax_job_id AND m.field_key = f.field_key
                WHERE j.processing_completed_at >= :period_start
                  AND j.processing_completed_at < :period_end
                  AND j.status IN ('COMPLETED', 'NEEDS_REVIEW')
                GROUP BY j.payer_hint, j.matched_template_version_id,
                         tv.template_id, f.field_key
                HAVING COUNT(DISTINCT f.fax_job_id) >= 1
            """),
            {"period_start": period_start, "period_end": period_end},
        )
        results = rows.fetchall()

        for row in results:
            payer_name = row[0]
            template_id = row[2]  # M5-FIX: already joined
            field_key = row[3]
            total_count = row[4]
            mismatch_count = row[5]

            mismatch_rate = mismatch_count / total_count if total_count > 0 else 0
            alert_triggered = mismatch_rate > ALERT_THRESHOLD and mismatch_count > 0

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
                            (tenant_id, user_id, action, resource_type, details)
                        VALUES
                            (:tenant_id, :user_id, :action, :resource_type, CAST(:details AS jsonb))
                    """),
                    {
                        "tenant_id": "system",
                        "user_id": "fax-beat",
                        "action": "MISMATCH_ALERT",
                        "resource_type": "mismatch_metric",
                        "details": json.dumps({
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
