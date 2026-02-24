"""
Template seeding script — bootstraps templates from client PDF samples.

For each PDF in the templates folder:
  1. Convert pages to images (PyMuPDF at 300 DPI)
  2. Run PaddleOCR on every page
  3. Auto-detect payer from OCR text (keyword signatures)
  4. Identify cover pages vs. content pages
  5. Determine doc type from text (approval / denial / PA form)
  6. Compute pHash + ORB features for each content page
  7. Auto-detect field ROIs by finding label→value pairs in OCR tokens
  8. Insert FaxTemplate + FaxTemplateVersion + FaxTemplateSample + FaxTemplateField

Usage:
    python scripts/seed_templates.py                     # Seed all PDFs
    python scripts/seed_templates.py --pdf Anthem.pdf    # Seed one PDF
    python scripts/seed_templates.py --dry-run           # Preview without DB writes
"""

import argparse
import io
import logging
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

# Use D:\temp for temporary files if C: is low on space
if os.path.exists("D:\\temp"):
    os.environ["TEMP"] = "D:\\temp"
    os.environ["TMP"] = "D:\\temp"
    import tempfile
    tempfile.tempdir = "D:\\temp"

import cv2
import numpy as np

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Must set minimal env before importing settings
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://faxadmin:faxpass123@127.0.0.1:5432/fax_processor")
os.environ.setdefault("MINIO_ENDPOINT", "localhost:9000")
os.environ.setdefault("MINIO_ACCESS_KEY", "minioadmin")
os.environ.setdefault("MINIO_SECRET_KEY", "minioadmin123")

from libs.shared.config.payer_signatures import PAYER_SIGNATURES
from libs.shared.db.models.enums import DocTypeEnum, PayerNameEnum
from libs.shared.db.models.fax_template import (
    FaxTemplate,
    FaxTemplateField,
    FaxTemplateSample,
    FaxTemplateVersion,
)
from libs.shared.db.session import get_db_session
from libs.shared.template.orb_matcher import OrbMatcher
from libs.shared.template.phash import PerceptualHasher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seed_templates")

# ---------------------------------------------------------------------------
# PDF Template Definitions — ground-truth from real client PDFs
# ---------------------------------------------------------------------------
# Each entry maps a PDF filename to its metadata and per-page field ROIs.
# ROI coordinates are normalized [0.0 - 1.0] relative to page dimensions.
# These were measured from the actual client PDFs.

TEMPLATE_DEFINITIONS = {
    "Humana_Healthy Horizons.pdf": {
        "payer": "HUMANA",
        "doc_type": "PRIOR_AUTH_APPROVAL",
        "template_name": "Humana Healthy Horizons - Medicaid Inpatient Notice",
        "description": "Humana Healthy Horizons Medicaid Inpatient Notice of Days Approved",
        "cover_pages": [1],
        "content_pages": [1, 2],  # Page 1 has provider_name, page 2 is main content
        "rotate_pages": {2: 270},  # Rotate 270° (counter-clockwise 90°) to make landscape
        "fields": {
            "prior_auth_number": {
                "page": 2,
                "label": "Humana Authorization Number",
                "labels": ["Humana Authorization Number", "Authorization Number"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.0467, "y0": 0.4547, "x1": 0.1357, "y1": 0.4906},
            },
            "patient_name": {
                "page": 2,
                "label": "Patient Name",
                "labels": ["Patient Name"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.1580, "y0": 0.4547, "x1": 0.3070, "y1": 0.5094},
            },
            "patient_dob": {
                "page": 2,
                "label": "Date of birth",
                "labels": ["Date of birth", "Date of Birth", "DOB"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.3048, "y0": 0.4460, "x1": 0.4238, "y1": 0.5094},
            },
            "member_id": {
                "page": 2,
                "label": "MemberID",
                "labels": ["MemberID", "Member ID"],
                "is_required": True,
                "expected_type": "member_id",
                "roi": {"x0": 0.4205, "y0": 0.4532, "x1": 0.5128, "y1": 0.5050},
            },
            "auth_effective_date": {
                "page": 2,
                "label": "Admission/Service Start Date",
                "labels": ["Admission/Service Start Date", "Admission/Service", "Service Start Date"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.7152, "y0": 0.4432, "x1": 0.9344, "y1": 0.5108},
            },
            "auth_expiration_date": {
                "page": 2,
                "label": "Dates Approved",
                "labels": ["Dates Approved"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.8176, "y0": 0.4590, "x1": 0.9310, "y1": 0.5050},
            },
            "provider_name": {
                "page": 1,
                "label": "Provider / Facility Name",
                "labels": ["Provider", "Facility Name", "Provider Name"],
                "is_required": False,
                "expected_type": "name",
                "roi": {"x0": 0.1339, "y0": 0.3671, "x1": 0.4241, "y1": 0.3993},
            },
            "service_code": {
                "page": 2,
                "label": "Service / CPT / HCPC Code",
                "labels": ["Service Code", "CPT Code", "HCPC Code"],
                "is_required": False,
                "expected_type": "service_code",
                "roi": {"x0": 0.4650, "y0": 0.7885, "x1": 0.6641, "y1": 0.8245},
            },
        },
    },
    "Anthem.pdf": {
        "payer": "ANTHEM",
        "doc_type": "PRIOR_AUTH_APPROVAL",
        "template_name": "Anthem BCBS Ohio - Authorization Notification",
        "description": "Anthem Blue Cross Blue Shield Ohio Medicaid Managed Care authorization",
        "cover_pages": [1],
        "content_pages": [2],
        "fields": {
            "patient_name": {
                "page": 2,
                "label": "Member Name",
                "labels": ["Member Name"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.5045, "y0": 0.1358, "x1": 0.8229, "y1": 0.1692},
            },
            "member_id": {
                "page": 2,
                "label": "Medicaid ID #",
                "labels": ["Medicaid ID #", "Medicaid ID", "Medicaid ID#"],
                "is_required": True,
                "expected_type": "member_id",
                "roi": {"x0": 0.4970, "y0": 0.1749, "x1": 0.8750, "y1": 0.2106},
            },
            "patient_dob": {
                "page": 2,
                "label": "Member DOB",
                "labels": ["Member DOB", "DOB"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.5030, "y0": 0.2060, "x1": 0.7292, "y1": 0.2428},
            },
            "provider_name": {
                "page": 2,
                "label": "Provider Name",
                "labels": ["Provider Name"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.5074, "y0": 0.2371, "x1": 0.8750, "y1": 0.2658},
            },
            "service_code": {
                "page": 2,
                "label": "Name of Service(s) Requested",
                "labels": ["Name of Service", "Service(s) Requested", "Services Requested"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.5085, "y0": 0.2356, "x1": 0.7864, "y1": 0.2956},
            },
            "prior_auth_number": {
                "page": 2,
                "label": "(*)Reference#",
                "labels": ["Reference#", "Reference #", "(*)Reference#", "Reference"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.4926, "y0": 0.3786, "x1": 0.7351, "y1": 0.3970},
            },
            "auth_effective_date": {
                "page": 2,
                "label": "Admission Date",
                "labels": ["Admission Date"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.4970, "y0": 0.4028, "x1": 0.9167, "y1": 0.4373},
            },
            "auth_expiration_date": {
                "page": 2,
                "label": "Authorized / Denied Days",
                "labels": ["Authorized / Denied Days", "Authorized/Denied Days", "Authorized"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.6875, "y0": 0.4039, "x1": 0.9003, "y1": 0.4384},
            },
            "next_review_date": {
                "page": 2,
                "label": "Next Review Date",
                "labels": ["Next Review Date"],
                "is_required": False,
                "expected_type": "date",
                "roi": {"x0": 0.4970, "y0": 0.4315, "x1": 0.8125, "y1": 0.4845},
            },
        },
    },
    "Molina_healthcare.pdf": {
        "payer": "MOLINA",
        "doc_type": "PRIOR_AUTH_APPROVAL",
        "template_name": "Molina Healthcare - Authorization Notification",
        "description": "Molina Healthcare Ohio Authorization Notification with service codes",
        "cover_pages": [1],
        "content_pages": [1, 2, 4],  # Page 1 has provider_name
        "fields": {
            "member_id": {
                "page": 2,
                "label": "Health Plan ID",
                "labels": ["Health Plan ID", "Health Plan Id"],
                "is_required": True,
                "expected_type": "member_id",
                "roi": {"x0": 0.1577, "y0": 0.0863, "x1": 0.2634, "y1": 0.1001},
            },
            "patient_name": {
                "page": 2,
                "label": "Member Name",
                "labels": ["Member Name"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.1518, "y0": 0.1001, "x1": 0.2619, "y1": 0.1116},
            },
            "patient_dob": {
                "page": 2,
                "label": "Member DOB",
                "labels": ["Member DOB", "DOB"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.1458, "y0": 0.1093, "x1": 0.2292, "y1": 0.1231},
            },
            "prior_auth_number": {
                "page": 2,
                "label": "Reference#",
                "labels": ["Reference#", "Reference #", "Authorization Number"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.0908, "y0": 0.1266, "x1": 0.2649, "y1": 0.1346},
            },
            "service_code": {
                "page": 2,
                "label": "Service Code",
                "labels": ["Service Code"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.0982, "y0": 0.2186, "x1": 0.1741, "y1": 0.2612},
            },
            "auth_effective_date": {
                "page": 2,
                "label": "Start Date",
                "labels": ["Start Date", "Date Span"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.6860, "y0": 0.2106, "x1": 0.8542, "y1": 0.2704},
            },
            "auth_expiration_date": {
                "page": 2,
                "label": "End Date",
                "labels": ["End Date"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.7738, "y0": 0.2140, "x1": 0.8571, "y1": 0.2566},
            },
            "provider_name": {
                "page": 1,
                "label": "Provider / Facility Name",
                "labels": ["Provider", "Facility Name", "Provider Name"],
                "is_required": False,
                "expected_type": "name",
                "roi": {"x0": 0.1205, "y0": 0.1634, "x1": 0.2500, "y1": 0.1830},
            },
            "next_review_date": {
                "page": 2,
                "label": "Next Review Date",
                "labels": ["Next Review Date"],
                "is_required": False,
                "expected_type": "date",
                "roi": {"x0": 0.0804, "y0": 0.3878, "x1": 0.2961, "y1": 0.4016},
            },
        },
    },
    "Caresource.pdf": {
        "payer": "CARESOURCE",
        "doc_type": "PRIOR_AUTH_DENIAL",
        "template_name": "CareSource - Notice of Adverse Decision",
        "description": "CareSource Ohio Notice of Adverse Decision for PA denial",
        "cover_pages": [1, 2],
        "content_pages": [3, 6],
        "fields": {
            "patient_name": {
                "page": 3,
                "label": "Member Name",
                "labels": ["Member Name"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.0923, "y0": 0.3763, "x1": 0.3304, "y1": 0.4304},
            },
            "patient_dob": {
                "page": 3,
                "label": "Member Date of Birth",
                "labels": ["Member Date of Birth", "Date of Birth"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.3438, "y0": 0.3705, "x1": 0.5878, "y1": 0.4315},
            },
            "member_id": {
                "page": 3,
                "label": "Member ID",
                "labels": ["Member ID", "Member ID:"],
                "is_required": True,
                "expected_type": "member_id",
                "roi": {"x0": 0.5893, "y0": 0.3717, "x1": 0.8244, "y1": 0.4223},
            },
            "prior_auth_number": {
                "page": 3,
                "label": "Reference Number",
                "labels": ["Reference Number", "Reference Number:"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.0908, "y0": 0.4292, "x1": 0.3125, "y1": 0.4776},
            },
            "provider_name": {
                "page": 3,
                "label": "Requesting Provider Name",
                "labels": ["Requesting Provider Name", "Requesting Provider"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.6071, "y0": 0.4292, "x1": 0.9256, "y1": 0.4891},
            },
            "service_code": {
                "page": 3,
                "label": "Service Code",
                "labels": ["Service Code", "Service Code:"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.0565, "y0": 0.5247, "x1": 0.2589, "y1": 0.5880},
            },
            "auth_effective_date": {
                "page": 3,
                "label": "Date(s) Requested",
                "labels": ["Date(s) Requested", "Dates Requested"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.2768, "y0": 0.5305, "x1": 0.4688, "y1": 0.5995},
            },
            "auth_expiration_date": {
                "page": 3,
                "label": "Auth End / Expiration Date",
                "labels": ["Expiration Date", "End Date"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.2946, "y0": 0.5696, "x1": 0.4301, "y1": 0.5938},
            },
        },
    },
    "buckeye.pdf": {
        "payer": "BUCKEYE",
        "doc_type": "PRIOR_AUTH_DENIAL",
        "template_name": "Buckeye Health Plan - Denial Notification",
        "description": "Buckeye Health Plan denial decision letter",
        "cover_pages": [1, 2, 3],
        "content_pages": [7],  # Page 7 has the main decision details
        "fields": {
            "prior_auth_number": {
                "page": 7,
                "label": "Reference Number",
                "labels": ["Reference Number", "Reference Number:"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.2440, "y0": 0.2920, "x1": 0.4955, "y1": 0.3218},
            },
            "patient_name": {
                "page": 7,
                "label": "Dear",
                "labels": ["Dear"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.0509, "y0": 0.3736, "x1": 0.8937, "y1": 0.4563},
            },
            "auth_effective_date": {
                "page": 7,
                "label": "Auth Start / Effective Date",
                "labels": ["Effective Date", "Start Date"],
                "is_required": False,
                "expected_type": "date",
                "roi": {"x0": 0.2844, "y0": 0.5414, "x1": 0.5240, "y1": 0.5609},
            },
            "provider_name": {
                "page": 7,
                "label": "Provider / Facility Name",
                "labels": ["Provider", "Facility Name", "Provider Name"],
                "is_required": False,
                "expected_type": "name",
                "roi": {"x0": 0.1183, "y0": 0.3264, "x1": 0.2590, "y1": 0.3575},
            },
        },
    },
    "Amerihealth_caritas.pdf": {
        "payer": "AMERIHEALTH",
        "doc_type": "PRIOR_AUTH_APPROVAL",
        "template_name": "AmeriHealth Caritas Ohio - Review Status",
        "description": "AmeriHealth Caritas Ohio Review Status form with auth details",
        "cover_pages": [1],
        "content_pages": [2],
        "fields": {
            "patient_name": {
                "page": 2,
                "label": "Member Name",
                "labels": ["Member Name", "Member Name:"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.2031, "y0": 0.2908, "x1": 0.5453, "y1": 0.3115},
            },
            "member_id": {
                "page": 2,
                "label": "Member plan ID number",
                "labels": ["Member plan ID number", "Member plan ID", "plan ID number"],
                "is_required": True,
                "expected_type": "member_id",
                "roi": {"x0": 0.2031, "y0": 0.3172, "x1": 0.5484, "y1": 0.3667},
            },
            "patient_dob": {
                "page": 2,
                "label": "Member date of birth",
                "labels": ["Member date of birth", "date of birth"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.7422, "y0": 0.3207, "x1": 0.9219, "y1": 0.3655},
            },
            "provider_name": {
                "page": 2,
                "label": "Provider or facility",
                "labels": ["Provider or facility", "Provider or facility:"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.1969, "y0": 0.3621, "x1": 0.6734, "y1": 0.3931},
            },
            "prior_auth_number": {
                "page": 2,
                "label": "Authorization number",
                "labels": ["Authorization number", "Authorization number:"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.0531, "y0": 0.5080, "x1": 0.3219, "y1": 0.5517},
            },
            "service_code": {
                "page": 2,
                "label": "Request for (HCPC/CPT codes)",
                "labels": ["Request for", "HCPC/CPT codes", "HCPC/CPT"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.0547, "y0": 0.6908, "x1": 0.4438, "y1": 0.7437},
            },
            "auth_effective_date": {
                "page": 2,
                "label": "Dates of service",
                "labels": ["Dates of service", "Dates of service:"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.5953, "y0": 0.5034, "x1": 0.8531, "y1": 0.5391},
            },
            "auth_expiration_date": {
                "page": 2,
                "label": "Auth End / Expiration Date",
                "labels": ["Expiration Date", "End Date"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.1922, "y0": 0.5517, "x1": 0.4906, "y1": 0.5736},
            },
            "next_review_date": {
                "page": 2,
                "label": "Next Review Date",
                "labels": ["Next Review Date"],
                "is_required": False,
                "expected_type": "date",
                "roi": {"x0": 0.2969, "y0": 0.5540, "x1": 0.4938, "y1": 0.5759},
            },
        },
    },
    "paramount.pdf": {
        "payer": "PARAMOUNT",
        "doc_type": "PRIOR_AUTH_APPROVAL",
        "template_name": "Paramount Advantage - Authorization Fax",
        "description": "Paramount Advantage (Affiliate of ProMedica) authorization fax",
        "cover_pages": [],
        "content_pages": [1],  # Single page with all content
        "fields": {
            "patient_name": {
                "page": 1,
                "label": "Member Name",
                "labels": ["Member Name", "Member Name:"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.0656, "y0": 0.3598, "x1": 0.3603, "y1": 0.3897},
            },
            "member_id": {
                "page": 1,
                "label": "Member Number",
                "labels": ["Member Number", "Member Number:"],
                "is_required": True,
                "expected_type": "member_id",
                "roi": {"x0": 0.1786, "y0": 0.3897, "x1": 0.4198, "y1": 0.4161},
            },
            "patient_dob": {
                "page": 1,
                "label": "Member DOB",
                "labels": ["Member DOB", "Member DOB:"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.0458, "y0": 0.4126, "x1": 0.3145, "y1": 0.4494},
            },
            "prior_auth_number": {
                "page": 1,
                "label": "Authorization Number",
                "labels": ["Authorization Number", "Authorization Number:"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.2412, "y0": 0.4437, "x1": 0.4458, "y1": 0.4678},
            },
            "auth_effective_date": {
                "page": 1,
                "label": "Authorization Dates",
                "labels": ["Authorization Dates", "Authorization Dates:"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.2290, "y0": 0.5000, "x1": 0.4855, "y1": 0.5207},
            },
            "auth_expiration_date": {
                "page": 1,
                "label": "Auth End / Expiration Date",
                "labels": ["Expiration Date", "End Date"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.3160, "y0": 0.5011, "x1": 0.4229, "y1": 0.5276},
            },
            "provider_name": {
                "page": 1,
                "label": "Provider / Facility Name",
                "labels": ["Provider", "Facility Name", "Provider Name"],
                "is_required": False,
                "expected_type": "name",
                "roi": {"x0": 0.1252, "y0": 0.2793, "x1": 0.2458, "y1": 0.3023},
            },
            "service_code": {
                "page": 1,
                "label": "Service / CPT / HCPC Code",
                "labels": ["Service Code", "CPT Code", "HCPC Code"],
                "is_required": False,
                "expected_type": "service_code",
                "roi": {"x0": 0.2870, "y0": 0.4759, "x1": 0.3542, "y1": 0.4989},
            },
        },
    },
    "promedica.pdf": {
        "payer": "PROMEDICA",
        "doc_type": "PRIOR_AUTH_FORM",
        "template_name": "ProMedica - OH Urine Drug Testing PA Request",
        "description": "Ohio Urine Drug Testing Prior Authorization PA Request Form via ProMedica",
        "cover_pages": [1, 2],
        "content_pages": [3, 13],  # Page 3 is PA form, page 13 has auth number
        "fields": {
            "patient_name": {
                "page": 3,
                "label": "Last Name",
                "labels": ["Last Name", "First Name"],
                "is_required": True,
                "expected_type": "text",
                "roi": {"x0": 0.1603, "y0": 0.2609, "x1": 0.7349, "y1": 0.2793},
            },
            "patient_dob": {
                "page": 3,
                "label": "DOB",
                "labels": ["DOB", "DOB:"],
                "is_required": True,
                "expected_type": "date",
                "roi": {"x0": 0.1667, "y0": 0.2805, "x1": 0.2746, "y1": 0.2989},
            },
            "member_id": {
                "page": 3,
                "label": "Member ID",
                "labels": ["Member ID", "Member ID:"],
                "is_required": True,
                "expected_type": "member_id",
                "roi": {"x0": 0.3778, "y0": 0.2701, "x1": 0.5175, "y1": 0.3184},
            },
            "provider_name": {
                "page": 3,
                "label": "Ordering Provider Name",
                "labels": ["Ordering Provider Name"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.3381, "y0": 0.3230, "x1": 0.5397, "y1": 0.3414},
            },
            "provider_npi": {
                "page": 3,
                "label": "NPI",
                "labels": ["NPI", "NPI:"],
                "is_required": False,
                "expected_type": "npi",
                "roi": {"x0": 0.3905, "y0": 0.3448, "x1": 0.4968, "y1": 0.3586},
            },
            "service_code": {
                "page": 3,
                "label": "CPT Code",
                "labels": ["CPT Code", "type of test"],
                "is_required": False,
                "expected_type": "text",
                "roi": {"x0": 0.4238, "y0": 0.5782, "x1": 0.4873, "y1": 0.5989},
            },
            "auth_expiration_date": {
                "page": 3,
                "label": "Auth End / Expiration Date",
                "labels": ["Expiration Date", "End Date"],
                "is_required": False,
                "expected_type": "date",
                "roi": {"x0": 0.5286, "y0": 0.5402, "x1": 0.6508, "y1": 0.5655},
            },
            "prior_auth_number": {
                "page": 13,
                "label": "Authorization / Reference Number",
                "labels": ["Authorization Number", "Reference Number"],
                "is_required": True,
                "expected_type": "auth_number",
                "roi": {"x0": 0.1524, "y0": 0.1115, "x1": 0.3095, "y1": 0.1345},
            },
        },
    },
}


# ---------------------------------------------------------------------------
# OCR token container (lightweight, no DB dependency)
# ---------------------------------------------------------------------------
class OcrTokenLocal:
    """Local OCR token for label matching without DB."""

    __slots__ = ("text", "x0", "y0", "x1", "y1", "confidence", "line_number")

    def __init__(self, text: str, x0: float, y0: float, x1: float, y1: float,
                 confidence: float, line_number: int):
        self.text = text
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.confidence = confidence
        self.line_number = line_number


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def pdf_to_images(pdf_path: str) -> list[np.ndarray]:
    """Convert PDF pages to numpy images at 300 DPI."""
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    pages = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        mat = fitz.Matrix(300 / 72, 300 / 72)  # 300 DPI
        pix = page.get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )

        if pix.n == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
        elif pix.n == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        pages.append(img)

    doc.close()
    return pages


def run_ocr_on_image(image: np.ndarray) -> list[OcrTokenLocal]:
    """Run PaddleOCR on an image via subprocess to avoid Windows deadlock.

    PaddleOCR's PaddlePaddle backend can deadlock on Windows when imported
    after heavy module-level imports (SQLAlchemy, etc.). Running in a clean
    subprocess matches the pattern proven to work in diagnostic testing.
    """
    import json
    import subprocess
    import tempfile

    height, width = image.shape[:2]

    # Save image to temp file
    tmp_dir = "D:\\temp" if os.path.isdir("D:\\temp") else tempfile.gettempdir()
    img_path = os.path.join(tmp_dir, f"_ocr_tmp_{os.getpid()}.png")
    cv2.imwrite(img_path, image)

    # Subprocess script that runs PaddleOCR and outputs JSON
    ocr_script = """
import sys, json, os
os.environ["TEMP"] = os.environ.get("TEMP", r"D:\\temp")
os.environ["TMP"] = os.environ.get("TMP", r"D:\\temp")
import cv2
from paddleocr import PaddleOCR
ocr = PaddleOCR(use_angle_cls=True, lang="en", use_gpu=False, show_log=False)
img = cv2.imread(sys.argv[1])
result = ocr.ocr(img, cls=True)
detections = []
if result and result[0]:
    for det in result[0]:
        points = det[0]
        text, conf = det[1]
        detections.append({"points": points, "text": text.strip(), "conf": conf})
json.dump(detections, sys.stdout)
"""

    try:
        env = os.environ.copy()
        env["TEMP"] = tmp_dir
        env["TMP"] = tmp_dir
        proc = subprocess.run(
            [sys.executable, "-c", ocr_script, img_path],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if proc.returncode != 0:
            logger.error("  OCR subprocess failed: %s", proc.stderr[-500:] if proc.stderr else "no stderr")
            return []

        detections = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        logger.error("  OCR subprocess timed out after 120s")
        return []
    except json.JSONDecodeError as e:
        logger.error("  OCR subprocess returned invalid JSON: %s", e)
        return []
    finally:
        if os.path.exists(img_path):
            os.remove(img_path)

    if not detections:
        return []

    # Process detections into OcrTokenLocal
    processed = []
    for det in detections:
        points = det["points"]
        y_center = (points[0][1] + points[2][1]) / 2
        processed.append((y_center, points, det["text"], det["conf"]))

    processed.sort(key=lambda x: x[0])

    # Assign line numbers
    line_threshold = height * 0.015  # 1.5% of height
    line_num = 1
    last_y = -999
    tokens = []

    for y_center, points, text, conf in processed:
        if y_center - last_y > line_threshold and last_y > 0:
            line_num += 1
        last_y = y_center

        # Normalize coordinates [0.0 - 1.0]
        x0 = min(p[0] for p in points) / width
        y0 = min(p[1] for p in points) / height
        x1 = max(p[0] for p in points) / width
        y1 = max(p[1] for p in points) / height

        tokens.append(OcrTokenLocal(
            text=text,
            x0=max(0.0, x0),
            y0=max(0.0, y0),
            x1=min(1.0, x1),
            y1=min(1.0, y1),
            confidence=conf,
            line_number=line_num,
        ))

    return tokens


def _ocr_normalize(text: str) -> str:
    """Normalize text for OCR-tolerant matching (O↔0, I↔1)."""
    return text.lower().replace("0", "o").replace("1", "i")


def _is_label_boundary(token_text: str, label_words: set[str]) -> bool:
    """Check if a token is likely a label for another field.

    Used during value-token collection to stop at column/field boundaries.
    """
    normalized = token_text.lower().rstrip(":").rstrip("#").strip()
    if not normalized or len(normalized) < 3:
        return False
    # Exact word match
    if normalized in label_words:
        return True
    # Token starts with a known label word (e.g. "MemberID" starts with "member")
    for lw in label_words:
        if len(lw) >= 4 and normalized.startswith(lw):
            return True
    return False


def find_field_roi(
    label_strings: list[str],
    tokens: list[OcrTokenLocal],
    search_right: bool = True,
    search_below: bool = True,
    value_max_tokens: int = 4,
    max_value_width: float = 0.30,
    all_label_words: set[str] | None = None,
) -> dict | None:
    """
    Find the ROI for a field value by locating its label in OCR tokens.

    Strategy:
      1. Find tokens that match (substring) any of the label strings
         (with OCR-tolerant matching: O↔0, I↔1)
      2. Collect value tokens to the RIGHT on the same line
         (with max_value_width clipping and label boundary detection)
      3. If no right-tokens, check for inline label:value in the label token
      4. If still nothing, collect tokens BELOW the label
      5. Return the bounding box of the value tokens

    Returns dict with {x0, y0, x1, y1} (normalized) or None if not found.
    """
    if not tokens or not label_strings:
        return None

    # Canonicalize labels for matching
    canon_labels = [l.lower().strip() for l in label_strings]

    # --- Step 1: Find label token(s) ---
    best_label_tokens = []
    best_label_score = 0

    for label in canon_labels:
        label_words = label.split()
        label_norm = _ocr_normalize(label)

        for i, token in enumerate(tokens):
            token_text = token.text.lower().strip()
            token_norm = _ocr_normalize(token_text)

            # Single-word label: substring match (OCR-tolerant)
            if len(label_words) == 1:
                lw_norm = _ocr_normalize(label_words[0])
                if lw_norm in token_norm or token_norm in lw_norm:
                    score = len(label_words[0]) / max(len(token_text), 1)
                    if score > best_label_score:
                        best_label_tokens = [token]
                        best_label_score = score
            else:
                # Multi-word label: find consecutive tokens on same line
                matched_tokens = []
                words_found = 0
                for j in range(i, min(i + len(label_words) + 2, len(tokens))):
                    t = tokens[j]
                    # Must be on same line (or very close)
                    if matched_tokens and abs(t.line_number - matched_tokens[0].line_number) > 1:
                        break
                    t_text = t.text.lower().strip().rstrip(":").rstrip("#")
                    t_norm = _ocr_normalize(t_text)
                    token_added = False
                    for w in label_words[words_found:]:
                        w_clean = w.rstrip(":").rstrip("#")
                        w_norm = _ocr_normalize(w_clean)
                        if w_norm in t_norm or t_norm in w_norm:
                            if not token_added:
                                matched_tokens.append(t)
                                token_added = True
                            words_found += 1
                        else:
                            break  # Words must appear sequentially

                if words_found >= len(label_words) * 0.6:  # 60% word match
                    score = words_found / len(label_words)
                    if score > best_label_score:
                        best_label_tokens = matched_tokens
                        best_label_score = score

    if not best_label_tokens:
        return None

    # --- Step 2: Find value tokens to the RIGHT on the same line ---
    label_x0 = min(t.x0 for t in best_label_tokens)
    label_x1 = max(t.x1 for t in best_label_tokens)
    label_y0 = min(t.y0 for t in best_label_tokens)
    label_y1 = max(t.y1 for t in best_label_tokens)
    label_line = best_label_tokens[0].line_number
    row_height = max(label_y1 - label_y0, 0.01)  # Minimum height to avoid division issues

    value_tokens = []

    if search_right:
        right_candidates = []
        max_x = label_x1 + max_value_width  # Width-limited search
        for token in tokens:
            if token in best_label_tokens:
                continue
            # Same line, to the right, within max width
            same_line = abs(token.y0 - label_y0) < row_height * 0.7
            to_right = token.x0 >= label_x1 - 0.01
            within_width = token.x0 < max_x
            if same_line and to_right and within_width:
                right_candidates.append(token)
        # Sort by x-position (reading order)
        right_candidates.sort(key=lambda t: t.x0)
        # Truncate at label boundary (another field's label)
        if all_label_words:
            truncated = []
            for t in right_candidates:
                if _is_label_boundary(t.text, all_label_words):
                    break
                truncated.append(t)
            right_candidates = truncated
        value_tokens = right_candidates[:value_max_tokens]

    # --- Step 2b: Check for inline label:value in the label token ---
    if not value_tokens and len(best_label_tokens) == 1:
        label_token = best_label_tokens[0]
        token_text = label_token.text.strip()
        # Check for "Label:Value" or "Label: Value" pattern
        colon_idx = token_text.find(":")
        if colon_idx > 0 and colon_idx < len(token_text) - 1:
            value_part = token_text[colon_idx + 1:].strip()
            if value_part:
                # Estimate the value portion's horizontal extent
                # The label is roughly (0..colon_idx) and value is (colon_idx+1..end)
                label_frac = (colon_idx + 1) / len(token_text)
                value_x0 = label_token.x0 + (label_token.x1 - label_token.x0) * label_frac
                # Create a synthetic token for the value portion
                value_tokens = [OcrTokenLocal(
                    text=value_part,
                    x0=value_x0,
                    y0=label_token.y0,
                    x1=label_token.x1,
                    y1=label_token.y1,
                    confidence=label_token.confidence,
                    line_number=label_token.line_number,
                )]

    # --- Step 3: If no right-tokens, try below ---
    if not value_tokens and search_below:
        below_candidates = []
        max_below_y = label_y1 + row_height * 4.0  # 4x row height to handle table gaps
        max_below_x = label_x0 + max_value_width  # Column-width clipping
        for token in tokens:
            if token in best_label_tokens:
                continue
            # Below the label, within 2.5 row heights
            below = label_y1 - 0.005 <= token.y0 <= max_below_y
            # Tighter horizontal alignment: column-aligned with max_value_width
            h_close = token.x0 < max_below_x and token.x1 > label_x0 - 0.02
            if below and h_close:
                # Skip if this token is another field's label
                if all_label_words and _is_label_boundary(token.text, all_label_words):
                    continue
                below_candidates.append(token)
        # Sort by y then x (reading order) and take up to max_tokens
        below_candidates.sort(key=lambda t: (t.y0, t.x0))
        value_tokens = below_candidates[:value_max_tokens]

    if not value_tokens:
        return None

    # --- Step 4: Compute value ROI with padding ---
    padding = 0.008  # ~0.8% padding
    roi = {
        "x0": max(0.0, min(t.x0 for t in value_tokens) - padding),
        "y0": max(0.0, min(t.y0 for t in value_tokens) - padding),
        "x1": min(1.0, max(t.x1 for t in value_tokens) + padding),
        "y1": min(1.0, max(t.y1 for t in value_tokens) + padding),
    }

    # Sanity check: ROI must be reasonable
    roi_w = roi["x1"] - roi["x0"]
    roi_h = roi["y1"] - roi["y0"]
    if roi_w < 0.01 or roi_h < 0.005 or roi_w > 0.85 or roi_h > 0.20:
        # Suspicious ROI — too small, too big, or inverted
        logger.warning(
            "    Suspicious ROI for labels %s: w=%.3f h=%.3f — skipping",
            label_strings[0], roi_w, roi_h,
        )
        return None

    # Collect the value text for logging
    value_text = " ".join(t.text for t in value_tokens)
    logger.info(
        "    Found value for '%s': '%s' at ROI (%.3f,%.3f)-(%.3f,%.3f)",
        label_strings[0], value_text[:60],
        roi["x0"], roi["y0"], roi["x1"], roi["y1"],
    )

    return roi


def compute_features(image: np.ndarray) -> tuple[int, bytes, bytes, int, int]:
    """
    Compute pHash + ORB features for a page image.

    Returns: (phash_value, orb_keypoints_bytes, orb_descriptors_bytes, width, height)
    """
    hasher = PerceptualHasher()
    orb_matcher = OrbMatcher()

    phash_val = hasher.compute_hash(image)
    # Convert to signed for PostgreSQL BigInteger storage
    phash_val = PerceptualHasher.to_signed(phash_val)
    orb_features = orb_matcher.compute_features(image)
    kp_bytes, desc_bytes = orb_features.serialize()

    height, width = image.shape[:2]

    logger.info(
        "    Features: pHash=%d, ORB keypoints=%d, image=%dx%d",
        phash_val, len(orb_features.keypoints), width, height,
    )

    return phash_val, kp_bytes, desc_bytes or b"", width, height


def seed_one_template(
    pdf_path: str,
    definition: dict,
    dry_run: bool = False,
) -> bool:
    """
    Seed a single template from a PDF file.

    Returns True on success.
    """
    filename = os.path.basename(pdf_path)
    payer_key = definition["payer"]
    doc_type_key = definition["doc_type"]
    template_name = definition["template_name"]
    content_pages = definition["content_pages"]
    field_defs = definition["fields"]

    logger.info("=" * 70)
    logger.info("Processing: %s", filename)
    logger.info("  Payer: %s, DocType: %s", payer_key, doc_type_key)
    logger.info("  Content pages: %s", content_pages)

    # --- Convert PDF to images ---
    try:
        page_images = pdf_to_images(pdf_path)
    except Exception as e:
        logger.error("  Failed to convert PDF: %s", e)
        return False

    logger.info("  Pages: %d", len(page_images))

    # Apply page rotations if specified
    rotate_pages = definition.get("rotate_pages", {})
    for page_num, angle in rotate_pages.items():
        idx = page_num - 1
        if idx < len(page_images):
            if angle == 90:
                page_images[idx] = cv2.rotate(page_images[idx], cv2.ROTATE_90_CLOCKWISE)
            elif angle == 180:
                page_images[idx] = cv2.rotate(page_images[idx], cv2.ROTATE_180)
            elif angle == 270:
                page_images[idx] = cv2.rotate(page_images[idx], cv2.ROTATE_90_COUNTERCLOCKWISE)
            logger.info("  Rotated page %d by %d°", page_num, angle)

    if not content_pages:
        logger.warning("  No content pages defined — skipping")
        return False

    # Use first content page as the template sample
    sample_page_idx = content_pages[0] - 1  # 0-indexed
    if sample_page_idx >= len(page_images):
        logger.error("  Content page %d exceeds PDF pages (%d)", content_pages[0], len(page_images))
        return False

    sample_image = page_images[sample_page_idx]

    # --- Run OCR only on pages that need auto-detection ---
    # Skip OCR for pages where ALL fields have hardcoded ROIs
    pages_needing_ocr = set()
    for fk, fd in field_defs.items():
        if "roi" not in fd:
            pages_needing_ocr.add(fd["page"])

    ocr_by_page: dict[int, list[OcrTokenLocal]] = {}
    if pages_needing_ocr:
        logger.info("  Running OCR on pages needing auto-detect: %s", sorted(pages_needing_ocr))
        for page_num in sorted(pages_needing_ocr):
            idx = page_num - 1
            if idx < len(page_images):
                tokens = run_ocr_on_image(page_images[idx])
                ocr_by_page[page_num] = tokens
                logger.info("  Page %d: %d OCR tokens", page_num, len(tokens))
    else:
        logger.info("  All fields have hardcoded ROIs — skipping OCR")

    # --- Compute features for sample page ---
    logger.info("  Computing pHash + ORB features for page %d...", content_pages[0])
    phash_val, kp_bytes, desc_bytes, width, height = compute_features(sample_image)

    # --- Resolve field ROIs (use hardcoded or auto-detect) ---
    logger.info("  Resolving field ROIs...")

    # Build set of all label words per page for boundary detection (used by auto-detect fallback)
    from collections import defaultdict
    import re as _re
    label_words_by_page: dict[int, set[str]] = defaultdict(set)
    for _fk, _fdef in field_defs.items():
        pg = _fdef["page"]
        for lbl in _fdef.get("labels", []):
            for w in _re.split(r'[\s/]+', lbl.lower()):
                cleaned = w.rstrip(":").rstrip("#").rstrip(")").lstrip("(").strip()
                if len(cleaned) >= 3:
                    label_words_by_page[pg].add(cleaned)

    field_rois: dict[str, dict] = {}
    for field_key, field_def in field_defs.items():
        page_num = field_def["page"]
        labels = field_def.get("labels", [field_def.get("label", field_key)])

        # Use hardcoded ROI if available (manually drawn coordinates)
        if "roi" in field_def:
            roi = field_def["roi"]
            logger.info(
                "    Using hardcoded ROI for '%s': (%.4f,%.4f)-(%.4f,%.4f)",
                field_key, roi["x0"], roi["y0"], roi["x1"], roi["y1"],
            )
            field_rois[field_key] = {
                "roi": roi,
                "page": page_num,
                "is_required": field_def.get("is_required", False),
                "expected_type": field_def.get("expected_type", "text"),
                "label": field_def.get("label", field_key),
                "labels": labels,
            }
            continue

        # Fallback: auto-detect ROI from OCR tokens
        page_tokens = ocr_by_page.get(page_num, [])
        if not page_tokens:
            logger.warning("    No OCR tokens for page %d — cannot find '%s'", page_num, field_key)
            continue

        own_words: set[str] = set()
        for lbl in labels:
            for w in _re.split(r'[\s/]+', lbl.lower()):
                cleaned = w.rstrip(":").rstrip("#").rstrip(")").lstrip("(").strip()
                if len(cleaned) >= 3:
                    own_words.add(cleaned)
        boundary_words = label_words_by_page[page_num] - own_words

        roi = find_field_roi(
            labels, page_tokens,
            search_right=field_def.get("search_right", True),
            search_below=field_def.get("search_below", True),
            value_max_tokens=field_def.get("value_max_tokens", 4),
            max_value_width=field_def.get("max_value_width", 0.30),
            all_label_words=boundary_words,
        )
        if roi:
            field_rois[field_key] = {
                "roi": roi,
                "page": page_num,
                "is_required": field_def.get("is_required", False),
                "expected_type": field_def.get("expected_type", "text"),
                "label": field_def.get("label", field_key),
                "labels": labels,
            }
        else:
            logger.warning("    Could NOT find ROI for '%s' (labels: %s)", field_key, labels)

    logger.info("  Resolved %d/%d field ROIs", len(field_rois), len(field_defs))

    if dry_run:
        logger.info("  [DRY RUN] Would create template: %s", template_name)
        for fk, fv in field_rois.items():
            r = fv["roi"]
            logger.info("    Field '%s': page=%d ROI=(%.3f,%.3f)-(%.3f,%.3f)",
                        fk, fv["page"], r["x0"], r["y0"], r["x1"], r["y1"])
        return True

    # --- Insert into DB ---
    logger.info("  Writing to database...")

    try:
        payer_enum = PayerNameEnum(payer_key)
    except ValueError:
        logger.error("  Invalid payer: %s", payer_key)
        return False

    try:
        doc_type_enum = DocTypeEnum(doc_type_key)
    except ValueError:
        logger.error("  Invalid doc_type: %s", doc_type_key)
        return False

    with get_db_session() as db:
        # Check if template already exists
        from sqlalchemy import select
        existing = db.execute(
            select(FaxTemplate).where(
                FaxTemplate.payer_name == payer_enum,
                FaxTemplate.doc_type == doc_type_enum,
                FaxTemplate.template_name == template_name,
            )
        ).scalar_one_or_none()

        if existing:
            logger.info("  Template already exists (id=%s) — replacing versions", existing.template_id)
            template = existing
            # Delete old versions (cascade deletes samples + fields)
            for v in list(template.versions):
                db.delete(v)
            db.flush()
        else:
            template = FaxTemplate(
                template_id=uuid4(),
                payer_name=payer_enum,
                doc_type=doc_type_enum,
                template_name=template_name,
                description=definition.get("description", ""),
                is_active=True,
                created_by="seed_templates.py",
            )
            db.add(template)
            db.flush()
            logger.info("  Created template: %s (id=%s)", template_name, template.template_id)

        # Create version (with template_config for rotation/content/cover pages)
        template_config = {
            "rotate_pages": {str(k): v for k, v in definition.get("rotate_pages", {}).items()},
            "content_pages": definition.get("content_pages", []),
            "cover_pages": definition.get("cover_pages", []),
        }
        version = FaxTemplateVersion(
            template_version_id=uuid4(),
            template_id=template.template_id,
            version_label="v1.0-seed",
            match_min_score=Decimal("0.60"),
            match_phash_threshold=6,
            match_orb_min_matches=15,
            template_config=template_config,
            is_active=True,
            activated_at=datetime.now(timezone.utc),
        )
        db.add(version)
        db.flush()

        # Store sample image in MinIO
        sample_key = f"templates/{payer_key}/{template.template_id}/sample_page{content_pages[0]}.png"
        try:
            from libs.shared.storage.s3_adapter import S3StorageAdapter
            storage = S3StorageAdapter()
            _, png_data = cv2.imencode(".png", sample_image)
            storage.upload(key=sample_key, data=png_data.tobytes(), content_type="image/png")
            logger.info("  Uploaded sample image: %s", sample_key)
        except Exception as e:
            logger.warning("  Could not upload to MinIO (%s) — using placeholder key", e)
            sample_key = f"templates/{payer_key}/sample_page{content_pages[0]}.png"

        # Create sample
        sample = FaxTemplateSample(
            sample_id=uuid4(),
            template_version_id=version.template_version_id,
            sample_storage_key=sample_key,
            phash_value=phash_val,
            orb_keypoints=kp_bytes,
            orb_descriptors=desc_bytes,
            width_px=width,
            height_px=height,
        )
        db.add(sample)

        # Load payer validation rules once
        field_validations = {}
        try:
            import yaml
            rules_path = PROJECT_ROOT / "configs" / "payer_rules.yml"
            if rules_path.exists():
                with open(rules_path) as f:
                    rules = yaml.safe_load(f)
                payer_rules = rules.get("payers", {}).get(payer_key, {})
                field_validations = payer_rules.get("field_validations", {})
        except Exception:
            pass

        # Create fields
        fields_created = 0
        for field_key, field_info in field_rois.items():
            roi = field_info["roi"]
            # Store label aliases in post_processing for smart label-anchored extraction
            post_proc = {}
            label_aliases = field_info.get("labels", [])
            if label_aliases:
                post_proc["label_aliases"] = label_aliases

            field = FaxTemplateField(
                template_field_id=uuid4(),
                template_version_id=version.template_version_id,
                field_key=field_key,
                field_label=field_info.get("label", field_key),
                is_required=field_info.get("is_required", False),
                roi_x0=Decimal(str(round(roi["x0"], 6))),
                roi_y0=Decimal(str(round(roi["y0"], 6))),
                roi_x1=Decimal(str(round(roi["x1"], 6))),
                roi_y1=Decimal(str(round(roi["y1"], 6))),
                target_page=field_info["page"],
                expected_type=field_info.get("expected_type", "text"),
                post_processing=post_proc,
            )

            # Add validation regex from payer_rules
            if field_key in field_validations:
                regex = field_validations[field_key].get("regex")
                if regex:
                    field.validation_regex = regex
                    field.validation_message = field_validations[field_key].get("description", "")

            db.add(field)
            fields_created += 1

        db.commit()
        logger.info(
            "  SUCCESS: template=%s, version=%s, sample=1, fields=%d",
            template.template_id, version.template_version_id, fields_created,
        )

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Seed templates from client PDFs")
    parser.add_argument(
        "--pdf",
        help="Specific PDF filename to seed (e.g., 'Anthem.pdf')",
    )
    parser.add_argument(
        "--dir",
        default=str(PROJECT_ROOT / "pdfs" / "Templetes_pdf"),
        help="Directory containing template PDFs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without writing to DB",
    )
    args = parser.parse_args()

    pdf_dir = Path(args.dir)
    if not pdf_dir.exists():
        logger.error("PDF directory not found: %s", pdf_dir)
        sys.exit(1)

    # Determine which PDFs to process
    if args.pdf:
        pdfs_to_process = {args.pdf: TEMPLATE_DEFINITIONS.get(args.pdf)}
        if not pdfs_to_process[args.pdf]:
            logger.error("No template definition for: %s", args.pdf)
            logger.info("Available: %s", ", ".join(TEMPLATE_DEFINITIONS.keys()))
            sys.exit(1)
    else:
        pdfs_to_process = TEMPLATE_DEFINITIONS

    logger.info("Template Seeding Script")
    logger.info("PDF directory: %s", pdf_dir)
    logger.info("PDFs to process: %d", len(pdfs_to_process))
    if args.dry_run:
        logger.info("MODE: DRY RUN (no DB writes)")
    logger.info("")

    success = 0
    failed = 0

    for pdf_name, definition in pdfs_to_process.items():
        pdf_path = pdf_dir / pdf_name
        if not pdf_path.exists():
            logger.warning("PDF not found: %s — skipping", pdf_path)
            failed += 1
            continue

        try:
            ok = seed_one_template(str(pdf_path), definition, dry_run=args.dry_run)
            if ok:
                success += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("FAILED to process %s: %s", pdf_name, e, exc_info=True)
            failed += 1

    logger.info("")
    logger.info("=" * 70)
    logger.info("SUMMARY: %d success, %d failed out of %d total",
                success, failed, len(pdfs_to_process))

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
