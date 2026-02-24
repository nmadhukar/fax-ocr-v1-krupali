"""
PriorAuthClient stub.

Attaches extracted JSON to a prior authorization case in an external
system.  This is a stub implementation — replace with actual integration
when the external Prior Auth service is available.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PriorAuthClient:
    """Stub client for prior-auth case management integration."""

    def attach_extraction(
        self,
        case_id: str,
        extraction_json: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach extracted data to a prior auth case.

        Args:
            case_id: External case identifier (or fax_job_id as fallback).
            extraction_json: Merged extraction output.

        Returns:
            Acknowledgement dict.
        """
        logger.info(
            "PriorAuthClient.attach_extraction called (stub) — case_id=%s, fields=%d",
            case_id,
            len(extraction_json),
        )
        return {"status": "attached", "case_id": case_id}

    def create_case(
        self,
        patient_name: str | None = None,
        member_id: str | None = None,
        payer: str | None = None,
    ) -> dict[str, Any]:
        """Create a new prior auth case (stub).

        Returns:
            Dict with stub case_id.
        """
        logger.info(
            "PriorAuthClient.create_case called (stub) — patient=%s, member=%s",
            patient_name,
            member_id,
        )
        from uuid import uuid4

        return {"status": "created", "case_id": str(uuid4())}
