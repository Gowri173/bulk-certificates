"""Tests for certificate PDF download and job ZIP archive download."""

import io
import uuid
import zipfile
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.enums import CertificateStatus
from app.models import Certificate, Job


def test_download_single_certificate_success(
    client: TestClient, db_session: Session
) -> None:
    """Downloading a successful certificate returns 200, application/pdf, and valid PDF bytes."""
    payload = {
        "event_name": "Download Test Event",
        "recipients": [{"name": "Sara Connor", "email": "sara@example.com"}],
    }
    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    job_id = create_resp.json()["job_id"]

    cert = db_session.query(Certificate).filter(Certificate.job_id == job_id).first()
    assert cert is not None
    assert cert.status == CertificateStatus.SUCCESS

    # Download certificate
    resp = client.get(f"/api/v1/certificates/{cert.id}/download")
    assert resp.status_code == 200
    assert "application/pdf" in resp.headers["content-type"]
    assert f'filename="{cert.serial_number}.pdf"' in resp.headers["content-disposition"]

    # Verify content is valid PDF
    pdf_bytes = resp.content
    assert pdf_bytes.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1


def test_download_single_certificate_not_ready_or_failed_returns_409(
    client: TestClient, db_session: Session
) -> None:
    """Attempting to download a pending or failed certificate returns 409 Conflict."""
    job_id = str(uuid.uuid4())
    job = Job(id=job_id, event_name="Dummy Job", total_count=2)
    db_session.add(job)

    pending_cert = Certificate(
        id=str(uuid.uuid4()),
        job_id=job_id,
        row_index=0,
        status=CertificateStatus.PENDING,
        recipient_name="Pending Person",
    )
    failed_cert = Certificate(
        id=str(uuid.uuid4()),
        job_id=job_id,
        row_index=1,
        status=CertificateStatus.FAILED,
        recipient_name="Failed Person",
        error_message="Validation error",
    )
    db_session.add_all([pending_cert, failed_cert])
    db_session.commit()

    # 1. Pending download -> 409
    resp_pending = client.get(f"/api/v1/certificates/{pending_cert.id}/download")
    assert resp_pending.status_code == 409
    assert "is not available for download" in resp_pending.json()["detail"]

    # 2. Failed download -> 409
    resp_failed = client.get(f"/api/v1/certificates/{failed_cert.id}/download")
    assert resp_failed.status_code == 409
    assert "is not available for download" in resp_failed.json()["detail"]


def test_download_single_certificate_404_for_unknown_id(client: TestClient) -> None:
    """Requesting download for an unknown certificate ID returns 404."""
    fake_id = str(uuid.uuid4())
    resp = client.get(f"/api/v1/certificates/{fake_id}/download")
    assert resp.status_code == 404
    assert f"Certificate not found: {fake_id}" in resp.json()["detail"]


def test_download_job_zip_contains_all_successful_certificates(
    client: TestClient
) -> None:
    """Job ZIP download packages exactly the successful certificates with sanitized names."""
    payload = {
        "event_name": "ZIP Archive Event",
        "recipients": [
            {"name": "Bruce Wayne", "email": "bruce@example.com"},
            {"name": "Clark Kent", "email": "clark@example.com"},
            {"name": "", "email": "invalid-email"},  # Failed
        ],
    }
    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    job_id = create_resp.json()["job_id"]

    zip_resp = client.get(f"/api/v1/certificate-jobs/{job_id}/download")
    assert zip_resp.status_code == 200
    assert "application/zip" in zip_resp.headers["content-type"]
    assert f'filename="certificates-{job_id}.zip"' in zip_resp.headers["content-disposition"]

    # Inspect the ZIP payload in-memory
    zip_bytes = io.BytesIO(zip_resp.content)
    with zipfile.ZipFile(zip_bytes, "r") as zf:
        file_list = zf.namelist()
        assert len(file_list) == 2  # Only the 2 successful recipients

        for name in file_list:
            assert name.endswith(".pdf")
            data = zf.read(name)
            assert data.startswith(b"%PDF")


def test_download_job_zip_with_no_successes_returns_409(client: TestClient) -> None:
    """Requesting ZIP download for a job with no successful certificates returns 409."""
    payload = {
        "event_name": "No Successes Event",
        "recipients": [{"name": "", "email": "bad"}],
    }
    create_resp = client.post("/api/v1/certificate-jobs", json=payload)
    job_id = create_resp.json()["job_id"]

    zip_resp = client.get(f"/api/v1/certificate-jobs/{job_id}/download")
    assert zip_resp.status_code == 409
    assert "No successful certificates are available" in zip_resp.json()["detail"]
