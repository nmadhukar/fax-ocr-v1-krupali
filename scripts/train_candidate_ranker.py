"""
Train a lightweight candidate ranker model from HITL corrections.

The produced artifact is a JSON method-bias model consumed by FieldBuilder.
It is intentionally simple and transparent so it can run inside restricted
environments without external ML dependencies.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# H3-FIX: Fail fast if credentials are not set — never use hardcoded defaults.
_REQUIRED_ENV = ["DATABASE_URL"]
_missing = [v for v in _REQUIRED_ENV if not os.environ.get(v)]
if _missing:
    print(
        f"ERROR: Required environment variables not set: {', '.join(_missing)}\n"
        f"Set them before running this script, e.g.:\n"
        f"  DATABASE_URL=postgresql+psycopg2://user:pass@host/db python {sys.argv[0]}",
        file=sys.stderr,
    )
    sys.exit(1)
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SECRET_KEY", os.environ.get("SECRET_KEY", "dev-only"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("train_candidate_ranker")


def _safe_bias(wins: int, total: int) -> float:
    # Laplace smoothing + bounded linear map into [-0.18, 0.18].
    # M6-NOTE: This ±0.18 range produces a 0.36 total swing which can
    # dominate FieldBuilder's agreement_bonus (0.15) and conflict_penalty
    # (0.05).  This is intentional for well-trained models (min_samples=10)
    # but operators should monitor via metrics_exact_match.json.
    rate = (wins + 1.0) / (total + 2.0)
    return max(-0.18, min(0.18, (rate - 0.5) * 0.72))


def main() -> int:
    parser = argparse.ArgumentParser(description="Train candidate ranker from HITL feedback")
    parser.add_argument(
        "--output",
        default="models/candidate_ranker/model.json",
        help="Output JSON model path",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=10,
        help="Minimum samples per method/field for field-specific bias",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="Only use corrections from the last N days (default: 90)",
    )
    args = parser.parse_args()

    from sqlalchemy import text

    from libs.shared.db.session import get_db_session

    with get_db_session() as db:
        rows = db.execute(
            text(
                """
                SELECT
                    f.field_key,
                    ef.method,
                    COUNT(*) AS total,
                    SUM(
                        CASE
                          WHEN lower(trim(coalesce(ef.field_value, ''))) =
                               lower(trim(coalesce(f.corrected_value, '')))
                          THEN 1 ELSE 0
                        END
                    ) AS wins
                FROM fax_feedback f
                JOIN fax_extracted_field ef
                  ON ef.fax_job_id = f.fax_job_id
                 AND ef.field_key = f.field_key
                WHERE f.corrected_value IS NOT NULL
                  AND f.feedback_type = 'correction'
                  AND f.created_at >= NOW() - make_interval(days => :lookback_days)
                GROUP BY f.field_key, ef.method
                """
            )
            ),
            {"lookback_days": args.lookback_days},
        ).fetchall()

        method_totals: dict[str, list[int]] = {}
        field_method: dict[str, dict[str, dict[str, int]]] = {}
        for row in rows:
            fk = str(row.field_key)
            method = str(row.method)
            total = int(row.total or 0)
            wins = int(row.wins or 0)

            method_totals.setdefault(method, [0, 0])
            method_totals[method][0] += wins
            method_totals[method][1] += total

            field_method.setdefault(fk, {})
            field_method[fk][method] = {"wins": wins, "total": total}

        global_bias: dict[str, float] = {}
        for method, (wins, total) in method_totals.items():
            global_bias[method] = round(_safe_bias(wins, total), 6)

        field_bias: dict[str, dict[str, dict[str, float]]] = {}
        for fk, methods in field_method.items():
            method_bias: dict[str, float] = {}
            for method, agg in methods.items():
                wins = int(agg["wins"])
                total = int(agg["total"])
                if total < args.min_samples:
                    continue
                method_bias[method] = round(_safe_bias(wins, total), 6)
            if method_bias:
                field_bias[fk] = {"method_bias": method_bias}

        model = {
            "version": "candidate-ranker-v1",
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "sample_count": sum(v[1] for v in method_totals.values()),
            "global": {"method_bias": global_bias},
            "fields": field_bias,
        }

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to temp file then rename to avoid partial reads
        import tempfile

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(output_path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(model, f, indent=2)
            Path(tmp_path).replace(output_path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        logger.info("Saved ranker model to %s", output_path)

        metrics_rows = db.execute(
            text(
                """
                SELECT
                    j.payer_hint,
                    j.matched_template_version_id,
                    f.field_key,
                    COUNT(*) AS reviewed_fields,
                    SUM(
                        CASE
                          WHEN EXISTS (
                            SELECT 1 FROM fax_extracted_field ef
                            WHERE ef.fax_job_id = f.fax_job_id
                              AND ef.field_key = f.field_key
                              AND lower(trim(coalesce(ef.field_value, ''))) =
                                  lower(trim(coalesce(f.corrected_value, '')))
                          )
                          THEN 1 ELSE 0
                        END
                    ) AS exact_match
                FROM fax_feedback f
                JOIN fax_job j ON j.fax_job_id = f.fax_job_id
                WHERE f.corrected_value IS NOT NULL
                GROUP BY j.payer_hint, j.matched_template_version_id, f.field_key
                ORDER BY reviewed_fields DESC
                """
            )
        ).fetchall()

        metrics_payload = []
        for row in metrics_rows:
            reviewed = int(row.reviewed_fields or 0)
            exact = int(row.exact_match or 0)
            metrics_payload.append(
                {
                    "payer": str(row.payer_hint),
                    "template_version_id": str(row.matched_template_version_id)
                    if row.matched_template_version_id
                    else None,
                    "field_key": str(row.field_key),
                    "reviewed_fields": reviewed,
                    "exact_match": exact,
                    "exact_match_rate": round(exact / reviewed, 4) if reviewed else 0.0,
                }
            )

        metrics_path = output_path.parent / "metrics_exact_match.json"
        tmp_fd2, tmp_path2 = tempfile.mkstemp(
            dir=str(metrics_path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd2, "w", encoding="utf-8") as f:
                json.dump(metrics_payload, f, indent=2)
            Path(tmp_path2).replace(metrics_path)
        except BaseException:
            Path(tmp_path2).unlink(missing_ok=True)
            raise
        logger.info("Saved exact-match metrics to %s", metrics_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
