"""
Template drift monitor task.

Aggregates drift signals from recent jobs and raises alerts when
template versions degrade. Optionally creates inactive candidate versions
to speed up template recalibration workflows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from celery import shared_task
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from libs.shared.config import get_settings
from libs.shared.db.models.audit_log import AuditAction, AuditLog
from libs.shared.db.models.fax_template import (
    FaxTemplateSample,
    FaxTemplateField,
    FaxTemplateVersion,
)
from libs.shared.db.session import get_db_session

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0)
def aggregate_template_drift(self) -> dict:
    """Compute recent template drift metrics and emit alerts."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)

    alerts = 0
    candidate_versions = 0
    checked_versions = 0
    discovery_clusters = 0

    with get_db_session() as db:
        rows = db.execute(
            text(
                """
                SELECT
                    matched_template_version_id AS version_id,
                    COUNT(*) AS total_jobs,
                    AVG(COALESCE(matched_template_score, 0)) AS avg_match_score,
                    SUM(CASE WHEN status = 'NEEDS_REVIEW' THEN 1 ELSE 0 END) AS needs_review_jobs
                FROM fax_job
                WHERE processing_completed_at >= :since
                  AND matched_template_version_id IS NOT NULL
                GROUP BY matched_template_version_id
                """
            ),
            {"since": since},
        ).fetchall()

        # H2-FIX: batch-load correction counts for ALL version IDs in one query
        version_ids = [str(row.version_id) for row in rows if row.version_id]
        corrections_map: dict[str, int] = {}
        if version_ids:
            corr_rows = db.execute(
                text(
                    """
                    SELECT
                        j.matched_template_version_id::text AS version_id,
                        COUNT(DISTINCT f.fax_job_id) AS correction_count
                    FROM fax_feedback f
                    JOIN fax_job j ON j.fax_job_id = f.fax_job_id
                    WHERE j.matched_template_version_id = ANY(:version_ids::uuid[])
                      AND j.processing_completed_at >= :since
                    GROUP BY j.matched_template_version_id
                    """
                ),
                {"version_ids": version_ids, "since": since},
            ).fetchall()
            for cr in corr_rows:
                corrections_map[str(cr.version_id)] = int(cr.correction_count)

        for row in rows:
            version_id = row.version_id
            total_jobs = int(row.total_jobs or 0)
            if total_jobs <= 0:
                continue

            checked_versions += 1
            avg_match_score = float(row.avg_match_score or 0.0)
            needs_review_rate = float(row.needs_review_jobs or 0) / total_jobs

            # H2-FIX: use pre-fetched corrections map
            corrections_count = corrections_map.get(str(version_id), 0)
            correction_rate = corrections_count / total_jobs

            signals: list[str] = []
            if avg_match_score < settings.adaptive.template_drift_low_match_threshold:
                signals.append("low_match_score")
            if needs_review_rate > settings.adaptive.template_drift_missing_critical_threshold:
                signals.append("high_needs_review_rate")
            if correction_rate > 0.25:
                signals.append("high_correction_rate")

            if not signals:
                continue

            alerts += 1
            payload = {
                "template_version_id": str(version_id),
                "window_start": since.isoformat(),
                "window_end": now.isoformat(),
                "total_jobs": total_jobs,
                "avg_match_score": round(avg_match_score, 4),
                "needs_review_rate": round(needs_review_rate, 4),
                "correction_rate": round(correction_rate, 4),
                "signals": signals,
            }

            db.add(
                AuditLog.log_action(
                    tenant_id="system",
                    action=AuditAction.PROCESS,
                    resource_type="template_drift_alert",
                    details=payload,
                )
            )

            if _ensure_candidate_version(db, version_id, payload):
                candidate_versions += 1

        # Self-learning discovery for unmatched document populations.
        # This supports non-prior-auth inbound forms where templates are not
        # yet seeded, by surfacing recurring clusters for template creation.
        unmatched_rows = db.execute(
            text(
                """
                SELECT
                    COALESCE(payer_hint::text, 'UNKNOWN') AS payer_name,
                    COALESCE(doc_type::text, 'UNKNOWN') AS doc_type,
                    COUNT(*) AS total_jobs
                FROM fax_job
                WHERE processing_completed_at >= :since
                  AND matched_template_version_id IS NULL
                GROUP BY COALESCE(payer_hint::text, 'UNKNOWN'), COALESCE(doc_type::text, 'UNKNOWN')
                HAVING COUNT(*) >= 3
                """
            ),
            {"since": since},
        ).fetchall()

        for row in unmatched_rows:
            discovery_clusters += 1
            payload = {
                "window_start": since.isoformat(),
                "window_end": now.isoformat(),
                "payer_name": str(row.payer_name),
                "doc_type": str(row.doc_type),
                "total_jobs": int(row.total_jobs or 0),
                "action": "create_template_candidate",
            }
            db.add(
                AuditLog.log_action(
                    tenant_id="system",
                    action=AuditAction.PROCESS,
                    resource_type="template_discovery_cluster",
                    details=payload,
                )
            )

        db.commit()

    logger.info(
        "Template drift monitor: checked=%d alerts=%d candidate_versions=%d",
        checked_versions,
        alerts,
        candidate_versions,
    )
    return {
        "checked_versions": checked_versions,
        "alerts": alerts,
        "candidate_versions_created": candidate_versions,
        "discovery_clusters": discovery_clusters,
        "window_start": since.isoformat(),
        "window_end": now.isoformat(),
    }


def _ensure_candidate_version(db, base_version_id, payload: dict) -> bool:
    """
    Create one inactive candidate version per template/day when drift is detected.
    M3-FIX: Also clones sample images so the candidate can be used for matching.
    """
    base_version = db.get(FaxTemplateVersion, base_version_id)
    if base_version is None:
        return False

    label = f"auto-drift-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    existing = (
        db.query(FaxTemplateVersion)
        .filter(
            FaxTemplateVersion.template_id == base_version.template_id,
            FaxTemplateVersion.version_label == label,
        )
        .first()
    )
    if existing is not None:
        return False

    new_cfg = {
        **(base_version.template_config or {}),
        "auto_generated": True,
        "drift_parent_version_id": str(base_version.template_version_id),
        "drift_alert": payload,
    }
    candidate = FaxTemplateVersion(
        template_id=base_version.template_id,
        version_label=label,
        match_min_score=base_version.match_min_score,
        match_phash_threshold=base_version.match_phash_threshold,
        match_orb_min_matches=base_version.match_orb_min_matches,
        template_config=new_cfg,
        is_active=False,
    )
    db.add(candidate)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        logger.info("Drift candidate version already exists for label=%s", label)
        return False

    # Clone field definitions so reviewers can recalibrate anchor/ROI config.
    fields = (
        db.query(FaxTemplateField)
        .filter(FaxTemplateField.template_version_id == base_version.template_version_id)
        .all()
    )
    for field in fields:
        db.add(
            FaxTemplateField(
                template_version_id=candidate.template_version_id,
                field_key=field.field_key,
                field_label=field.field_label,
                is_required=field.is_required,
                roi_x0=field.roi_x0,
                roi_y0=field.roi_y0,
                roi_x1=field.roi_x1,
                roi_y1=field.roi_y1,
                target_page=field.target_page,
                validation_regex=field.validation_regex,
                validation_message=field.validation_message,
                expected_type=field.expected_type,
                post_processing={
                    **(field.post_processing or {}),
                    "needs_recalibration": True,
                },
            )
        )

    # M3-FIX: Clone sample images so the candidate version has reference images
    # for pHash/ORB matching.
    samples = (
        db.query(FaxTemplateSample)
        .filter(FaxTemplateSample.template_version_id == base_version.template_version_id)
        .all()
    )
    for sample in samples:
        db.add(
            FaxTemplateSample(
                template_version_id=candidate.template_version_id,
                sample_storage_key=sample.sample_storage_key,
                phash_value=sample.phash_value,
                orb_descriptors=sample.orb_descriptors,
                orb_keypoints=sample.orb_keypoints,
                width_px=sample.width_px,
                height_px=sample.height_px,
            )
        )
    return True
