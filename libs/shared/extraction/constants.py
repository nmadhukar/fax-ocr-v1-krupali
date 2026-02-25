"""
Canonical field definitions shared across the extraction pipeline.
"""

# Critical fields: wrong value has direct patient-safety / billing impact.
# This is the single source of truth — all modules should import from here.
CRITICAL_FIELDS: frozenset[str] = frozenset({
    "patient_name",
    "patient_dob",
    "member_id",
    "prior_auth_number",
    "auth_effective_date",
    "auth_expiration_date",
    "decision",
    "service_code",
    "diagnosis_codes",
})
