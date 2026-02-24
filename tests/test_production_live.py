"""
Production Live Test Suite — Healthcare Fax OCR System
=======================================================
Tests every endpoint, edge case, and workflow against running containers.
Run with:  python tests/test_production_live.py
"""

import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

# Force UTF-8 stdout on Windows (prevents charmap codec errors with non-ASCII chars in API output)
try:
    if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer") and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

if os.getenv("RUN_PRODUCTION_LIVE_TESTS", "").lower() not in {"1", "true", "yes"}:
    pytest.skip(
        "Production live tests are disabled. Set RUN_PRODUCTION_LIVE_TESTS=1 to enable.",
        allow_module_level=True,
    )

requests = pytest.importorskip("requests")

# ── Base URLs ──────────────────────────────────────────────────────────────
INGRESS = "http://localhost:8001"
REVIEW  = "http://localhost:8002"
QUERY   = "http://localhost:8003"

# ── Test PDFs ──────────────────────────────────────────────────────────────
PDF_DIR   = Path(__file__).parent.parent / "pdfs" / "Client_response_for_templete"
TMPL_DIR  = Path(__file__).parent.parent / "pdfs" / "Templetes_pdf"
TEST_PDFS = sorted(PDF_DIR.glob("*.pdf"))

TENANT = "test-tenant"

# ── Result tracking ────────────────────────────────────────────────────────
PASS   = []
FAIL   = []
WARN   = []


def _safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except (OSError, IOError):
        # Broken pipe (e.g. stdout truncated by head -N) — write to stderr
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:
            pass


def ok(name, detail=""):
    PASS.append(name)
    _safe_print(f"  [PASS]  {name}" + (f" -- {detail}" if detail else ""))


def fail(name, detail=""):
    FAIL.append(name)
    _safe_print(f"  [FAIL]  {name}" + (f" -- {detail}" if detail else ""))


def warn(name, detail=""):
    WARN.append(name)
    _safe_print(f"  [WARN]  {name}" + (f" -- {detail}" if detail else ""))


def section(title):
    _safe_print(f"\n{'='*60}")
    _safe_print(f"  {title}")
    _safe_print(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. HEALTH CHECKS
# ══════════════════════════════════════════════════════════════════════════════
def test_health():
    section("1. HEALTH CHECKS")

    for name, url in [("Ingress :8001", f"{INGRESS}/health"),
                      ("Review  :8002", f"{REVIEW}/health"),
                      ("Query   :8003", f"{QUERY}/health")]:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                body = r.json()
                status = body.get("status", "unknown")
                if status == "healthy":
                    ok(f"Health {name}", f"status={status}")
                else:
                    warn(f"Health {name}", f"status={status} - {body}")
            else:
                fail(f"Health {name}", f"HTTP {r.status_code}")
        except Exception as e:
            fail(f"Health {name}", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 2. DOCS PAGES LOAD
# ══════════════════════════════════════════════════════════════════════════════
def test_docs_pages():
    section("2. SWAGGER UI /docs PAGES")

    for name, url in [("Ingress /docs", f"{INGRESS}/docs"),
                      ("Review  /docs", f"{REVIEW}/docs"),
                      ("Query   /docs", f"{QUERY}/docs")]:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200 and "swagger" in r.text.lower():
                ok(name, "page loaded with Swagger content")
            elif r.status_code == 200:
                warn(name, "200 but swagger keyword not in body")
            else:
                fail(name, f"HTTP {r.status_code}")
        except Exception as e:
            fail(name, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 3. OPENAPI SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════
def test_openapi_schemas():
    section("3. OPENAPI SCHEMAS")

    for name, url in [("Ingress openapi.json", f"{INGRESS}/openapi.json"),
                      ("Review  openapi.json", f"{REVIEW}/openapi.json"),
                      ("Query   openapi.json", f"{QUERY}/openapi.json")]:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                schema = r.json()
                path_count = len(schema.get("paths", {}))
                ok(name, f"{path_count} paths")
            else:
                fail(name, f"HTTP {r.status_code}")
        except Exception as e:
            fail(name, str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 4. UPLOAD EDGE CASES — ERROR HANDLING
# ══════════════════════════════════════════════════════════════════════════════
def test_upload_edge_cases():
    section("4. UPLOAD EDGE CASES")

    # 4a. Non-PDF file (should be rejected)
    try:
        r = requests.post(
            f"{INGRESS}/v1/faxes/upload",
            files={"file": ("test.txt", b"this is not a pdf", "text/plain")},
            data={"tenant_id": TENANT},
            timeout=30,
        )
        if r.status_code in (400, 415, 422):
            ok("Reject non-PDF file", f"HTTP {r.status_code} as expected")
        else:
            fail("Reject non-PDF file", f"Expected 400/422 got {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("Reject non-PDF file", str(e))

    # 4b. Missing tenant_id
    if TEST_PDFS:
        try:
            with open(TEST_PDFS[0], "rb") as f:
                r = requests.post(
                    f"{INGRESS}/v1/faxes/upload",
                    files={"file": (TEST_PDFS[0].name, f, "application/pdf")},
                    timeout=30,
                )
            if r.status_code in (400, 422):
                ok("Reject missing tenant_id", f"HTTP {r.status_code} as expected")
            else:
                warn("Reject missing tenant_id", f"Got {r.status_code}: {r.text[:100]}")
        except Exception as e:
            fail("Reject missing tenant_id", str(e))

    # 4c. Empty PDF body
    try:
        r = requests.post(
            f"{INGRESS}/v1/faxes/upload",
            files={"file": ("empty.pdf", b"", "application/pdf")},
            data={"tenant_id": TENANT},
            timeout=30,
        )
        if r.status_code in (400, 415, 422):
            ok("Reject empty file", f"HTTP {r.status_code} as expected")
        else:
            warn("Reject empty file", f"Got {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("Reject empty file", str(e))

    # 4d. Fake PDF (PDF header, garbage body) — system accepts at upload, fails in worker (correct behavior)
    try:
        r = requests.post(
            f"{INGRESS}/v1/faxes/upload",
            files={"file": ("fake.pdf", b"%PDF-1.4 corrupted garbage", "application/pdf")},
            data={"tenant_id": TENANT},
            timeout=30,
        )
        if r.status_code in (200, 201, 202, 400, 422):
            ok("Fake PDF handled", f"HTTP {r.status_code} (accepted or rejected gracefully)")
        else:
            warn("Fake PDF handled", f"Got {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("Fake PDF handled", str(e))

    # 4e. GET on unknown job ID
    try:
        bad_id = str(uuid.uuid4())
        r = requests.get(f"{INGRESS}/v1/faxes/{bad_id}", timeout=15)
        if r.status_code == 404:
            ok("Unknown job ID returns 404", f"job={bad_id}")
        else:
            fail("Unknown job ID returns 404", f"Got {r.status_code}")
    except Exception as e:
        fail("Unknown job ID returns 404", str(e))

    # 4f. Malformed UUID
    try:
        r = requests.get(f"{INGRESS}/v1/faxes/not-a-uuid", timeout=15)
        if r.status_code in (400, 404, 422):
            ok("Malformed UUID returns 4xx", f"HTTP {r.status_code}")
        else:
            fail("Malformed UUID returns 4xx", f"Got {r.status_code}")
    except Exception as e:
        fail("Malformed UUID returns 4xx", str(e))


def _upload_pdf(pdf_path: Path) -> dict | None:
    """Upload a PDF, handle rate limiting with one retry. Returns response body or None."""
    for attempt in range(2):
        try:
            with open(pdf_path, "rb") as f:
                r = requests.post(
                    f"{INGRESS}/v1/faxes/upload",
                    files={"file": (pdf_path.name, f, "application/pdf")},
                    data={"tenant_id": TENANT},
                    timeout=30,
                )
            if r.status_code in (200, 201, 202):
                return r.json()
            elif r.status_code == 429 and attempt == 0:
                print(f"    Rate limit hit, waiting 10s and retrying...")
                time.sleep(10)
                continue
            else:
                print(f"    Upload HTTP {r.status_code}: {r.text[:150]}")
                return None
        except Exception as e:
            print(f"    Upload exception: {e}")
            return None
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 5. UPLOAD REAL PDFS + FULL PIPELINE
# ══════════════════════════════════════════════════════════════════════════════
def test_full_pipeline():
    section("5. FULL PROCESSING PIPELINE - REAL PDFs")

    if not TEST_PDFS:
        warn("No test PDFs found", str(PDF_DIR))
        return []

    job_ids = []

    for i, pdf_path in enumerate(TEST_PDFS):
        # Small delay between uploads to avoid triggering rate limiter (token-bucket)
        if i > 0:
            time.sleep(3)

        print(f"\n  Uploading: {pdf_path.name}")
        try:
            upload_resp = _upload_pdf(pdf_path)
            if upload_resp is None:
                fail(f"Upload {pdf_path.name}", "Upload request failed")
                continue

            body = upload_resp
            job_id = body.get("fax_job_id")
            status = body.get("status")

            # If dedup returned a FAILED or stuck PROCESSING job, delete it and re-upload for fresh processing
            if status in ("FAILED", "PROCESSING") and job_id:
                print(f"    [dedup returned {status} job {job_id}] deleting and re-uploading...")
                del_r = requests.delete(f"{INGRESS}/v1/faxes/{job_id}", timeout=30)
                if del_r.status_code in (200, 204):
                    time.sleep(2)
                    body = _upload_pdf(pdf_path)
                    if body is None:
                        fail(f"Upload {pdf_path.name}", "Re-upload after delete failed")
                        continue
                    job_id = body.get("fax_job_id")
                    status = body.get("status")
                    ok(f"Upload {pdf_path.name}", f"job_id={job_id} status={status} (fresh)")
                else:
                    ok(f"Upload {pdf_path.name}", f"job_id={job_id} status={status} (delete failed: {del_r.status_code})")
            else:
                ok(f"Upload {pdf_path.name}", f"job_id={job_id} status={status}")

            if job_id:
                job_ids.append((job_id, pdf_path.name, status or "PENDING"))

        except Exception as e:
            fail(f"Upload {pdf_path.name}", str(e))

    # ── Poll for completion ────────────────────────────────────────────────
    section("5b. POLLING UNTIL COMPLETE (max 3 min each)")

    completed_jobs = []

    for job_id, filename, upload_status in job_ids:
        # If upload response already shows a terminal state (deduped job), use it directly
        if upload_status == "COMPLETED":
            ok(f"Pipeline {filename}", f"status=COMPLETED (deduped/cached)")
            completed_jobs.append((job_id, filename, "COMPLETED"))
            continue
        elif upload_status == "FAILED":
            fail(f"Pipeline {filename}", "status=FAILED (deduped/cached)")
            continue

        print(f"\n  Polling: {filename} ({job_id})")
        deadline = time.time() + 480  # 8 min — solo pool processes jobs sequentially, later jobs wait longer
        last_status = None
        consecutive_errors = 0

        while time.time() < deadline:
            try:
                r = requests.get(f"{INGRESS}/v1/faxes/{job_id}", timeout=20)
                consecutive_errors = 0  # reset on success
                if r.status_code == 200:
                    body = r.json()
                    status = body.get("status")
                    payer = body.get("payer_hint", body.get("payer", "?"))
                    conf  = body.get("overall_conf", body.get("overall_confidence", "?"))

                    if status != last_status:
                        print(f"    -> {status} | payer={payer} | conf={conf}")
                        last_status = status

                    if status in ("COMPLETED", "NEEDS_REVIEW"):
                        ok(f"Pipeline {filename}", f"status={status} payer={payer} conf={conf}")
                        completed_jobs.append((job_id, filename, status))
                        break
                    elif status == "FAILED":
                        fail(f"Pipeline {filename}", "status=FAILED")
                        break
                else:
                    fail(f"Polling {filename}", f"HTTP {r.status_code}")
                    break
            except requests.exceptions.Timeout:
                consecutive_errors += 1
                print(f"    [timeout #{consecutive_errors} - retrying]")
                if consecutive_errors >= 5:
                    fail(f"Polling {filename}", "5 consecutive timeouts")
                    break
                # Don't sleep extra - just retry immediately on timeout
                continue
            except Exception as e:
                fail(f"Polling {filename}", str(e))
                break

            time.sleep(10)
        else:
            warn(f"Pipeline {filename}", f"Timed out after 5 min. last={last_status}")

    return completed_jobs


# ══════════════════════════════════════════════════════════════════════════════
# 6. RESULTS ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════
def test_results(completed_jobs):
    section("6. EXTRACTION RESULTS")

    for job_id, filename, job_status in completed_jobs:
        try:
            r = requests.get(f"{INGRESS}/v1/faxes/{job_id}/results", timeout=30)
            if r.status_code == 200:
                body = r.json()
                payer    = body.get("payer_hint", body.get("payer", "UNKNOWN"))
                doc_type = body.get("doc_type", "UNKNOWN")
                conf     = body.get("overall_conf", body.get("overall_confidence", 0))
                fields   = body.get("fields", {})
                summary  = body.get("summary", {})

                found   = summary.get("fields_found", 0)
                total   = summary.get("total_fields", 0)
                flagged = summary.get("fields_flagged_for_review", 0)

                ok(f"Results {filename}",
                   f"payer={payer} doc_type={doc_type} conf={conf:.2f} fields={found}/{total} flagged={flagged}")

                # Validate response structure
                if not isinstance(fields, dict):
                    fail(f"Results structure {filename}", "fields is not a dict")
                else:
                    for key, field in fields.items():
                        if "value" not in field:
                            fail(f"Results field {key}", "missing 'value' key")
                        if "confidence" not in field:
                            fail(f"Results field {key}", "missing 'confidence' key")
                        if "source" not in field:
                            fail(f"Results field {key}", "missing 'source' key")

                # Check at least some fields were extracted
                non_null = sum(1 for f in fields.values() if f.get("value") is not None)
                if non_null == 0:
                    fail(f"Results {filename}", "ZERO fields extracted!")
                elif non_null < 3:
                    warn(f"Results {filename}", f"Only {non_null} fields found")

            elif r.status_code == 404:
                warn(f"Results {filename}", "404 - still processing or failed")
            else:
                fail(f"Results {filename}", f"HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            fail(f"Results {filename}", str(e))

    # Edge case: results on unknown job
    try:
        r = requests.get(f"{INGRESS}/v1/faxes/{uuid.uuid4()}/results", timeout=15)
        if r.status_code == 404:
            ok("Results unknown job -> 404")
        else:
            fail("Results unknown job -> 404", f"Got {r.status_code}")
    except Exception as e:
        fail("Results unknown job -> 404", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 7. OCR ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════
def test_ocr_endpoint(completed_jobs):
    section("7. RAW OCR ENDPOINT")

    if not completed_jobs:
        warn("OCR endpoint", "No completed jobs to test against")
        return

    job_id, filename, _ = completed_jobs[0]
    try:
        r = requests.get(f"{INGRESS}/v1/faxes/{job_id}/ocr", timeout=30)
        if r.status_code == 200:
            body = r.json()
            pages = body.get("pages", [])
            if pages:
                token_count = sum(len(p.get("tokens", [])) for p in pages)
                ok("OCR endpoint", f"pages={len(pages)} tokens={token_count}")
                # Check each page has full_text
                for p in pages:
                    if not p.get("full_text"):
                        warn("OCR page full_text", f"page {p.get('page_number')} has empty full_text")
            else:
                warn("OCR endpoint", "No pages in response")
        else:
            fail("OCR endpoint", f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("OCR endpoint", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 8. LIST FAXES
# ══════════════════════════════════════════════════════════════════════════════
def test_list_faxes():
    section("8. LIST FAXES")

    # Basic list
    try:
        r = requests.get(f"{INGRESS}/v1/faxes/", timeout=30)
        if r.status_code == 200:
            body = r.json()
            items = body.get("items", body if isinstance(body, list) else [])
            ok("List faxes", f"returned {len(items)} items")
        else:
            fail("List faxes", f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("List faxes", str(e))

    # Filter by status
    for status_val in ["COMPLETED", "NEEDS_REVIEW", "FAILED", "PENDING"]:
        try:
            r = requests.get(f"{INGRESS}/v1/faxes/?status={status_val}&limit=5", timeout=30)
            if r.status_code == 200:
                body = r.json()
                count = len(body.get("items", body if isinstance(body, list) else []))
                ok(f"List filter status={status_val}", f"{count} results")
            else:
                fail(f"List filter status={status_val}", f"HTTP {r.status_code}")
        except Exception as e:
            fail(f"List filter status={status_val}", str(e))

    # Pagination
    try:
        r = requests.get(f"{INGRESS}/v1/faxes/?limit=2&offset=0", timeout=30)
        if r.status_code == 200:
            ok("List pagination (limit=2)", "OK")
        else:
            fail("List pagination", f"HTTP {r.status_code}")
    except Exception as e:
        fail("List pagination", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 9. REVIEW WORKFLOW
# ══════════════════════════════════════════════════════════════════════════════
def test_review_workflow(completed_jobs):
    section("9. REVIEW WORKFLOW (END TO END)")

    # 9a. List pending reviews
    try:
        r = requests.get(f"{REVIEW}/v1/faxes/reviews/pending", timeout=30)
        if r.status_code == 200:
            items = r.json()
            ok("GET /reviews/pending", f"{len(items)} items in queue")
            pending_items = items
        else:
            fail("GET /reviews/pending", f"HTTP {r.status_code}: {r.text[:100]}")
            pending_items = []
    except Exception as e:
        fail("GET /reviews/pending", str(e))
        pending_items = []

    # 9b. List unclaimed
    try:
        r = requests.get(f"{REVIEW}/v1/faxes/reviews/unclaimed", timeout=30)
        if r.status_code == 200:
            items = r.json()
            ok("GET /reviews/unclaimed", f"{len(items)} unclaimed")
        else:
            fail("GET /reviews/unclaimed", f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("GET /reviews/unclaimed", str(e))

    # Find a NEEDS_REVIEW job
    review_job_id = None
    for job_id, filename, status in completed_jobs:
        if status == "NEEDS_REVIEW":
            review_job_id = job_id
            review_filename = filename
            break

    if not review_job_id:
        # Try to get one from pending queue
        if pending_items:
            review_job_id = pending_items[0].get("fax_job_id")
            review_filename = "from-queue"

    if not review_job_id:
        warn("Review workflow", "No NEEDS_REVIEW jobs found to test claim/packet/submit flow")
        return

    print(f"\n  Testing review flow on: {review_job_id} ({review_filename})")

    # 9c. Claim the document
    reviewer_id = "test-reviewer-pm"
    try:
        r = requests.post(
            f"{REVIEW}/v1/faxes/{review_job_id}/review/claim",
            json={"reviewer_id": reviewer_id},
            timeout=30,
        )
        if r.status_code == 200:
            body = r.json()
            ok("POST /review/claim",
               f"claimed_by={body.get('claimed_by')} expires={body.get('expires_at')}")
        elif r.status_code == 409:
            warn("POST /review/claim", "Already claimed - OK for conflict test")
        else:
            fail("POST /review/claim", f"HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        fail("POST /review/claim", str(e))

    # 9d. Get review packet
    try:
        r = requests.get(f"{REVIEW}/v1/faxes/{review_job_id}/review-packet", timeout=15)
        if r.status_code == 200:
            body = r.json()
            pages         = body.get("pages", [])
            fields        = body.get("extracted_fields", [])
            flagged       = body.get("flagged_fields", [])
            reasons       = body.get("review_reasons", [])
            template_info = body.get("template_match", {})

            ok("GET /review-packet",
               f"pages={len(pages)} fields={len(fields)} flagged={len(flagged)}")

            # Validate page image URLs
            for page in pages:
                url = page.get("image_url", "")
                if url.startswith("http"):
                    ok(f"  Page {page['page_number']} image URL", url[:60] + "...")
                else:
                    fail(f"  Page {page['page_number']} image URL", f"Bad URL: {url[:60]}")

            # Validate flagged fields structure
            for ff in flagged:
                if "field_key" not in ff or "flag_reason" not in ff:
                    fail("Flagged field structure", f"Missing keys: {ff}")
                else:
                    ok(f"  Flagged: {ff['field_key']}", f"reason={ff['flag_reason']}")

            # Check template match info
            if template_info:
                ok("  Template match info", f"payer={template_info.get('payer')} score={template_info.get('score')}")

        elif r.status_code == 404:
            warn("GET /review-packet", "404 - no review record exists for this job")
        else:
            fail("GET /review-packet", f"HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        fail("GET /review-packet", str(e))

    # 9e. Submit corrections
    try:
        # Submit a no-op correction to verify the endpoint
        r = requests.post(
            f"{REVIEW}/v1/faxes/{review_job_id}/review/submit",
            json={
                "reviewer_id": reviewer_id,
                "corrected_fields": []
            },
            timeout=15,
        )
        if r.status_code == 200:
            body = r.json()
            ok("POST /review/submit (empty corrections)",
               f"submitted_by={body.get('submitted_by')} corrections={body.get('corrections_count')}")
        else:
            fail("POST /review/submit", f"HTTP {r.status_code}: {r.text[:150]}")
    except Exception as e:
        fail("POST /review/submit", str(e))

    # 9f. Edge case — claim unknown job
    try:
        r = requests.post(
            f"{REVIEW}/v1/faxes/{uuid.uuid4()}/review/claim",
            json={"reviewer_id": "tester"},
            timeout=30,
        )
        if r.status_code in (404, 400):
            ok("Claim unknown job -> 404", f"HTTP {r.status_code}")
        else:
            fail("Claim unknown job -> 404", f"Got {r.status_code}")
    except Exception as e:
        fail("Claim unknown job -> 404", str(e))

    # 9g. Packet for unknown job
    try:
        r = requests.get(f"{REVIEW}/v1/faxes/{uuid.uuid4()}/review-packet", timeout=30)
        if r.status_code == 404:
            ok("Packet unknown job -> 404", f"HTTP {r.status_code}")
        else:
            fail("Packet unknown job -> 404", f"Got {r.status_code}")
    except Exception as e:
        fail("Packet unknown job -> 404", str(e))

    # 9h. Release expired claims
    try:
        r = requests.post(f"{REVIEW}/v1/faxes/reviews/release-expired", timeout=30)
        if r.status_code == 200:
            ok("POST /reviews/release-expired", f"released={r.json().get('released', '?')}")
        else:
            fail("POST /reviews/release-expired", f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("POST /reviews/release-expired", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 10. TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════
def test_templates():
    section("10. TEMPLATE ADMIN ENDPOINTS")

    # List templates
    try:
        r = requests.get(f"{REVIEW}/v1/templates/", timeout=30)
        if r.status_code == 200:
            items = r.json()
            ok("GET /templates/", f"{len(items)} templates")
            if len(items) < 8:
                warn("Template count", f"Expected 8 payer templates, got {len(items)}")
            for t in items:
                ok(f"  Template: {t.get('payer_name','?')}",
                   f"fields={t.get('field_count','?')}")
        else:
            fail("GET /templates/", f"HTTP {r.status_code}: {r.text[:100]}")
            return
    except Exception as e:
        fail("GET /templates/", str(e))
        return

    # Get single template
    if items:
        tmpl_id = items[0].get("template_id")
        try:
            r = requests.get(f"{REVIEW}/v1/templates/{tmpl_id}", timeout=30)
            if r.status_code == 200:
                ok("GET /templates/{id}", f"template_id={tmpl_id}")
            else:
                fail("GET /templates/{id}", f"HTTP {r.status_code}")
        except Exception as e:
            fail("GET /templates/{id}", str(e))

    # Unknown template
    try:
        r = requests.get(f"{REVIEW}/v1/templates/{uuid.uuid4()}", timeout=30)
        if r.status_code == 404:
            ok("GET /templates/unknown -> 404")
        else:
            fail("GET /templates/unknown -> 404", f"Got {r.status_code}")
    except Exception as e:
        fail("GET /templates/unknown -> 404", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 11. ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════
def test_analytics():
    section("11. ANALYTICS ENDPOINTS")

    endpoints = [
        ("GET /analytics/quality",          f"{REVIEW}/v1/analytics/quality"),
        ("GET /analytics/payers",           f"{REVIEW}/v1/analytics/payers"),
        ("GET /analytics/feedback-summary", f"{REVIEW}/v1/analytics/feedback-summary"),
    ]

    for name, url in endpoints:
        try:
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                ok(name, str(r.json())[:80])
            else:
                fail(name, f"HTTP {r.status_code}: {r.text[:80]}")
        except Exception as e:
            fail(name, str(e))

    # Per-payer analytics
    for payer in ["HUMANA", "CARESOURCE", "ANTHEM", "INVALID_PAYER"]:
        try:
            r = requests.get(f"{REVIEW}/v1/analytics/payer/{payer}", timeout=30)
            if payer == "INVALID_PAYER":
                if r.status_code in (404, 400, 200):
                    ok(f"Analytics payer=INVALID_PAYER", f"HTTP {r.status_code} handled gracefully")
                else:
                    fail(f"Analytics payer=INVALID_PAYER", f"HTTP {r.status_code}")
            elif r.status_code == 200:
                ok(f"Analytics payer={payer}", str(r.json())[:60])
            else:
                warn(f"Analytics payer={payer}", f"HTTP {r.status_code}")
        except Exception as e:
            fail(f"Analytics payer={payer}", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 12. MODEL VERSIONS
# ══════════════════════════════════════════════════════════════════════════════
def test_model_versions():
    section("12. MODEL VERSION ENDPOINTS")

    # List models
    try:
        r = requests.get(f"{REVIEW}/v1/models/", timeout=30)
        if r.status_code == 200:
            items = r.json()
            ok("GET /models/", f"{len(items)} registered models")
            for m in items:
                ok(f"  Model: {m.get('model_type','?')}",
                   f"version={m.get('version','?')} active={m.get('is_active','?')}")
        else:
            fail("GET /models/", f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        fail("GET /models/", str(e))

    # Active model by type
    for model_type in ["LAYOUTLM", "PADDLEOCR"]:
        try:
            r = requests.get(f"{REVIEW}/v1/models/active/{model_type}", timeout=30)
            if r.status_code == 200:
                ok(f"GET /models/active/{model_type}", r.json().get("version", "?"))
            elif r.status_code == 404:
                warn(f"GET /models/active/{model_type}", "No active version registered yet")
            else:
                fail(f"GET /models/active/{model_type}", f"HTTP {r.status_code}")
        except Exception as e:
            fail(f"GET /models/active/{model_type}", str(e))

    # Promote unknown model — should 404
    try:
        r = requests.post(
            f"{REVIEW}/v1/models/{uuid.uuid4()}/promote",
            json={"promoted_by": "test-runner"},
            timeout=30,
        )
        if r.status_code in (404, 400):
            ok("Promote unknown model -> 404", f"HTTP {r.status_code}")
        else:
            fail("Promote unknown model -> 404", f"Got {r.status_code}")
    except Exception as e:
        fail("Promote unknown model -> 404", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 13. QUERY API
# ══════════════════════════════════════════════════════════════════════════════
def test_query_api(completed_jobs):
    section("13. QUERY API")

    # 13a. Tier 1 and Tier 2 queries
    # Tier-2 (semantic) uses sentence-transformers; first call may be slow if not warmed up
    for query_text, tier, name in [
        ("member_id",          1, "Tier1 field key search"),
        ("DENIED",             1, "Tier1 decision=DENIED search"),
        ("H2036",              1, "Tier1 service_code search"),
        ("prior authorization denied", 2, "Tier2 semantic search"),
        ("Humana approval",    2, "Tier2 payer semantic"),
    ]:
        req_timeout = 90 if tier == 2 else 15
        try:
            r = requests.post(
                f"{QUERY}/v1/query",
                json={"query": query_text, "tenant_id": TENANT, "tier": tier, "limit": 5},
                timeout=req_timeout,
            )
            if r.status_code == 200:
                body = r.json()
                results = body.get("results", [])
                t_ms    = body.get("latency_ms", body.get("processing_time_ms", "?"))
                ok(f"Query {name}", f"results={len(results)} time={t_ms}ms")
            else:
                fail(f"Query {name}", f"HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            fail(f"Query {name}", str(e))

    # 13b. Empty query string
    try:
        r = requests.post(
            f"{QUERY}/v1/query",
            json={"query": "", "tenant_id": TENANT},
            timeout=30,
        )
        if r.status_code in (400, 422):
            ok("Empty query -> 422", f"HTTP {r.status_code}")
        else:
            warn("Empty query -> 422", f"Got {r.status_code}")
    except Exception as e:
        fail("Empty query -> 422", str(e))

    # 13c. Limit boundary
    try:
        r = requests.post(
            f"{QUERY}/v1/query",
            json={"query": "auth", "tier": 1, "limit": 100},
            timeout=15,
        )
        if r.status_code == 200:
            ok("Query limit=100", f"{len(r.json().get('results',[]))} results")
        else:
            fail("Query limit=100", f"HTTP {r.status_code}")
    except Exception as e:
        fail("Query limit=100", str(e))

    # 13d. Specific job query
    if completed_jobs:
        job_id = completed_jobs[0][0]
        try:
            r = requests.post(
                f"{QUERY}/v1/query",
                json={"query": "member", "tier": 1, "fax_job_id": job_id, "limit": 10},
                timeout=15,
            )
            if r.status_code == 200:
                ok("Query scoped to fax_job_id", f"results={len(r.json().get('results',[]))}")
            else:
                fail("Query scoped to fax_job_id", f"HTTP {r.status_code}")
        except Exception as e:
            fail("Query scoped to fax_job_id", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 14. DELETE ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════
def test_delete(completed_jobs):
    section("14. DELETE ENDPOINT")

    # Delete an unknown job — should 404
    try:
        r = requests.delete(f"{INGRESS}/v1/faxes/{uuid.uuid4()}", timeout=30)
        if r.status_code in (404, 204):
            ok("DELETE unknown job", f"HTTP {r.status_code}")
        else:
            fail("DELETE unknown job", f"Got {r.status_code}")
    except Exception as e:
        fail("DELETE unknown job", str(e))

    # Upload a throwaway unique PDF just for the delete test.
    # Using a unique timestamp ensures a fresh job (no SHA-256 dedup) that we
    # can safely delete without disrupting any cached production jobs.
    try:
        unique_pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n" + str(time.time_ns()).encode() + b"\n%%EOF"
        r_up = requests.post(
            f"{INGRESS}/v1/faxes/upload",
            files={"file": ("delete_test.pdf", unique_pdf, "application/pdf")},
            data={"tenant_id": TENANT},
            timeout=30,
        )
        if r_up.status_code in (200, 201, 202):
            throwaway_id = r_up.json().get("job_id") or r_up.json().get("fax_job_id")
            if throwaway_id:
                r = requests.delete(f"{INGRESS}/v1/faxes/{throwaway_id}", timeout=30)
                if r.status_code in (200, 204):
                    ok("DELETE throwaway job", f"HTTP {r.status_code}")
                    r2 = requests.get(f"{INGRESS}/v1/faxes/{throwaway_id}", timeout=15)
                    if r2.status_code == 404:
                        ok("Deleted job returns 404", "confirmed deleted")
                    else:
                        fail("Deleted job returns 404", f"Still returns {r2.status_code}")
                else:
                    fail("DELETE throwaway job", f"HTTP {r.status_code}: {r.text[:100]}")
            else:
                warn("DELETE throwaway job", "No job_id in upload response")
        else:
            warn("DELETE throwaway job", f"Upload returned {r_up.status_code}")
    except Exception as e:
        fail("DELETE throwaway job", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 15. SECURITY HEADERS
# ══════════════════════════════════════════════════════════════════════════════
def test_security_headers():
    section("15. SECURITY HEADERS")

    for name, url in [("Ingress", f"{INGRESS}/health"),
                      ("Review",  f"{REVIEW}/health"),
                      ("Query",   f"{QUERY}/health")]:
        try:
            r = requests.get(url, timeout=15)
            headers = r.headers

            checks = {
                "X-Content-Type-Options":  "nosniff",
                "X-Frame-Options":         "DENY",
                "Strict-Transport-Security": None,  # just check presence
            }
            all_ok = True
            for h, expected in checks.items():
                val = headers.get(h)
                if val is None:
                    warn(f"{name} header {h}", "MISSING")
                    all_ok = False
                elif expected and expected not in val:
                    warn(f"{name} header {h}", f"Expected '{expected}' got '{val}'")
                    all_ok = False
                else:
                    ok(f"{name} {h}", val[:50])
        except Exception as e:
            fail(f"Security headers {name}", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 16. RATE LIMITING (light check — don't hammer the server)
# ══════════════════════════════════════════════════════════════════════════════
def test_rate_limit_presence():
    section("16. RATE LIMIT HEADERS")
    try:
        r = requests.get(f"{INGRESS}/health", timeout=15)
        rl_headers = [h for h in r.headers if "rate" in h.lower() or "retry" in h.lower() or "limit" in h.lower()]
        if rl_headers:
            ok("Rate limit headers present", str(rl_headers))
        else:
            warn("Rate limit headers", "No rate-limit headers on health response (token-bucket may be internal only)")
    except Exception as e:
        fail("Rate limit check", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# PRINT FINAL REPORT
# ══════════════════════════════════════════════════════════════════════════════
def print_report():
    total  = len(PASS) + len(FAIL) + len(WARN)
    print(f"\n{'='*60}")
    print(f"  PRODUCTION TEST REPORT")
    print(f"{'='*60}")
    print(f"  TOTAL : {total}")
    print(f"  PASS: {len(PASS)}")
    print(f"  FAIL: {len(FAIL)}")
    print(f"  WARN: {len(WARN)}")

    if FAIL:
        print(f"\n  FAILURES:")
        for f in FAIL:
            print(f"    [FAIL] {f}")

    if WARN:
        print(f"\n  WARNINGS:")
        for w in WARN:
            print(f"    [WARN] {w}")

    print(f"\n{'='*60}")
    verdict = "ALL TESTS PASSED" if not FAIL else f"{len(FAIL)} FAILURES -- SEE ABOVE"
    print(f"  {verdict}")
    print(f"{'='*60}\n")

    return len(FAIL)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("\nHealthcare Fax OCR -- Production Live Test Suite")
    print(f"    Ingress: {INGRESS}")
    print(f"    Review:  {REVIEW}")
    print(f"    Query:   {QUERY}")
    print(f"    PDFs:    {len(TEST_PDFS)} test files found\n")

    test_health()
    test_docs_pages()
    test_openapi_schemas()
    test_upload_edge_cases()
    completed_jobs = test_full_pipeline()
    test_results(completed_jobs)
    test_ocr_endpoint(completed_jobs)
    test_list_faxes()
    test_review_workflow(completed_jobs)
    test_templates()
    test_analytics()
    test_model_versions()
    test_query_api(completed_jobs)
    test_delete(completed_jobs)
    test_security_headers()
    test_rate_limit_presence()

    failures = print_report()
    sys.exit(failures)
